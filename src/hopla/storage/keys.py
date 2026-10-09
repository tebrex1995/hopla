"""Where everything lives in the raw store (ADR-0003). Pure functions; no I/O.

Dates in keys are the Europe/Belgrade date (a 00:30 fetch belongs to the new day). A body's
date is its fetch's; a run part's date is its run's start.

    raw/<prefix>/<date>/<sha>.gz              response bodies (gzip), 90-day lifecycle
    runs/<prefix>/<date>/<run_id>/<seq>.jsonl  run manifests, numbered parts, never expire
    ops/flags.json, ops/disabled/<source>.json, ops/audit/<date>/<id>.json
    _preflight/<runner>                        the store check each run starts with

`<prefix>` is a source id (`srbijavoz`), `_host/<fqdn>` (robots and terms fetches) or
`_job/<job id>` (glue jobs, R0). One run serves one prefix.
"""

import re
from datetime import date, datetime

from hopla.core.time import local_date

FLAGS_KEY = "ops/flags.json"
DISABLED_PREFIX = "ops/disabled/"
RAW_ROOT, RUNS_ROOT, OPS_ROOT, PREFLIGHT_ROOT = "raw/", "runs/", "ops/", "_preflight/"
MAX_SEQ = 9999

_SOURCE_ID = re.compile(r"[a-z][a-z0-9_]*")
_FQDN = re.compile(r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_JOB_ID = re.compile(r"[a-z][a-z0-9_.]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")  # a ULID (storage.ids)
_RUNNER = re.compile(r"[a-z][a-z0-9-]*")


def _check(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"not a valid {what}: {value!r}")
    return value


def source_prefix(source_id: str) -> str:
    return _check(_SOURCE_ID, source_id, "source id")


def host_prefix(fqdn: str) -> str:
    return f"_host/{_check(_FQDN, fqdn, 'host name')}"


def job_prefix(job_id: str) -> str:
    return f"_job/{_check(_JOB_ID, job_id, 'job id')}"


def check_prefix(prefix: str) -> str:
    """A run or body prefix: a source id, `_host/<fqdn>` or `_job/<id>`."""
    kind, _, name = prefix.partition("/")
    valid = (
        (kind == "_host" and _FQDN.fullmatch(name))
        or (kind == "_job" and _JOB_ID.fullmatch(name))
        or (not name and _SOURCE_ID.fullmatch(kind))
    )
    if not valid:
        raise ValueError(f"not a valid prefix: {prefix!r}")
    return prefix


def body_key(prefix: str, fetched_at: datetime, sha: str) -> str:
    sha = _check(_SHA256, sha, "sha256")
    return f"{RAW_ROOT}{check_prefix(prefix)}/{_day(fetched_at)}/{sha}.gz"


def runs_day_prefix(prefix: str, day: date) -> str:
    return f"{RUNS_ROOT}{check_prefix(prefix)}/{day.isoformat()}/"


def part_key(prefix: str, started_at: datetime, run_id: str, seq: int) -> str:
    if not 0 <= seq <= MAX_SEQ:
        raise ValueError(f"part number out of range: {seq}")
    run = _check(_RUN_ID, run_id, "run id")
    return f"{RUNS_ROOT}{check_prefix(prefix)}/{_day(started_at)}/{run}/{seq:04d}.jsonl"


def disable_key(source_id: str) -> str:
    return f"{DISABLED_PREFIX}{source_prefix(source_id)}.json"


def audit_key(at: datetime, audit_id: str) -> str:
    return f"{OPS_ROOT}audit/{_day(at)}/{_check(_RUN_ID, audit_id, 'audit id')}.json"


def preflight_key(runner: str) -> str:
    return f"{PREFLIGHT_ROOT}{_check(_RUNNER, runner, 'runner name')}"


def _day(instant: datetime) -> str:
    return local_date(instant).isoformat()
