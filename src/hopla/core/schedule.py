"""What to fetch now: the pure `due(now, ledger, cfg)` (ADR-0004 S2, S3, S4, S6, S9).

A trigger only says "look now"; this decides which units run. Each bucket has local slot
times in Belgrade (02 §6.3). A slot is a UTC instant, and a unit is **served** for a slot once
a complete run recorded that slot or a later one. So:
- after an outage each bucket runs once, for its latest slot (S6), and older slots are dropped;
- a second trigger in the same slot finds the unit served and does nothing (S6);
- two UTC dawn crons need no special code: in normal operation only one of them finds the 04:18
  slot unserved (S9). After one failed dawn that then succeeds, the earlier trigger may catch up
  yesterday's slot and the later one still runs today's: two dawn runs that day. A dawn that keeps
  failing is retried by its backoff like any unit (S3).

Slots on daylight-saving nights follow `core.time`: a gap time moves forward (and merges with
an equal slot), a fold time runs once, at its first occurrence. Query dates come from the
Belgrade date of `now`, not of the slot, so a run caught up after midnight asks for the new day.
"""

import functools
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from types import MappingProxyType

from hopla.core.ledger import LedgerSnapshot, Unit, UnitState
from hopla.core.time import local_date, require_aware, to_instant

_MAX_DOUBLINGS = 16  # 10 min × 2^16 is far above any cap; stops timedelta overflow at huge n


@dataclass(frozen=True)
class BucketSpec:
    name: str
    local_times: tuple[time, ...]  # Belgrade wall-clock slot times
    date_offsets: tuple[int, ...]  # travel dates as days from today; () for notices
    interval: timedelta  # S3 `bucket_interval`

    def __post_init__(self) -> None:
        if not self.local_times:
            raise ValueError(f"bucket {self.name!r} has no slot times")
        if list(self.local_times) != sorted(set(self.local_times)):
            raise ValueError(f"bucket {self.name!r}: slot times must be sorted and unique")
        if any(t.tzinfo is not None or t.second or t.microsecond for t in self.local_times):
            raise ValueError(f"bucket {self.name!r}: slot times are naive whole minutes")
        if self.interval <= timedelta(0):
            raise ValueError(f"bucket {self.name!r}: interval must be positive")


@dataclass(frozen=True)
class ScheduleConfig:
    buckets: tuple[BucketSpec, ...]
    units: tuple[Unit, ...]
    backoff_base: timedelta = timedelta(minutes=10)
    backoff_cap: timedelta = timedelta(hours=4)
    stale_after: timedelta = timedelta(minutes=15)  # timeout-minutes 10 + 5 (S4)

    def __post_init__(self) -> None:
        names = [b.name for b in self.buckets]
        if len(set(names)) != len(names):
            raise ValueError(f"bucket names must be unique: {names}")
        if unknown := sorted({u.bucket for u in self.units} - set(names)):
            raise ValueError(f"units name unknown buckets: {unknown}")
        if len(set(self.units)) != len(self.units):
            raise ValueError("units must be unique")
        if min(self.backoff_base, self.backoff_cap, self.stale_after) <= timedelta(0):
            raise ValueError("backoff_base, backoff_cap and stale_after must be positive")
        if self.backoff_cap < self.backoff_base:
            raise ValueError("backoff_cap must be at least backoff_base")

    def bucket(self, name: str) -> BucketSpec:
        return next(b for b in self.buckets if b.name == name)


class WaitReason(StrEnum):
    SERVED = "served"  # its latest slot is already done
    IN_PROGRESS = "in_progress"  # an unfinished run younger than stale_after holds it (S4)
    BACKOFF = "backoff"  # failing; waits until retry_at (S3)


@dataclass(frozen=True)
class DueUnit:
    unit: Unit
    slot: datetime
    dates: tuple[date, ...]


@dataclass(frozen=True)
class Waiting:
    unit: Unit
    reason: WaitReason
    retry_at: datetime | None = None


@dataclass(frozen=True)
class DuePlan:
    now: datetime
    due: tuple[DueUnit, ...]
    waiting: tuple[Waiting, ...]

    def dates_by_source(self) -> Mapping[str, tuple[date, ...]]:
        """Each source's travel dates over all its due units, so a date is fetched once per run."""
        merged: dict[str, set[date]] = {}
        for item in self.due:
            merged.setdefault(item.unit.source_id, set()).update(item.dates)
        return MappingProxyType({source: tuple(sorted(d)) for source, d in sorted(merged.items())})


def backoff(n: int, interval: timedelta, base: timedelta, cap: timedelta) -> timedelta:
    """S3: 0 after a success, else min(max(interval, base × 2^n), cap)."""
    if n < 0:
        raise ValueError(f"n must be ≥ 0, got {n}")
    if n == 0:
        return timedelta(0)
    return min(max(interval, base * (1 << min(n, _MAX_DOUBLINGS))), cap)


@functools.lru_cache(maxsize=4096)
def slots_on(spec: BucketSpec, day: date) -> tuple[datetime, ...]:
    """The bucket's slots on a Belgrade date, as sorted unique UTC instants."""
    return tuple(sorted({to_instant(day, t) for t in spec.local_times}))


def latest_slot(spec: BucketSpec, now: datetime) -> datetime:
    """The newest slot at or before `now`. A bucket has a slot every day, so yesterday has one."""
    now = require_aware(now, "now")
    today = local_date(now)
    candidates = slots_on(spec, today - timedelta(days=1)) + slots_on(spec, today)
    return max(slot for slot in candidates if slot <= now)


def due(now: datetime, ledger: LedgerSnapshot, cfg: ScheduleConfig) -> DuePlan:
    """The units to run now, and why every other unit waits."""
    now = require_aware(now, "now")
    today = local_date(now)
    due_units: list[DueUnit] = []
    waiting: list[Waiting] = []
    order = {b.name: i for i, b in enumerate(cfg.buckets)}
    for unit in sorted(cfg.units, key=lambda u: (u.source_id, order[u.bucket])):
        spec = cfg.bucket(unit.bucket)
        slot = latest_slot(spec, now)
        state = ledger.state(unit)
        retry_at = _retry_at(state, spec, cfg)
        if state.last_success_slot is not None and state.last_success_slot >= slot:
            waiting.append(Waiting(unit, WaitReason.SERVED))
        elif state.in_progress_since is not None:
            waiting.append(Waiting(unit, WaitReason.IN_PROGRESS))
        elif retry_at is not None and now < retry_at:
            waiting.append(Waiting(unit, WaitReason.BACKOFF, retry_at))
        else:
            dates = tuple(today + timedelta(days=k) for k in spec.date_offsets)
            due_units.append(DueUnit(unit, slot, dates))
    return DuePlan(now=now, due=tuple(due_units), waiting=tuple(waiting))


def _retry_at(state: UnitState, spec: BucketSpec, cfg: ScheduleConfig) -> datetime | None:
    """When a failing unit may run again (S2/S3), or None if it isn't failing."""
    if not state.n_failures:
        return None
    assert state.last_attempt_at is not None  # UnitState guarantees it
    return state.last_attempt_at + backoff(
        state.n_failures, spec.interval, cfg.backoff_base, cfg.backoff_cap
    )
