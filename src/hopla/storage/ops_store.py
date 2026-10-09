"""Ops state in the store (ADR-0004 S7, S11; ADR-0002 §4): flags, disables, a tick's reads.

- `ops/flags.json`: `pause_collection` and the active runner per host. Absent means defaults;
  anything unknown or malformed fails closed (it is the kill switch).
- `ops/disabled/<source>.json`: written **create-only** by the run that trips an access-control,
  robots or terms check. It is the only authority for automatic disables; a second trip keeps
  the first reason. Only `enable` (a human, `hopla source enable`) or the robots self-clear
  deletes one, only if it is still the one read, and each deletion leaves an audit object.
- Budget and robots answers come from one `ManifestIndex` per tick (no DB, no cache), so a fresh
  runner sees what the previous one did.
"""

import contextlib
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from hopla.core.clock import Clock
from hopla.core.time import local_date, require_aware
from hopla.storage.ids import new_ulid
from hopla.storage.keys import DISABLED_PREFIX, FLAGS_KEY, audit_key, disable_key
from hopla.storage.manifest_lines import FetchLine, LineKind
from hopla.storage.manifests import CorruptPart, ManifestIndex
from hopla.storage.raw_store import ObjectMissing, RawStore, StoreError

_JSON = "application/json"
ROBOTS_MAX_AGE = timedelta(hours=24)  # RFC 9309 cache, ADR-0002 §4
RUNNERS = frozenset({"github", "mac", "vps"})
FLAG_KEYS = frozenset({"pause_collection", "active_runner"})
# Requests charged for a manifest part that can't be read: at most 10 lines of up to 3 attempts.
CORRUPT_PART_CHARGE = 30


class OpsStoreError(StoreError):
    """An ops object exists but can't be understood: the run fails closed (preflight)."""

    def __init__(self, key: str, detail: str) -> None:
        super().__init__("invalid", "read", key, detail)


@dataclass(frozen=True)
class Flags:
    pause_collection: bool = False
    active_runner: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


class DisableReason(StrEnum):
    ACCESS_CONTROL = "access_control"
    ROBOTS = "robots"
    TERMS = "terms"


@dataclass(frozen=True)
class Disable:
    source: str
    reason: DisableReason
    at: datetime
    run_id: str | None = None
    detail: str | None = None  # e.g. "access_control:unexpected"
    raw_key: str | None = None  # the evidence: the stored body that tripped it


class OpsStore:
    def __init__(self, store: RawStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    async def read_flags(self) -> Flags:
        try:
            raw = await self._store.get(FLAGS_KEY)
        except ObjectMissing:
            return Flags()
        obj = _load_object(FLAGS_KEY, raw)
        # This is the kill switch: a typo ({"pause": true}) must stop the run, not be ignored.
        if unknown := sorted(obj.keys() - FLAG_KEYS):
            raise OpsStoreError(FLAGS_KEY, f"unknown flags {unknown}")
        pause, runners = obj.get("pause_collection", False), obj.get("active_runner", {})
        if not isinstance(pause, bool):
            raise OpsStoreError(FLAGS_KEY, "pause_collection is true or false")
        if not _is_text_map(runners) or not set(runners.values()) <= RUNNERS:
            raise OpsStoreError(FLAGS_KEY, f"active_runner maps hosts to {sorted(RUNNERS)}")
        return Flags(pause, MappingProxyType(dict(runners)))

    async def write_flags(self, flags: Flags) -> None:
        body = {
            "pause_collection": flags.pause_collection,
            "active_runner": dict(flags.active_runner),
        }
        await self._store.put(FLAGS_KEY, _dump(body), content_type=_JSON)

    async def disabled_sources(self) -> Mapping[str, Disable]:
        """Every disabled source. The source is the key's (`ops/disabled/<source>.json`); a body
        naming another source fails closed. Objects not ending in `.json` are ignored."""
        found = {}
        for obj in (await self._store.list(DISABLED_PREFIX)).objects:
            name = obj.key.removeprefix(DISABLED_PREFIX)
            if not name.endswith(".json"):
                continue
            try:
                raw = await self._store.get(obj.key)
            except ObjectMissing:  # enabled between the list and the get
                continue
            disable = _parse_disable(obj.key, raw)
            if disable.source != name.removesuffix(".json"):
                raise OpsStoreError(obj.key, f"names source {disable.source!r}, not its own")
            found[disable.source] = disable
        return MappingProxyType(found)

    async def disable(
        self,
        source: str,
        reason: DisableReason,
        *,
        at: datetime,
        run_id: str | None = None,
        detail: str | None = None,
        raw_key: str | None = None,
    ) -> bool:
        """Create the disable object; False if one exists (it is kept as it is)."""
        disable = Disable(
            source, DisableReason(reason), require_aware(at, "at"), run_id, detail, raw_key
        )
        return await self._store.put_if_absent(
            disable_key(source), _dump(_disable_json(disable)), content_type=_JSON
        )

    async def enable(self, source: str, *, actor: str) -> bool:
        """`hopla source enable`: delete the disable, whatever its reason, and audit it."""
        return await self._delete_disable(source, actor=actor, only=None)

    async def clear_robots_disable(self, source: str, *, run_id: str) -> bool:
        """The robots self-clear: deletes a robots disable only, never terms or access control."""
        return await self._delete_disable(
            source, actor=f"robots-self-clear:{run_id}", only=DisableReason.ROBOTS
        )

    async def _delete_disable(self, source: str, *, actor: str, only: DisableReason | None) -> bool:
        """Audit, then delete the disable that was read, and only that one: if a new trip
        replaced it meanwhile, the delete fails with `conflict` and the new one stays. A failed
        delete leaves a second audit record saying so, so the audit never claims an enable
        that didn't happen."""
        key = disable_key(source)
        info = await self._store.head(key)
        if info is None:
            return False
        try:
            previous = _parse_disable(key, await self._store.get(key))
        except ObjectMissing:  # enabled meanwhile by someone else
            return False
        if await self._store.head(key) != info:  # `previous` must be the body with that etag
            raise StoreError("conflict", "enable", key, "changed while it was read")
        if only is not None and previous.reason is not only:
            return False
        audit = {
            "action": "enable",
            "source": source,
            "actor": actor,
            "previous": _disable_json(previous),
            "etag": info.etag,
        }
        audit_at = await self._audit(audit)
        try:
            await self._store.delete(key, if_match=info.etag)
        except StoreError as err:
            with contextlib.suppress(StoreError):
                if await self._store.head(key) is None:  # it landed; only the answer was lost
                    return True
            failed = {"action": "enable_failed", "source": source, "audit": audit_at}
            with contextlib.suppress(StoreError):  # the delete's error is the one to raise
                await self._audit(failed | {"error": err.manifest_error()})
            raise
        return True

    async def _audit(self, record: Mapping[str, Any]) -> str:
        now = self._clock.now()
        key = audit_key(now, new_ulid(now))
        body = _dump({**record, "at": now.isoformat()})
        if not await self._store.put_if_absent(key, body, content_type=_JSON):
            raise StoreError("exists", "put_if_absent", key, "audit id collision")
        return key


@dataclass(frozen=True)
class RequestCounts:
    """Requests on one Belgrade day (S7). `unattributed` counts against every host: parts
    that can't be read and whose hosts can't be known."""

    by_host: Mapping[str, int]
    unattributed: int = 0

    def used(self, host: str) -> int:
        return self.by_host.get(host, 0) + self.unattributed


def requests_by_host(index: ManifestIndex, day: date) -> RequestCounts:
    """Requests per host name on a Belgrade day (S7), counted by each fetch's own date.

    Runs still in progress count, and so do robots and terms fetches. Load the index for the
    day before too: a run that started at 23:58 may fetch at 00:01. The budget never counts
    too low, so a part that can't be read is charged `CORRUPT_PART_CHARGE` on its run's day
    and the next, to every host the run may contact: its start's hosts; if the start is lost
    too, the `_host/` prefix's host or the hosts its prefix's other runs contact; failing all
    that, every host.
    """
    totals: defaultdict[str, int] = defaultdict(int)
    for _, line in index.lines():
        if local_date(line.fetched_at) == day:
            totals[line.host] += line.requests_used
    unattributed = 0
    for part in index.corrupt:
        if part.lines is not None:  # readable, just not the ledger's: counted exactly
            for line in part.lines:
                if local_date(line.fetched_at) == day:
                    totals[line.host] += line.requests_used
        elif day - timedelta(days=1) <= part.day <= day:
            hosts = _hosts_of(index, part)
            for host in hosts or ():
                totals[host] += CORRUPT_PART_CHARGE
            unattributed += CORRUPT_PART_CHARGE if hosts is None else 0
    return RequestCounts(MappingProxyType(dict(totals)), unattributed)


def _hosts_of(index: ManifestIndex, part: CorruptPart) -> tuple[str, ...] | None:
    """The hosts a corrupt part's run may have contacted; None if they can't be known."""
    for run in index.runs:
        if run.run_id == part.run_id and run.start is not None:
            return run.start.hosts
    if part.prefix.startswith("_host/"):
        return (part.prefix.removeprefix("_host/"),)
    known = {
        h for run in index.runs if run.prefix == part.prefix and run.start for h in run.start.hosts
    }
    return tuple(sorted(known)) or None


def latest_robots(index: ManifestIndex, host: str, *, now: datetime) -> FetchLine | None:
    """The newest robots.txt answer for `host` younger than 24 h, if it was a definitive one.

    Only a stored 2xx (rules) or 4xx (no rules) is cached. A 429 is a transport answer like a
    5xx or a network error (ADR-0002 §3): it blocks only the run that got it, and the next run
    asks again (ADR-0002 §4).
    """
    oldest = require_aware(now, "now") - ROBOTS_MAX_AGE
    candidates = [
        line
        for _, line in index.lines()
        if line.kind is LineKind.ROBOTS
        and line.host == host
        and line.stored
        and line.status is not None
        and 200 <= line.status < 500
        and line.status != 429
        and line.fetched_at > oldest
    ]
    return max(candidates, key=lambda line: line.fetched_at, default=None)


def latest_validators(index: ManifestIndex, url: str) -> FetchLine | None:
    """The newest good stored notice, robots or terms fetch of `url` (a 2xx, or a 304 copy of
    one): its ETag/Last-Modified and body feed the next conditional request. An error page's
    validators never do, and departures never use validators (ADR-0002 §3)."""
    candidates = [
        line
        for _, line in index.lines()
        if line.url == url
        and line.stored
        and line.kind is not LineKind.DEPARTURES
        and (line.not_modified or (line.status is not None and 200 <= line.status < 300))
    ]
    return max(candidates, key=lambda line: line.fetched_at, default=None)


def _dump(obj: Mapping[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _load_object(key: str, raw: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(raw, object_pairs_hook=_no_duplicates)
    except (ValueError, UnicodeDecodeError, RecursionError) as err:
        raise OpsStoreError(key, f"not JSON: {err}") from err
    if not isinstance(obj, dict):
        raise OpsStoreError(key, "not a JSON object")
    return obj


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # {"pause_collection": true, "pause_collection": false} must not read as "not paused".
    if len(obj := dict(pairs)) != len(pairs):
        raise ValueError("a duplicate key")
    return obj


def _is_text_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


def _disable_json(disable: Disable) -> dict[str, Any]:
    return asdict(disable) | {"reason": disable.reason.value, "at": disable.at.isoformat()}


def _parse_disable(key: str, raw: bytes) -> Disable:
    obj = _load_object(key, raw)
    try:
        return Disable(
            source=obj["source"],
            reason=DisableReason(obj["reason"]),
            at=require_aware(datetime.fromisoformat(obj["at"]), "at"),
            run_id=obj.get("run_id"),
            detail=obj.get("detail"),
            raw_key=obj.get("raw_key"),
        )
    except (KeyError, ValueError, TypeError) as err:
        raise OpsStoreError(key, f"not a disable record: {err}") from err
