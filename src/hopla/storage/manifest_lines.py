"""Manifest lines (ADR-0003 schema v1): `run_start`, `fetch`, `run_end`, and their codec.

Lines are JSON objects with `type` and `v`; unknown fields are ignored when read, an unknown
`type` or a newer `v` is an error. Every time is aware UTC. A line checks itself when built,
so a part read from the store is either valid v1 or corrupt. Fetch lines refuse what must
never be stored (NFR-051): cookies and auth headers, credentials or tokens in URLs.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from hopla.core.time import local_date, require_aware

VERSION = 1
MAX_ATTEMPTS = 3  # one request and two retries at most (ADR-0002 §3)
HEADERS = frozenset({"etag", "last-modified", "date", "cache-control"})
NOTICE_HEADERS = HEADERS | {"x-wp-total", "x-wp-totalpages", "link"}
# Parameter-name words that carry secrets. A name is split into words (camelCase, then any
# non-alphanumeric) and checked word by word: `access_token`, `apiKey` and `auth[token]` are
# refused while `author` or `keyword` pass.
SECRET_WORDS = frozenset(
    {"token", "key", "apikey", "sig", "signature", "secret", "session", "sessionid", "sid",
     "jsessionid", "auth", "authorization", "password", "passwd", "pwd", "pass", "nonce",
     "csrf", "xsrf", "jwt", "credential", "credentials", "otp"}
)  # fmt: skip
_ERROR = re.compile(r"[a-z][a-z_]*(?::[a-z0-9_.-]+)?")  # e.g. transport:timeout, store:server


class ManifestCorrupt(ValueError):
    pass


class LineKind(StrEnum):
    DEPARTURES = "departures"
    NOTICES = "notices"
    ROBOTS = "robots"
    TERMS = "terms"


class RunEndStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"  # paused or not the active runner (S11): no requests, not in the ledger


def _set_utc(obj: object, *names: str) -> None:
    for name in names:
        value = getattr(obj, name)
        if value is not None:
            object.__setattr__(obj, name, require_aware(value, name))


@dataclass(frozen=True)
class UnitSlot:
    source: str
    bucket: str
    slot: datetime

    def __post_init__(self) -> None:
        _set_utc(self, "slot")
        _require_text(self.source, self.bucket)


@dataclass(frozen=True)
class RunStart:
    run_id: str
    source: str  # the run's prefix: a source id, `_host/<fqdn>` or `_job/<id>`
    runner: str
    collector_version: str
    started_at: datetime  # runner clock: backoff counts from it (S3)
    hosts: tuple[str, ...]  # the host names this run may contact (S8)
    units: tuple[UnitSlot, ...]  # the ledger units it serves; none for `_host` runs
    v: int = VERSION

    def __post_init__(self) -> None:
        _set_utc(self, "started_at")
        _require_version(self.v)
        _require_text(self.run_id, self.source, self.runner, self.collector_version, *self.hosts)
        if any(unit.source != self.source for unit in self.units):
            raise ValueError("a run serves units of its own source only (one run, one prefix)")
        object.__setattr__(self, "hosts", tuple(self.hosts))
        object.__setattr__(self, "units", tuple(self.units))


@dataclass(frozen=True)
class FetchLine:
    """One request (and its retries), field for field as ADR-0003 lists them."""

    run_id: str
    kind: LineKind
    host: str
    url: str  # query parameters already reduced to the source's allow-list
    fetched_at: datetime
    runner: str
    collector_version: str
    status: int | None  # None: no response (transport error)
    requests_used: int  # attempts, retries included: what the budget counts
    elapsed_ms: int
    query: Mapping[str, str] | None = None
    scheduled_for: datetime | None = None
    transport: Literal["http", "playwright_xhr"] = "http"
    content_type: str | None = None
    headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    stored: bool = False
    sha: str | None = None
    key: str | None = None
    bytes: int | None = None
    gz_bytes: int | None = None
    not_modified: bool = False
    error: str | None = None
    v: int = VERSION

    def __post_init__(self) -> None:
        _set_utc(self, "fetched_at", "scheduled_for")
        _require_version(self.v)
        object.__setattr__(self, "kind", LineKind(self.kind))
        _check_types(self)
        _check_privacy(self)
        _check_body(self)
        _check_outcome(self)


@dataclass(frozen=True)
class RunEnd:
    run_id: str
    status: RunEndStatus
    ended_at: datetime  # runner clock
    fetches: int
    requests_used: int
    error: str | None = None  # e.g. "store:server", "tick_budget"
    v: int = VERSION

    def __post_init__(self) -> None:
        _set_utc(self, "ended_at")
        _require_version(self.v)
        _require_text(self.run_id)
        object.__setattr__(self, "status", RunEndStatus(self.status))
        _require_counts(self.fetches, self.requests_used)
        if self.error is not None and not _ERROR.fullmatch(self.error):
            raise ValueError(f"an error is a code such as store:server, not {self.error!r}")


def _require_version(v: object) -> None:
    if not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= VERSION):
        raise ValueError(f"unsupported manifest version {v!r}")


def _require_text(*values: object) -> None:
    if not all(isinstance(v, str) for v in values):
        raise ValueError("expected text")


def _check_types(line: FetchLine) -> None:
    """A line read from the store may hold anything: a wrong type is a corrupt part here, not
    a TypeError later in a reader (NFR-064)."""
    _require_text(line.run_id, line.host, line.url, line.runner, line.collector_version)
    _require_text(*line.headers.values(), *(line.query or {}).values())
    _require_text(*(v for v in (line.sha, line.key, line.content_type, line.error) if v))
    status = line.status
    if status is not None and not (_is_int(status) and 100 <= status <= 599):
        raise ValueError(f"a status is an HTTP status code, not {status!r}")
    if not (isinstance(line.stored, bool) and isinstance(line.not_modified, bool)):
        raise ValueError("stored and not_modified are true or false")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_counts(*counts: object) -> None:
    if not all(isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in counts):
        raise ValueError("counts are whole numbers, never negative")


def _words(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()))


def _secret_names(names: Iterable[str]) -> list[str]:
    return [n for n in names if SECRET_WORDS & _words(n)]


def _check_privacy(line: FetchLine) -> None:
    """NFR-051: no cookies, auth headers, credentials or token-like parameters, anywhere."""
    allowed = NOTICE_HEADERS if line.kind is LineKind.NOTICES else HEADERS
    if bad := sorted(set(line.headers) - allowed):
        raise ValueError(f"headers not allowed in a manifest: {bad}")
    parts = urlsplit(line.url)
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment:
        raise ValueError("a manifest URL is https, with no credentials or fragment")
    if ";" in parts.path or ";" in parts.query:
        raise ValueError("a manifest URL has no `;` parameters (;jsessionid=…)")
    names = [k for k, _ in parse_qsl(parts.query, keep_blank_values=True)]
    if leaks := _secret_names([*names, *(line.query or {})]):
        raise ValueError(f"a manifest keeps no token-like parameters: {leaks}")
    if parts.hostname != line.host:
        raise ValueError(f"host {line.host!r} isn't the URL's host: the budget is per host")
    if line.error is not None and not _ERROR.fullmatch(line.error):
        raise ValueError(f"an error is a code such as transport:timeout, not {line.error!r}")


def _check_body(line: FetchLine) -> None:
    sizes = (line.sha, line.key, line.bytes, line.gz_bytes)
    if line.stored != all(x is not None for x in sizes) or (not line.stored and line.key):
        raise ValueError("a stored line has sha, key and sizes; an unstored one has no key")
    if not line.stored:
        return
    if line.status is None:
        raise ValueError("a stored body came with a response status")
    tail = f"/{local_date(line.fetched_at)}/{line.sha}.gz"
    if not (str(line.key).startswith("raw/") and str(line.key).endswith(tail)):
        raise ValueError("the body key is raw/…/<the fetch's Belgrade date>/<sha>.gz")
    _require_counts(line.bytes, line.gz_bytes)


def _check_outcome(line: FetchLine) -> None:
    if line.transport not in ("http", "playwright_xhr"):
        raise ValueError(f"unknown transport {line.transport!r}")
    if line.not_modified != (line.status == 304):
        raise ValueError("a 304 is exactly a not-modified fetch")
    if line.not_modified and line.kind is LineKind.DEPARTURES:
        raise ValueError("departure fetches are never conditional, so never 304 (ADR-0002 §3)")
    if line.error and line.error.startswith("store") and line.stored:
        raise ValueError("a store error means the body wasn't stored")
    _require_counts(line.requests_used, line.elapsed_ms)
    if line.requests_used > MAX_ATTEMPTS:
        raise ValueError(f"a fetch makes at most {MAX_ATTEMPTS} attempts")


Line = RunStart | FetchLine | RunEnd
_TYPES: dict[str, type[RunStart] | type[FetchLine] | type[RunEnd]] = {
    "run_start": RunStart,
    "fetch": FetchLine,
    "run_end": RunEnd,
}
_TYPE_OF = {cls: name for name, cls in _TYPES.items()}
_TIME_FIELDS = frozenset({"started_at", "fetched_at", "scheduled_for", "ended_at", "slot"})


def encode_part(lines: Iterable[Line]) -> bytes:
    """Deterministic JSON lines: sorted keys, compact, UTF-8, a newline after each."""
    return b"".join(
        json.dumps(
            {"type": _TYPE_OF[type(line)], **_to_json(line)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        + b"\n"
        for line in lines
    )


def decode_part(data: bytes) -> tuple[Line, ...]:
    """The lines of one part. Anything that isn't a valid v1 line makes the part corrupt."""
    try:
        return tuple(
            _decode_line(json.loads(raw)) for raw in data.decode().splitlines() if raw.strip()
        )
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as err:
        raise ManifestCorrupt(f"{type(err).__name__}: {err}") from err


def _to_json(line: Line) -> dict[str, Any]:
    return {f.name: _value_to_json(getattr(line, f.name)) for f in fields(line)}


def _value_to_json(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {k: _value_to_json(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_value_to_json(v) for v in value]
    if isinstance(value, UnitSlot):
        return {"source": value.source, "bucket": value.bucket, "slot": value.slot.isoformat()}
    return value


def _decode_line(obj: object) -> Line:
    if not isinstance(obj, dict):
        raise ManifestCorrupt("a manifest line is a JSON object")
    cls = _TYPES.get(obj.get("type", ""))
    if cls is None:
        raise ManifestCorrupt(f"unknown line type {obj.get('type')!r}")
    if obj.get("v", VERSION) > VERSION:
        raise ManifestCorrupt(f"manifest version {obj['v']} is newer than {VERSION}")
    known = {f.name for f in fields(cls)}
    kwargs = {k: _value_from_json(k, v) for k, v in obj.items() if k in known}
    return cls(**kwargs)


def _value_from_json(name: str, value: Any) -> Any:
    if name in _TIME_FIELDS and value is not None:
        return datetime.fromisoformat(value)
    if name == "units":
        return tuple(
            UnitSlot(u["source"], u["bucket"], datetime.fromisoformat(u["slot"])) for u in value
        )
    if name == "hosts":
        return tuple(value)
    if name in ("headers", "query") and value is not None:
        return MappingProxyType(dict(value))
    return value
