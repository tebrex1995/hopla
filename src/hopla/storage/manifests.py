"""Run manifests (ADR-0003): what each run did, as JSON lines in numbered parts.

A run writes `run_start` alone in part `0000` before its first source request, then fetch
lines in parts of at most 10, and `run_end` last. Reading them back gives the run ledger
(ADR-0004 S1, through `core.ledger.fold`) and today's budget (S7), so all ops state lives in
the store and a fresh runner knows everything a previous one did. The lines themselves are
in `manifest_lines`.
"""

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import anyio

from hopla.core.ledger import LedgerSnapshot, RunOutcome, RunRecord, Unit, fold
from hopla.core.time import local_date, require_aware
from hopla.storage.keys import RUNS_ROOT, check_prefix, runs_day_prefix
from hopla.storage.manifest_lines import (
    FetchLine,
    Line,
    ManifestCorrupt,
    RunEnd,
    RunEndStatus,
    RunStart,
    decode_part,
)
from hopla.storage.raw_store import ObjectMissing, RawStore, StoreError

_PART = re.compile(r"(?P<run_id>[0-9A-HJKMNP-TV-Z]{26})/(?P<seq>\d{4})\.jsonl")
_GET_CONCURRENCY = 16


@dataclass(frozen=True)
class CorruptPart:
    """A part that couldn't be used. The budget still charges for it (ops_store)."""

    key: str
    error: str
    prefix: str
    day: date  # the run's Belgrade date (its folder)
    run_id: str
    seq: int
    # The fetches of a part that decoded but doesn't count for the ledger (after run_end): the
    # budget counts them exactly. None: unreadable, so the budget charges a flat amount.
    lines: tuple[FetchLine, ...] | None = None


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    prefix: str
    day: date  # the Belgrade date the run started on (its folder)
    start: RunStart | None  # None: part 0000 unreadable or missing
    started_server: datetime | None  # the store's LastModified of part 0000 (S4, S8)
    end: RunEnd | None
    lines: tuple[FetchLine, ...]


@dataclass(frozen=True)
class ManifestIndex:
    runs: tuple[RunSummary, ...]
    corrupt: tuple[CorruptPart, ...] = ()

    def records(self) -> tuple[RunRecord, ...]:
        """The ledger's input: judgeable runs, skipped ones left out (they changed nothing)."""
        return tuple(
            RunRecord(
                run_id=run.run_id,
                slots=tuple((Unit(u.source, u.bucket), u.slot) for u in run.start.units),
                started_at=run.start.started_at,
                started_server=run.started_server,
                outcome=None if run.end is None else RunOutcome(run.end.status.value),
            )
            for run in self.runs
            if run.start is not None
            and run.started_server is not None
            and not (run.end is not None and run.end.status is RunEndStatus.SKIPPED)
        )

    def lines(self) -> Iterator[tuple[RunSummary, FetchLine]]:
        for run in self.runs:
            for line in run.lines:
                yield run, line


async def discover_prefixes(store: RawStore) -> tuple[str, ...]:
    """Every run prefix in the store: sources, then `_host/<fqdn>` and `_job/<id>`."""
    found: list[str] = []
    for top in (await store.list(RUNS_ROOT, delimiter="/")).prefixes:
        name = top[len(RUNS_ROOT) : -1]
        if name in ("_host", "_job"):
            found += [
                p[len(RUNS_ROOT) : -1] for p in (await store.list(top, delimiter="/")).prefixes
            ]
        else:
            found.append(name)
    # A stray folder no run could have written is skipped, not a crash of every tick.
    return tuple(sorted(p for p in found if _is_prefix(p)))


def _is_prefix(name: str) -> bool:
    try:
        check_prefix(name)
    except ValueError:
        return False
    return True


async def load_manifests(
    store: RawStore, *, prefixes: Iterable[str] | None, days: Iterable[date]
) -> ManifestIndex:
    """Every run started on `days` (Belgrade dates) under `prefixes` (None: all of them).

    A part that can't be decoded (or was listed, then gone) is reported in `corrupt`, never
    raised: one bad object must not stop collection (NFR-064). Any other store error is
    raised: a store we can't read is a failed tick, not a missing run that would run again.
    """
    limiter = anyio.CapacityLimiter(_GET_CONCURRENCY)
    runs: list[RunSummary] = []
    corrupt: list[CorruptPart] = []
    for prefix in await discover_prefixes(store) if prefixes is None else tuple(prefixes):
        for day in days:
            day_runs, day_corrupt = await _load_day(store, prefix, day, limiter)
            runs += day_runs
            corrupt += day_corrupt
    return ManifestIndex(tuple(runs), tuple(sorted(corrupt, key=lambda c: c.key)))


async def _load_day(
    store: RawStore, prefix: str, day: date, limiter: anyio.CapacityLimiter
) -> tuple[list[RunSummary], list[CorruptPart]]:
    base = runs_day_prefix(prefix, day)
    entries: dict[str, list[tuple[int, str, datetime]]] = {}
    for obj in (await store.list(base)).objects:
        if m := _PART.fullmatch(obj.key[len(base) :]):
            entries.setdefault(m["run_id"], []).append((int(m["seq"]), obj.key, obj.last_modified))
    parts: dict[str, tuple[Line, ...]] = {}
    corrupt: list[CorruptPart] = []
    failed: list[StoreError] = []

    async def fetch(run_id: str, seq: int, key: str) -> None:
        async with limiter:
            try:
                lines = decode_part(await store.get(key))
                parts[key] = _checked_part(lines, prefix, run_id, seq)
            except (ObjectMissing, ManifestCorrupt) as err:
                corrupt.append(CorruptPart(key, str(err), prefix, day, run_id, seq))
            except StoreError as err:  # the first one stops the rest; raised plain, not grouped
                failed.append(err)
                tg.cancel_scope.cancel()

    async with anyio.create_task_group() as tg:
        for run_id, run_parts in entries.items():
            for seq, key, _ in run_parts:
                tg.start_soon(fetch, run_id, seq, key)
    if failed:
        raise failed[0]
    for run_id, run_parts in entries.items():
        corrupt += _after_run_end(prefix, day, run_id, sorted(run_parts), parts)
    runs = [
        _summarise(prefix, day, run_id, sorted(e), parts) for run_id, e in sorted(entries.items())
    ]
    return runs, corrupt


def _checked_part(lines: tuple[Line, ...], prefix: str, run_id: str, seq: int) -> tuple[Line, ...]:
    """The part's lines if they fit where the part sits; else the part is corrupt."""
    if any(line.run_id != run_id for line in lines):
        raise ManifestCorrupt(f"a line of another run is in run {run_id}")
    if seq == 0:
        if len(lines) != 1 or not isinstance(lines[0], RunStart):
            raise ManifestCorrupt("part 0000 holds the run_start alone")
        if lines[0].source != prefix:
            raise ManifestCorrupt(f"a run of {lines[0].source!r} filed under {prefix!r}")
    elif any(isinstance(line, RunStart) for line in lines):
        raise ManifestCorrupt("a run_start appears only in part 0000")
    if any(isinstance(line, RunEnd) for line in lines[:-1]):
        raise ManifestCorrupt("run_end is the last line")
    return lines


def _after_run_end(
    prefix: str,
    day: date,
    run_id: str,
    entries: list[tuple[int, str, datetime]],
    parts: dict[str, tuple[Line, ...]],
) -> list[CorruptPart]:
    """Parts after the one that ended the run are corrupt: taken out of `parts` (the ledger)
    and reported with their fetches (the budget)."""
    ended = False
    found = []
    for seq, key, _ in entries:
        if ended and key in parts:
            fetches = tuple(line for line in parts.pop(key) if isinstance(line, FetchLine))
            error = "a part after run_end"
            found.append(CorruptPart(key, error, prefix, day, run_id, seq, fetches))
        ended = ended or any(isinstance(line, RunEnd) for line in parts.get(key, ()))
    return found


def _summarise(
    prefix: str,
    day: date,
    run_id: str,
    entries: list[tuple[int, str, datetime]],
    parts: Mapping[str, tuple[Line, ...]],
) -> RunSummary:
    start: RunStart | None = None
    started_server: datetime | None = None
    end: RunEnd | None = None
    lines: list[FetchLine] = []
    for seq, key, modified in entries:
        for line in parts.get(key) or ():
            if isinstance(line, RunStart) and seq == 0:
                start, started_server = line, modified
            elif isinstance(line, FetchLine):
                lines.append(line)
            elif isinstance(line, RunEnd):
                end = line
    return RunSummary(run_id, prefix, day, start, started_server, end, tuple(lines))


class ManifestRunLedger:
    """The `RunLedger` port backed by the store's manifests (ADR-0004 S1).

    It reads the Belgrade days around `server_now` (D-1 to D+1): enough for every unit's
    latest slot and backoff, and for a runner whose clock runs ahead at midnight.
    """

    def __init__(
        self, store: RawStore, *, stale_after: timedelta, prefixes: Iterable[str] | None = None
    ) -> None:
        self._store = store
        self._stale_after = stale_after
        self._prefixes = None if prefixes is None else tuple(prefixes)

    async def snapshot(self, *, server_now: datetime) -> LedgerSnapshot:
        today = local_date(require_aware(server_now, "server_now"))
        days = [today + timedelta(days=k) for k in (-1, 0, 1)]
        index = await load_manifests(self._store, prefixes=self._prefixes, days=days)
        return fold(index.records(), server_now=server_now, stale_after=self._stale_after)
