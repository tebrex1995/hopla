"""Committed fixtures are described, trimmed and free of personal data and secrets (04 §2.3, §6).

The repo is public: a fixture republishes an operator's bytes, so it carries only what a test
needs. Each case is `tests/fixtures/<source>/<case>/` with `response.body` (the bytes as served,
possibly cut to the rows and the element a test reads), `meta.json` (where they came from) and,
once a parser exists, `expected.json`. Whether a body parses is the parser's and detector's job
(T-R0-10); this file checks hygiene only. The scanner is `tests/contract/sensitive.py`.
"""

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TypeIs

import pytest

from tests.contract.sensitive import decode_text, hits, json_strings, scan_texts, text_views

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"
FIXTURES_DIR = TESTS_DIR / "fixtures"
EVALS_DIR = TESTS_DIR / "evals"  # LLM eval items (R3); scanned once they exist

CASE_FILES = frozenset({"response.body", "meta.json"})
OPTIONAL_CASE_FILES = frozenset({"expected.json"})
MAX_BODY_BYTES = 32 * 1024  # about 9 w3 trains in a div#rezultati span

META_KEYS = frozenset(
    {
        "source",
        "case",
        "origin",
        "query",
        "captured_at",
        "http_status",
        "content_type",
        "headers",
        "raw_sha",
        "body_sha256",
        "trimmed",
        "fingerprint",
        "scrubbed",
        "scrub",
        "notes",
    }
)
QUERY_KEYS = frozenset({"url", "method", "from_id", "to_id", "date"})
TRIMMED_KEYS = frozenset({"unit", "kept", "of", "kept_ids", "span"})
SHA_KEYS = frozenset({"raw_sha", "body_sha256"})  # 64 hex chars by design: not scanned
# Recon captures are re-pulled from hopla-raw in T-R0-10 (`hopla fixtures pull`, ADR-0003).
ORIGINS = frozenset({"recon", "raw_store"})
# ADR-0003's manifest header allow-list; notices also keep the WordPress pagination headers.
HEADERS = frozenset({"etag", "last-modified", "date", "cache-control"})
NOTICE_HEADERS = HEADERS | {"x-wp-total", "x-wp-totalpages", "link"}
SHA256 = re.compile(r"[0-9a-f]{64}")
HTTPS_URL = re.compile(r"https://[^/\s]+(?:/\S*)?")
SPAN = re.compile(r"(?P<tag>[a-z][a-z0-9]*)#(?P<id>[\w-]+)")  # e.g. div#rezultati


def _is_int(value: object) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value: object) -> TypeIs[str]:
    return isinstance(value, str) and bool(value)


def _is_utc_timestamp(value: object) -> bool:
    try:
        datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(SHA256.fullmatch(value))


def _is_rule_list(value: object) -> bool:
    return isinstance(value, list) and all(map(_is_text, value))


# The meta fields checked on their own: (key, valid?, what's wrong otherwise).
_FIELD_CHECKS: tuple[tuple[str, Callable[[object], bool], str], ...] = (
    ("origin", lambda v: v in ORIGINS, f"origin must be one of {sorted(ORIGINS)}"),
    ("captured_at", _is_utc_timestamp, "captured_at must be UTC time, e.g. 2026-10-09T11:14:28Z"),
    ("http_status", lambda v: _is_int(v) and 100 <= v <= 599, "http_status must be HTTP status"),
    ("content_type", _is_text, "content_type must be the served header value"),
    ("raw_sha", _is_sha256, "raw_sha must be a sha256 hex digest"),
    ("scrubbed", lambda v: v is True, "scrubbed must be true"),
    ("scrub", _is_rule_list, "scrub must list the scrub rules applied (may be empty)"),
    ("notes", lambda v: isinstance(v, str), "notes must be text"),
)


def meta_problems(meta: object, source: str, case: str, body: bytes) -> list[str]:
    """Why `meta` doesn't describe `body` at tests/fixtures/<source>/<case>/ (empty if it does)."""
    if not isinstance(meta, dict):
        return ["meta.json must be a JSON object"]
    problems = []
    if missing := META_KEYS - meta.keys():
        problems.append(f"missing keys {sorted(missing)}")
    if extra := meta.keys() - META_KEYS:
        problems.append(f"unknown keys {sorted(extra)}")
    if (meta.get("source"), meta.get("case")) != (source, case):
        problems.append("source/case don't match the folder")
    problems += [message for key, valid, message in _FIELD_CHECKS if not valid(meta.get(key))]
    problems += _query_problems(meta.get("query"))
    problems += _headers_problems(meta.get("headers"), source)
    body_sha = hashlib.sha256(body).hexdigest()
    if meta.get("body_sha256") != body_sha:
        problems.append("body_sha256 doesn't match response.body")
    same_bytes = meta.get("raw_sha") == body_sha
    problems += _trimmed_problems(meta.get("trimmed"), body, same_bytes=same_bytes)
    problems += _fingerprint_problems(meta.get("fingerprint"), body)
    return problems


def _query_problems(query: object) -> list[str]:
    if not isinstance(query, dict):
        return ["query must be an object"]
    problems = []
    if extra := sorted(query.keys() - QUERY_KEYS):
        problems.append(f"query has unknown keys {extra}")
    if not HTTPS_URL.fullmatch(str(query.get("url"))):
        problems.append("query.url must be an https URL")
    if query.get("method") != "GET":
        problems.append("query.method must be GET (sources are only read)")
    return problems


def _headers_problems(headers: object, source: str) -> list[str]:
    if not isinstance(headers, dict):
        return ["headers must be an object"]
    allowed = NOTICE_HEADERS if source.startswith("notices_") else HEADERS
    problems = []
    if outside := sorted(headers.keys() - allowed):
        problems.append(f"headers outside the allow-list: {outside}")
    if not all(isinstance(value, str) for value in headers.values()):
        problems.append("header values must be text")
    return problems


def _trimmed_problems(trimmed: object, body: bytes, *, same_bytes: bool) -> list[str]:
    if trimmed is None:
        return [] if same_bytes else ["an untrimmed body must have raw_sha == body_sha256"]
    if not isinstance(trimmed, dict):
        return ["trimmed must be null or an object"]
    problems = []
    if same_bytes:
        problems.append("a trimmed body can't have raw_sha == body_sha256")
    if extra := sorted(trimmed.keys() - TRIMMED_KEYS):
        problems.append(f"trimmed has unknown keys {extra}")
    unit, kept, of, ids = (trimmed.get(k) for k in ("unit", "kept", "of", "kept_ids"))
    if not _is_text(unit):
        problems.append("trimmed.unit must name what was counted")
    if not (_is_int(kept) and _is_int(of) and 0 < kept < of):
        problems.append("trimmed must keep 1 to of - 1 items (keeping all is untrimmed)")
    if not (isinstance(ids, list) and all(map(_is_text, ids))):
        problems.append("trimmed.kept_ids must be a list of non-empty strings")
    elif len(set(ids)) != len(ids) or len(ids) != kept:
        problems.append("trimmed.kept_ids must be unique, one per kept item")
    elif absent := [i for i in ids if i.encode() not in body]:
        problems.append(f"trimmed.kept_ids must appear in the body: {absent}")
    problems += _span_problems(trimmed.get("span"), body)
    return problems


def _span_problems(span: object, body: bytes) -> list[str]:
    # `span`: the body is only this element's served bytes, start tag to end tag (or null).
    if span is None:
        return []
    if not (isinstance(span, str) and (m := SPAN.fullmatch(span))):
        return ["trimmed.span must be null or tag#id, e.g. div#rezultati"]
    start, end = f'<{m["tag"]} id="{m["id"]}"'.encode(), f"</{m['tag']}>".encode()
    if not (body.startswith(start) and body.endswith(end)):
        return [f"the body must be exactly the {span} element"]
    return []


def _fingerprint_problems(fingerprint: object, body: bytes) -> list[str]:
    # The M1 shape record: literal strings the detector looks for (ADR-0002 §4).
    if not (isinstance(fingerprint, dict) and all(map(_is_text, fingerprint.values()))):
        return ["fingerprint must map names to non-empty literal markers"]
    if body and not fingerprint:
        return ["fingerprint must record the shape of a non-empty body"]
    if absent := sorted(k for k, v in fingerprint.items() if v.encode() not in body):
        return [f"fingerprint markers not in the body: {absent}"]
    return []


def case_file_problems(names: set[str]) -> list[str]:
    """Why a case folder holding `names` isn't a valid case (empty if it is)."""
    problems = []
    if missing := CASE_FILES - names:
        problems.append(f"missing {sorted(missing)}")
    if extra := names - CASE_FILES - OPTIONAL_CASE_FILES:
        problems.append(f"unexpected {sorted(extra)}")
    return problems


def body_size_ok(size: int) -> bool:
    return size <= MAX_BODY_BYTES


def texts_to_scan(name: str, raw: bytes) -> list[str]:
    """What to scan in one fixture file. meta.json skips only its own top-level sha digests."""
    if name != "meta.json":
        return scan_texts(raw)
    meta = json.loads(decode_text(raw))
    if isinstance(meta, dict):
        meta = {k: v for k, v in meta.items() if k not in SHA_KEYS}
    return [view for value in json_strings(meta) for view in text_views(value)]


def committable_files(*dirs: Path) -> list[Path]:
    """Files under `dirs` that git would commit (tracked, or new and not ignored).

    Git decides, not the file system: an ignored `.DS_Store` isn't a fixture, an unignored
    stray file is. Symlinks are listed too, so the layout checks reject them.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *dirs],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as err:
        raise pytest.UsageError(f"the fixture hygiene checks need a git worktree: {err}") from err
    return sorted(REPO_ROOT / os.fsdecode(p) for p in out.split(b"\0") if p)


FILES = committable_files(FIXTURES_DIR)
CASES = sorted({p.parent for p in FILES if len(p.relative_to(FIXTURES_DIR).parts) == 3})


def _case_id(case_dir: Path) -> str:
    return f"{case_dir.parent.name}/{case_dir.name}"


def test_fixtures_exist() -> None:
    # The per-case checks are parametrized: none of them would fail on an empty folder.
    assert FIXTURES_DIR / "srbijavoz" / "weekday" in CASES


def test_every_fixture_file_belongs_to_a_case() -> None:
    misplaced = [str(p) for p in FILES if len(p.relative_to(FIXTURES_DIR).parts) != 3]
    assert misplaced == []


def test_no_fixture_is_a_symlink() -> None:
    # A link could point at a file outside the repo, which would then be scanned and published.
    assert [str(p) for p in FILES if p.is_symlink()] == []


@pytest.mark.parametrize("case_dir", CASES, ids=_case_id)
def test_case_has_exactly_the_expected_files(case_dir: Path) -> None:
    names = {p.name for p in FILES if p.parent == case_dir}
    assert case_file_problems(names) == []


@pytest.mark.parametrize("case_dir", CASES, ids=_case_id)
def test_meta_describes_the_body(case_dir: Path) -> None:
    body_path, meta_path = case_dir / "response.body", case_dir / "meta.json"
    assert body_path.is_file() and meta_path.is_file(), "see the layout test for this case"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    assert meta_problems(meta, case_dir.parent.name, case_dir.name, body_path.read_bytes()) == []


@pytest.mark.parametrize("case_dir", CASES, ids=_case_id)
def test_body_is_trimmed(case_dir: Path) -> None:
    assert body_size_ok((case_dir / "response.body").stat().st_size)


def _scanned_files() -> list[Path]:
    evals = committable_files(EVALS_DIR) if EVALS_DIR.is_dir() else []
    return [p for p in FILES + evals if p.is_file() and not p.is_symlink()]


@pytest.mark.parametrize("path", _scanned_files(), ids=lambda p: str(p.relative_to(TESTS_DIR)))
def test_no_personal_data_or_secrets(path: Path) -> None:
    assert sorted(hits(texts_to_scan(path.name, path.read_bytes()))) == []


# The checks themselves.
def test_case_file_layout() -> None:
    assert case_file_problems({"response.body", "meta.json"}) == []
    assert case_file_problems({"response.body", "meta.json", "expected.json"}) == []
    assert case_file_problems({"response.body"}) == ["missing ['meta.json']"]
    assert case_file_problems({"response.body", "meta.json", "notes.txt"}) == [
        "unexpected ['notes.txt']"
    ]


def test_body_size_boundary() -> None:
    assert body_size_ok(32 * 1024)
    assert not body_size_ok(32 * 1024 + 1)


def test_git_failure_is_a_clear_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)

    with pytest.raises(pytest.UsageError, match="need a git worktree"):
        committable_files(FIXTURES_DIR)


BODY = b'<div id="rezultati"><table><tr><td>12541</td><td>12641</td></tr></table></div>'
TRIM = {
    "unit": "train",
    "kept": 2,
    "of": 5,
    "kept_ids": ["12541", "12641"],
    "span": "div#rezultati",
}


def _valid_meta(body: bytes = BODY) -> dict[str, object]:
    return {
        "source": "src",
        "case": "c",
        "origin": "recon",
        "query": {"url": "https://example.rs/x", "method": "GET", "from_id": 1, "to_id": 2},
        "captured_at": "2026-10-09T11:14:28Z",
        "http_status": 200,
        "content_type": "text/html; charset=utf-8",
        "headers": {"cache-control": "private"},
        "raw_sha": "a" * 64,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "trimmed": TRIM,
        "fingerprint": {"container": '<div id="rezultati"'},
        "scrubbed": True,
        "scrub": [],
        "notes": "",
    }


def _problems(change: Mapping[str, object], body: bytes = BODY, source: str = "src") -> list[str]:
    meta = {**_valid_meta(body), "source": source, **change}
    return meta_problems(meta, source, "c", body)


def _trim(**change: object) -> dict[str, object]:
    return {"trimmed": TRIM | change}


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"origin": "raw_store"},
        {"http_status": 100},
        {"http_status": 599},
        {"query": {"url": "https://example.rs", "method": "GET"}},  # no path
        _trim(span=None),
        {"trimmed": None, "raw_sha": hashlib.sha256(BODY).hexdigest()},
    ],
    ids=["trimmed", "raw-store", "status-100", "status-599", "url-no-path", "no-span", "untrimmed"],
)
def test_meta_check_accepts(change: Mapping[str, object]) -> None:
    assert _problems(change) == []


def test_only_notices_may_keep_the_wordpress_pagination_headers() -> None:
    change = {"headers": dict.fromkeys(sorted(NOTICE_HEADERS), "x")}

    assert _problems(change, source="notices_src") == []
    assert any("allow-list" in p for p in _problems(change))


def test_meta_check_accepts_an_empty_body_without_markers() -> None:
    # A 429 or 500 is stored without a body (ADR-0002 §1), and so is a 304.
    empty_sha = hashlib.sha256(b"").hexdigest()
    change: dict[str, object] = {
        "trimmed": None,
        "raw_sha": empty_sha,
        "fingerprint": {},
        "http_status": 429,
    }

    assert _problems(change, body=b"") == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"extra": 1}, "unknown keys"),
        ({"case": "other"}, "don't match the folder"),
        ({"source": "other"}, "don't match the folder"),
        ({"origin": "browser"}, "origin"),
        ({"query": "https://example.rs/x"}, "query must be an object"),
        ({"query": {"url": "http://example.rs/x", "method": "GET"}}, "https URL"),
        ({"query": {"url": "https://", "method": "GET"}}, "https URL"),
        ({"query": {"url": "https:///x", "method": "GET"}}, "https URL"),
        ({"query": {"url": "https://example.rs/x y", "method": "GET"}}, "https URL"),
        ({"query": {"url": "https://example.rs/x", "method": "POST"}}, "GET"),
        ({"query": {"url": "https://example.rs/x", "method": "GET", "raw_sha": "x"}}, "query has"),
        ({"captured_at": "2026-10-09 11:14"}, "UTC time"),
        ({"captured_at": "2026-99-99T99:99:99Z"}, "UTC time"),
        ({"captured_at": "2026-10-09T11:14:28+02:00"}, "UTC time"),
        ({"http_status": True}, "HTTP status"),
        ({"http_status": 99}, "HTTP status"),
        ({"http_status": 600}, "HTTP status"),
        ({"content_type": ""}, "content_type"),
        ({"headers": ["date"]}, "headers must be an object"),
        ({"headers": {"set-cookie": "a=b"}}, "allow-list"),
        ({"headers": {"server": "IIS"}}, "allow-list"),
        ({"headers": {"etag": ["x"]}}, "values must be text"),
        ({"raw_sha": "abc"}, "raw_sha must"),
        ({"raw_sha": "A" * 64}, "raw_sha must"),
        ({"raw_sha": "a" * 63}, "raw_sha must"),
        ({"raw_sha": "a" * 65}, "raw_sha must"),
        ({"body_sha256": "0" * 64}, "doesn't match"),
        ({"trimmed": []}, "null or an object"),
        ({"trimmed": None}, "untrimmed body must have raw_sha"),
        ({"raw_sha": hashlib.sha256(BODY).hexdigest()}, "trimmed body can't have raw_sha"),
        (_trim(extra=1), "trimmed has unknown keys"),
        (_trim(unit=""), "trimmed.unit"),
        (_trim(kept=0, kept_ids=[]), "keep 1 to of - 1"),
        (_trim(kept=5, kept_ids=list("abcde")), "keep 1 to of - 1"),
        (_trim(kept=True, kept_ids=["12541"]), "keep 1 to of - 1"),
        (_trim(kept_ids=[1, 2]), "non-empty strings"),
        (_trim(kept_ids=["", "12541"]), "non-empty strings"),
        (_trim(kept_ids=["12541", "12541"]), "unique"),
        (_trim(kept_ids=["12541"]), "unique"),
        (_trim(kept_ids=["12541", "99999"]), "must appear in the body"),
        (_trim(span=3), "tag#id"),
        (_trim(span="rezultati"), "tag#id"),
        (_trim(span="div#nope"), "exactly the div#nope element"),
        ({"fingerprint": {}}, "shape of a non-empty body"),
        ({"fingerprint": {"rows": 3}}, "literal markers"),
        ({"fingerprint": {"rows": ""}}, "literal markers"),
        ({"fingerprint": {"rows": "<tr class="}}, "not in the body"),
        ({"scrubbed": "yes"}, "scrubbed"),
        ({"scrubbed": 1}, "scrubbed"),
        ({"scrub": None}, "scrub must"),
        ({"scrub": [1]}, "scrub must"),
        ({"notes": None}, "notes"),
    ],
)
def test_meta_check_rejects(change: Mapping[str, object], problem: str) -> None:
    problems = _problems(change)

    assert any(problem in p for p in problems), problems


def test_meta_check_reports_missing_keys() -> None:
    meta = _valid_meta()
    del meta["scrub"]

    assert "missing keys ['scrub']" in meta_problems(meta, "src", "c", BODY)


def test_a_meta_that_isnt_an_object_is_refused() -> None:
    assert meta_problems(["a", "list"], "src", "c", BODY) == ["meta.json must be a JSON object"]


@pytest.mark.parametrize(
    "change",
    [
        {"notes": "a@example.rs"},
        {"notes": "a&#64;example.rs"},
        {"query": {"url": "https://x.rs/?m=a%40example.rs"}},
    ],
    ids=["plain", "html-escaped", "percent-encoded"],
)
def test_meta_values_are_scanned_with_their_escape_views(change: Mapping[str, object]) -> None:
    meta = {**_valid_meta(), **change}

    assert ("email", "a@example.rs") in hits(texts_to_scan("meta.json", json.dumps(meta).encode()))


def test_only_the_top_level_sha_digests_skip_the_scan() -> None:
    meta = {**_valid_meta(), "notes": {"raw_sha": "a@example.rs"}}

    found = hits(texts_to_scan("meta.json", json.dumps(meta).encode()))

    assert ("email", "a@example.rs") in found
    assert not any(rule == "hex_token" for rule, _ in found)  # the real digests were skipped
