"""Schedule simulations (ADR-0004 S3, S6, S9; TS-004): `due()` ticked like `tick` will.

Each test ticks `due()` and records every due unit's run. The DST-night and year tests check the
runs against `schedule_sim.all_slots`, an oracle that doesn't use `slots_on` or `core.time`; the
failure tests check hand-computed attempt lists.
"""

from collections import Counter
from datetime import UTC, date, datetime, timedelta

import pytest

from hopla.config import Profile
from hopla.core.ledger import RunOutcome
from hopla.core.schedule import latest_slot
from hopla.core.time import local_date
from tests.unit.schedule_sim import (
    CFG,
    DAWN,
    DAY,
    FALL_BACK,
    NORMAL_DAY,
    SPRING_FORWARD,
    Simulator,
    all_slots,
    local,
    minutes,
    run_profile,
)


def test_a_failed_dawn_that_then_succeeds_runs_twice_that_day() -> None:
    # A dawn that failed at 20:07Z backs off until 00:07Z. The first dawn trigger catches up
    # yesterday's slot; the second still runs today's 04:18 (only the two dawn triggers tick).
    day = date(2027, 1, 15)
    failed_at = datetime(2027, 1, 14, 20, 7, tzinfo=UTC)
    sim = Simulator(datetime(2027, 1, 14, 3, 0, tzinfo=UTC))  # before that day's dawn
    sim.record_failure(DAWN, local(day - timedelta(days=1), "04:18"), failed_at)

    for trigger in (
        datetime(2027, 1, 15, 2, 18, tzinfo=UTC),
        datetime(2027, 1, 15, 3, 18, tzinfo=UTC),
    ):
        sim.tick(trigger)
    dawn_runs = [slot for u, slot, _ in sim.served if u == DAWN]

    assert dawn_runs == [local(day - timedelta(days=1), "04:18"), local(day, "04:18")]


def test_a_day_unit_failing_for_24_hours_is_tried_only_when_backoff_allows() -> None:
    # S3: never on every tick. Ticks every minute; every run fails.
    start = local(NORMAL_DAY, "08:05")
    sim = Simulator(start - timedelta(minutes=1), outcome=RunOutcome.FAILED)

    for now in minutes(start, start + timedelta(hours=24) - timedelta(minutes=1)):
        sim.tick(now)
    attempts = [ran.astimezone(UTC) for unit, _, ran in sim.served if unit == DAY]

    expected = ["08:05", "08:35", "09:15", "10:35", "13:15", "17:15", "21:15"]
    next_day = ["01:15", "05:15"]
    assert attempts == [local(NORMAL_DAY, t) for t in expected] + [
        local(NORMAL_DAY + timedelta(days=1), t) for t in next_day
    ]


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 7, 1),
        date(2026, 10, 24),
        FALL_BACK,
        date(2027, 1, 15),
        date(2027, 3, 27),
        SPRING_FORWARD,
    ],
)
def test_exactly_one_of_the_two_dawn_crons_runs_dawn(day: date) -> None:
    sim = Simulator(local(day - timedelta(days=1), "05:00"))
    triggers = [datetime(day.year, day.month, day.day, h, 18, tzinfo=UTC) for h in (2, 3)]

    plans = [sim.tick(t) for t in triggers]

    assert [DAWN in {d.unit for d in p.due} for p in plans].count(True) == 1


@pytest.mark.parametrize("night", [FALL_BACK, SPRING_FORWARD], ids=["fall-back", "spring-forward"])
def test_dst_nights_minute_by_minute_run_every_slot_once(night: date) -> None:
    start, end = (
        local(night - timedelta(days=1), "12:00"),
        local(night + timedelta(days=1), "12:00"),
    )
    sim = Simulator(start)

    for now in minutes(start + timedelta(minutes=1), end):
        sim.tick(now)

    for unit in CFG.units:
        served = [slot for u, slot, _ in sim.served if u == unit]
        assert served == all_slots(unit, start, end), unit


def test_a_year_of_the_public_profile_runs_every_slot_once_on_time() -> None:
    # Every Belgrade slot is a cron minute (:05, :15, :35, and 04:18 via 02:18/03:18 UTC),
    # so each slot runs at its own instant, across both DST changes.
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2027, 10, 1, tzinfo=UTC)

    sim = run_profile(Profile.PUBLIC, start, end)
    totals = Counter(u.bucket for u, _, _ in sim.served)

    # 365 Belgrade days; the spring-forward night merges one night and one notices slot.
    assert totals == {
        "dawn": 365 * 1,
        "day": 365 * 39,
        "night": 365 * 4 - 1,
        "far": 365 * 6,
        "notices": 365 * 40 - 1,
    }
    for unit in CFG.units:
        runs = [(slot, ran) for u, slot, ran in sim.served if u == unit]
        assert [slot for slot, _ in runs] == all_slots(unit, start, end), unit
        assert all(slot == ran for slot, ran in runs), unit


def test_the_private_profile_never_runs_a_slot_twice() -> None:
    start, end = datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC)

    sim = run_profile(Profile.PRIVATE, start, end)
    counts = Counter((u, slot) for u, slot, _ in sim.served)
    dawn_days = Counter(local_date(slot) for u, slot, _ in sim.served if u == DAWN)

    assert max(counts.values()) == 1
    assert set(dawn_days.values()) == {1}
    # Each run serves the latest slot at its tick: never a stale one.
    assert all(slot == latest_slot(CFG.bucket(u.bucket), ran) for u, slot, ran in sim.served)
