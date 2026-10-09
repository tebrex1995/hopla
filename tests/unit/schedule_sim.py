"""Shared helpers for the schedule tests: the config, a tick simulator and an independent oracle."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hopla.config import Profile, Settings, parse_time
from hopla.core.ledger import LedgerSnapshot, RunOutcome, RunRecord, Unit, UnitState, fold
from hopla.core.schedule import DuePlan, due, latest_slot
from hopla.core.time import to_instant

CFG = Settings().schedule_config()  # conftest clears HOPLA_*, so these are the defaults
DAWN, DAY, NIGHT, FAR = (Unit("srbijavoz", b) for b in ("dawn", "day", "night", "far"))
NOTICES = Unit("notices_srbijavoz", "notices")
FALL_BACK, SPRING_FORWARD = date(2026, 10, 25), date(2027, 3, 28)
NORMAL_DAY = date(2026, 10, 13)


def local(day: date, hhmm: str) -> datetime:
    """The UTC instant of a Belgrade wall-clock time on `day`."""
    return to_instant(day, parse_time(hhmm))


def snapshot(states: dict[Unit, UnitState], server_now: datetime) -> LedgerSnapshot:
    return LedgerSnapshot(server_now=server_now, states=states)


def served_up_to(slot_time: datetime) -> dict[Unit, UnitState]:
    """Every unit served through its latest slot at `slot_time`."""
    return {
        u: UnitState(last_success_slot=latest_slot(CFG.bucket(u.bucket), slot_time))
        for u in CFG.units
    }


class Simulator:
    """Ticks `due()` and records every due unit as one instant run, like `tick` will.

    Per unit it keeps only the runs since its last success: an older run can't change the
    ledger (a later success resets the count, and the slot and attempt times only grow), so
    the fold stays small over a simulated year.
    """

    def __init__(self, start: datetime, *, outcome: RunOutcome = RunOutcome.COMPLETE) -> None:
        self.outcome = outcome
        self.served: list[tuple[Unit, datetime, datetime]] = []  # (unit, slot, ran at)
        self._runs: dict[Unit, list[RunRecord]] = {}
        self._count = 0
        for unit, state in served_up_to(start).items():
            assert state.last_success_slot is not None
            self._record(((unit, state.last_success_slot),), start, RunOutcome.COMPLETE)

    def _record(
        self, slots: tuple[tuple[Unit, datetime], ...], at: datetime, outcome: RunOutcome
    ) -> None:
        self._count += 1
        record = RunRecord(f"r{self._count:07d}", slots, at, at, outcome)
        for unit, _ in slots:
            kept = [] if outcome is RunOutcome.COMPLETE else self._runs.get(unit, [])
            self._runs[unit] = [*kept, record]

    def record_failure(self, unit: Unit, slot: datetime, at: datetime) -> None:
        self._record(((unit, slot),), at, RunOutcome.FAILED)

    def records(self) -> list[RunRecord]:
        unique = {r.run_id: r for runs in self._runs.values() for r in runs}
        return list(unique.values())

    def tick(self, now: datetime) -> DuePlan:
        ledger = fold(self.records(), server_now=now, stale_after=CFG.stale_after)
        plan = due(now, ledger, CFG)
        if plan.due:
            self._record(tuple((d.unit, d.slot) for d in plan.due), now, self.outcome)
            self.served += [(d.unit, d.slot, now) for d in plan.due]
        return plan


def all_slots(unit: Unit, start: datetime, end: datetime) -> list[datetime]:
    """The expected slots in (start, end], computed without `slots_on` or `core.time`.

    It reads the Belgrade wall clock of every UTC minute: a slot is the first minute showing
    its local time on its date (the fold's first occurrence), or, if that time never shows (the
    spring-forward gap), the first minute one hour later (the gap moves forward).

    Two assumptions, both checked: Belgrade's clock changes are exactly one hour, and a slot
    time that also equals "another slot time + 1 h" is only treated as gap-shifted when the
    other time really never showed (the `not in first_seen` check).
    """
    zone = ZoneInfo("Europe/Belgrade")
    times = set(CFG.bucket(unit.bucket).local_times)
    shifted = {(datetime.combine(date.min, t) + timedelta(hours=1)).time(): t for t in times}
    first_seen: dict[tuple[date, time], datetime] = {}
    offsets = set()
    now = start.replace(second=0, microsecond=0) - timedelta(days=1)
    while now <= end + timedelta(days=1):
        local_now = now.astimezone(zone)
        offsets.add(local_now.utcoffset())
        wall = local_now.time().replace(fold=0)
        if wall in times or wall in shifted:
            first_seen.setdefault((local_now.date(), wall), now)
        now += timedelta(minutes=1)
    assert offsets <= {timedelta(hours=1), timedelta(hours=2)}, "the oracle assumes 1 h changes"
    slots = set()
    for (day, wall), instant in first_seen.items():
        if wall in times:
            slots.add(instant)
        if wall in shifted and (day, shifted[wall]) not in first_seen:
            slots.add(instant)  # its time fell in the gap
    return sorted(s for s in slots if start < s <= end)


def minutes(start: datetime, end: datetime) -> Iterator[datetime]:
    now = start
    while now <= end:
        yield now
        now += timedelta(minutes=1)


def cron_times(expression: str, start: datetime, end: datetime) -> Iterator[datetime]:
    """UTC instants in (start, end] of a `minute hour * * *` cron (the GitHub schedule)."""

    def field(spec: str, top: int) -> list[int]:
        if spec == "*":
            return list(range(top))
        values: list[int] = []
        for part in spec.split(","):
            low, _, high = part.partition("-")
            values += range(int(low), int(high or low) + 1)
        return values

    minute_spec, hour_spec, *_ = expression.split()
    day = start.date()
    while day <= end.date():
        for h in field(hour_spec, 24):
            for m in field(minute_spec, 60):
                at = datetime(day.year, day.month, day.day, h, m, tzinfo=UTC)
                if start < at <= end:
                    yield at
        day += timedelta(days=1)


def run_profile(profile: Profile, start: datetime, end: datetime) -> Simulator:
    """Ticks at every cron time of `profile` between start and end."""
    sim = Simulator(start)
    crons = Settings(profile=profile).crons()
    for now in sorted({t for cron in crons for t in cron_times(cron, start, end)}):
        sim.tick(now)
    return sim
