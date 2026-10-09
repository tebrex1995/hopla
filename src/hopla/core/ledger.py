"""The run ledger: per unit, the last success, the last attempt and the failure count (ADR-0004 S1).

A unit is one source × one bucket (dawn, day, night, far, notices). Runs are recorded in the
store's manifests (ADR-0003); `fold` turns those run records into the ledger. It's pure, so the
manifest reader (T-R0-03) and the in-memory fake give the same ledger from the same records.

Time bases are kept apart on purpose (S4, S8): `started_at` is the runner's clock, used for
backoff; `started_server` is the store's LastModified of the run's first part, used only to
judge whether an unfinished run is still in progress. The runner clock never judges staleness.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from hopla.core.time import require_aware


@dataclass(frozen=True, order=True)
class Unit:
    source_id: str
    bucket: str


class RunOutcome(StrEnum):
    """The `run_end` status of a run (ADR-0003). A run without one has no outcome yet."""

    COMPLETE = "complete"
    PARTIAL = "partial"  # some query failed: counts as a failure (S4)
    FAILED = "failed"


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    slots: tuple[tuple[Unit, datetime], ...]  # the (unit, UTC slot) pairs this run serves
    started_at: datetime  # runner clock
    started_server: datetime  # store LastModified of the run's first part
    outcome: RunOutcome | None  # None: no run_end (in progress, or died)

    def __post_init__(self) -> None:
        units = [unit for unit, _ in self.slots]
        if len(set(units)) != len(units):
            raise ValueError(f"run {self.run_id} lists a unit twice")
        # Manifests are read at this boundary: every time becomes aware UTC or is refused.
        aware = tuple((unit, require_aware(slot, "slot")) for unit, slot in self.slots)
        object.__setattr__(self, "slots", aware)
        object.__setattr__(self, "started_at", require_aware(self.started_at, "started_at"))
        server = require_aware(self.started_server, "started_server")
        object.__setattr__(self, "started_server", server)


@dataclass(frozen=True)
class UnitState:
    last_success_slot: datetime | None = None  # the newest slot a complete run served
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None  # runner time of the newest counted run
    n_failures: int = 0  # consecutive failed, partial or stale runs since the last success
    in_progress_since: datetime | None = None  # server time of an unfinished young run

    def __post_init__(self) -> None:
        if self.n_failures < 0:
            raise ValueError("n_failures can't be negative")
        if self.n_failures and self.last_attempt_at is None:
            raise ValueError("a failing unit has a last attempt")


@dataclass(frozen=True)
class LedgerSnapshot:
    server_now: datetime
    states: Mapping[Unit, UnitState] = field(default_factory=lambda: MappingProxyType({}))

    def state(self, unit: Unit) -> UnitState:
        return self.states.get(unit, UnitState())


def fold(
    records: Iterable[RunRecord], *, server_now: datetime, stale_after: timedelta
) -> LedgerSnapshot:
    """The ledger after `records`, as the store saw it at `server_now`.

    Runs apply in store order (server time, then run id; S8). For each unit a run serves:
    complete → success; partial or failed → one more failure; no outcome → in progress while
    younger than `stale_after`, a failure after that (S4).
    """
    server_now = require_aware(server_now, "server_now")
    states: dict[Unit, UnitState] = {}
    for record in sorted(records, key=lambda r: (r.started_server, r.run_id)):
        for unit, slot in record.slots:
            states[unit] = _apply(
                states.get(unit, UnitState()), record, slot, server_now, stale_after
            )
    return LedgerSnapshot(server_now=server_now, states=MappingProxyType(states))


def _apply(
    state: UnitState,
    record: RunRecord,
    slot: datetime,
    server_now: datetime,
    stale_after: timedelta,
) -> UnitState:
    if record.outcome is None and server_now - record.started_server < stale_after:
        return replace(state, in_progress_since=record.started_server)
    attempted = replace(
        state,
        in_progress_since=None,
        last_attempt_at=_latest(state.last_attempt_at, record.started_at),
    )
    if record.outcome is RunOutcome.COMPLETE:
        return replace(
            attempted,
            n_failures=0,
            last_success_slot=_latest(state.last_success_slot, slot),
            last_success_at=_latest(state.last_success_at, record.started_at),
        )
    return replace(attempted, n_failures=state.n_failures + 1)


def _latest(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None else max(current, candidate)
