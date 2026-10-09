"""`due()` (ADR-0004 S2, S3, S4, S6, S9; TS-004, TS-006): which units run, and when.

The simulations over whole nights and a year are in test_schedule_simulation.py.
"""

from datetime import UTC, datetime, time, timedelta, timezone

import pytest

from hopla.core.ledger import RunOutcome, RunRecord, Unit, UnitState, fold
from hopla.core.schedule import (
    BucketSpec,
    DuePlan,
    DueUnit,
    ScheduleConfig,
    Waiting,
    WaitReason,
    backoff,
    due,
    latest_slot,
    slots_on,
)
from tests.unit.schedule_sim import (
    CFG,
    DAWN,
    DAY,
    FALL_BACK,
    FAR,
    NIGHT,
    NORMAL_DAY,
    NOTICES,
    SPRING_FORWARD,
    Simulator,
    local,
    served_up_to,
    snapshot,
)


@pytest.mark.parametrize(
    ("bucket", "normal", "fall_back", "spring_forward"),
    [
        ("dawn", 1, 1, 1),
        ("day", 39, 39, 39),
        ("night", 4, 4, 3),  # 02:05 is in the gap: it moves to 03:05 and merges with it
        ("far", 6, 6, 6),  # 02:15 moves to 03:15 CEST
        ("notices", 40, 40, 39),  # 02:05 merges with 03:05
    ],
)
def test_slot_counts_per_day(bucket: str, normal: int, fall_back: int, spring_forward: int) -> None:
    spec = CFG.bucket(bucket)
    counts = [len(slots_on(spec, d)) for d in (NORMAL_DAY, FALL_BACK, SPRING_FORWARD)]

    assert counts == [normal, fall_back, spring_forward]


def test_dst_slots_land_where_the_rule_says() -> None:
    night, far = CFG.bucket("night"), CFG.bucket("far")

    # The fold: 02:05 and 02:15 exist once, at their first (summer-time) occurrence.
    assert datetime(2026, 10, 25, 0, 5, tzinfo=UTC) in slots_on(night, FALL_BACK)
    assert datetime(2026, 10, 25, 1, 5, tzinfo=UTC) not in slots_on(night, FALL_BACK)
    assert datetime(2026, 10, 25, 0, 15, tzinfo=UTC) in slots_on(far, FALL_BACK)
    # The gap: 02:15 runs at 03:15 CEST (01:15Z), not three hours later at 06:15.
    assert datetime(2027, 3, 28, 1, 15, tzinfo=UTC) in slots_on(far, SPRING_FORWARD)


def test_latest_slot_boundaries() -> None:
    day = CFG.bucket("day")
    slot = local(NORMAL_DAY, "10:35")

    assert latest_slot(day, slot) == slot  # a slot counts at its own instant
    assert latest_slot(day, slot - timedelta(microseconds=1)) == local(NORMAL_DAY, "10:05")
    # After midnight the latest day slot is yesterday's 23:35.
    assert latest_slot(day, local(NORMAL_DAY, "00:01")) == local(
        NORMAL_DAY - timedelta(days=1), "23:35"
    )


# --- S2, S3, S4: due or waiting --------------------------------------------------------------


def test_an_unserved_unit_is_due_for_its_latest_slot() -> None:
    now = local(NORMAL_DAY, "10:40")

    plan = due(now, snapshot({}, now), CFG)

    assert {(d.unit, d.slot) for d in plan.due} >= {(DAY, local(NORMAL_DAY, "10:35"))}


def test_each_wait_reason() -> None:
    now = local(NORMAL_DAY, "10:40")
    states = served_up_to(now) | {
        DAY: UnitState(in_progress_since=now),
        FAR: UnitState(n_failures=1, last_attempt_at=now - timedelta(minutes=5)),
    }

    plan = due(now, snapshot(states, now), CFG)
    reasons = {w.unit: w.reason for w in plan.waiting}

    assert plan.due == ()
    assert reasons[DAY] is WaitReason.IN_PROGRESS
    assert reasons[FAR] is WaitReason.BACKOFF
    assert reasons[DAWN] is reasons[NIGHT] is reasons[NOTICES] is WaitReason.SERVED


@pytest.mark.parametrize(
    ("n", "day_or_notices", "night", "far_or_dawn"),
    [
        (0, 0, 0, 0),
        (1, 30, 60, 240),
        (2, 40, 60, 240),
        (3, 80, 80, 240),
        (4, 160, 160, 240),
        (5, 240, 240, 240),
        (6, 240, 240, 240),
        (10_000, 240, 240, 240),
    ],
)
def test_backoff_table(n: int, day_or_notices: int, night: int, far_or_dawn: int) -> None:
    def minutes_for(bucket: str) -> float:
        spec = CFG.bucket(bucket)
        return backoff(n, spec.interval, CFG.backoff_base, CFG.backoff_cap) / timedelta(minutes=1)

    assert [minutes_for(b) for b in ("day", "notices", "night", "far", "dawn")] == [
        day_or_notices,
        day_or_notices,
        night,
        far_or_dawn,
        far_or_dawn,
    ]


def test_backoff_refuses_a_negative_count() -> None:
    with pytest.raises(ValueError):
        backoff(-1, timedelta(minutes=30), CFG.backoff_base, CFG.backoff_cap)


def test_a_served_unit_waits_as_served_even_with_a_run_in_progress() -> None:
    now = local(NORMAL_DAY, "10:40")
    states = served_up_to(now) | {
        DAY: UnitState(last_success_slot=local(NORMAL_DAY, "10:35"), in_progress_since=now)
    }

    plan = due(now, snapshot(states, now), CFG)

    assert next(w.reason for w in plan.waiting if w.unit == DAY) is WaitReason.SERVED


@pytest.mark.parametrize("skew", [timedelta(minutes=-10), timedelta(minutes=10)])
def test_backoff_runs_on_the_runner_clock(skew: timedelta) -> None:
    # S4/S8: the store's clock judges staleness only. Backoff counts from the runner's own
    # start time, the clock `now` comes from, so a skewed store doesn't move retry_at.
    runner = local(NORMAL_DAY, "10:35")
    records = [RunRecord("r1", ((DAY, runner),), runner, runner + skew, RunOutcome.FAILED)]
    ledger = fold(records, server_now=runner + timedelta(minutes=5), stale_after=CFG.stale_after)

    plan = due(runner + timedelta(minutes=5), ledger, CFG)

    assert ledger.state(DAY).last_attempt_at == runner
    assert next(w.retry_at for w in plan.waiting if w.unit == DAY) == runner + timedelta(minutes=30)


def test_a_failing_unit_waits_exactly_until_retry_at() -> None:
    attempt = local(NORMAL_DAY, "10:35")
    states = served_up_to(attempt) | {DAY: UnitState(n_failures=2, last_attempt_at=attempt)}
    retry_at = attempt + timedelta(minutes=40)

    before = due(retry_at - timedelta(seconds=1), snapshot(states, retry_at), CFG)
    at = due(retry_at, snapshot(states, retry_at), CFG)

    assert DAY in {w.unit for w in before.waiting if w.retry_at == retry_at}
    assert DAY in {d.unit for d in at.due}


def test_a_stale_unfinished_run_is_a_failure_that_backs_off_from_its_start() -> None:
    started = local(NORMAL_DAY, "10:35")
    records = [RunRecord("r1", ((DAY, started),), started, started, None)]
    now = started + timedelta(minutes=16)

    ledger = fold(records, server_now=now, stale_after=CFG.stale_after)
    plan = due(now, ledger, CFG)
    day_wait = next(w for w in plan.waiting if w.unit == DAY)

    assert (day_wait.reason, day_wait.retry_at) == (
        WaitReason.BACKOFF,
        started + timedelta(minutes=30),
    )


# --- S6: catch-up and double triggers -------------------------------------------------------


def test_after_an_outage_each_bucket_runs_once_for_its_latest_slot() -> None:
    sim = Simulator(local(NORMAL_DAY, "08:05"))

    plan = sim.tick(local(NORMAL_DAY, "14:20"))
    again = sim.tick(local(NORMAL_DAY, "14:21"))

    assert {(d.unit, d.slot) for d in plan.due} == {
        (DAY, local(NORMAL_DAY, "14:05")),
        (FAR, local(NORMAL_DAY, "14:15")),
        (NOTICES, local(NORMAL_DAY, "14:05")),
    }
    assert again.due == ()


def test_a_second_trigger_in_the_same_slot_does_nothing() -> None:
    sim = Simulator(local(NORMAL_DAY, "10:30"))
    first = sim.tick(local(NORMAL_DAY, "10:35"))

    second = sim.tick(local(NORMAL_DAY, "10:35") + timedelta(seconds=40))  # cron + dispatch

    assert DAY in {d.unit for d in first.due}
    assert second.due == ()


# --- dates ---------------------------------------------------------------------------------


def test_dates_come_from_now_not_from_the_slot() -> None:
    # The 23:35 slot caught up at 00:10 asks for the new today and tomorrow.
    now = local(NORMAL_DAY, "00:10")
    states = served_up_to(local(NORMAL_DAY - timedelta(days=1), "23:00"))

    plan = due(now, snapshot(states, now), CFG)
    day_run = next(d for d in plan.due if d.unit == DAY)

    assert day_run.slot == local(NORMAL_DAY - timedelta(days=1), "23:35")
    assert day_run.dates == (NORMAL_DAY, NORMAL_DAY + timedelta(days=1))


def test_far_dates_are_two_to_seven_days_ahead_and_notices_have_none() -> None:
    now = local(NORMAL_DAY, "14:15")
    plan = due(now, snapshot({}, now), CFG)
    dates = {d.unit: d.dates for d in plan.due}

    assert dates[FAR] == tuple(NORMAL_DAY + timedelta(days=k) for k in range(2, 8))
    assert dates[NOTICES] == ()


def test_dates_by_source_fetches_each_date_once() -> None:
    # Dawn and day both due at 04:35: one fetch per date for the source.
    now = local(NORMAL_DAY, "04:35")
    plan = due(now, snapshot(served_up_to(local(NORMAL_DAY, "04:10")), now), CFG)

    assert {d.unit for d in plan.due} == {DAWN, DAY}
    assert plan.dates_by_source() == {"srbijavoz": (NORMAL_DAY, NORMAL_DAY + timedelta(days=1))}


def test_now_in_any_zone_gives_the_same_plan() -> None:
    now = local(FALL_BACK, "02:30")
    state = snapshot(served_up_to(local(FALL_BACK, "01:00")), now)

    plans = [due(now.astimezone(z), state, CFG) for z in (UTC, timezone(timedelta(hours=5)))]

    assert plans[0].due == plans[1].due
    with pytest.raises(ValueError, match="timezone-aware"):
        due(now.replace(tzinfo=None), state, CFG)


# --- config validation ---------------------------------------------------------------------


def test_bucket_spec_validation() -> None:
    def bucket(times: tuple[time, ...], interval: timedelta = timedelta(minutes=30)) -> BucketSpec:
        return BucketSpec("b", times, (0,), interval)

    with pytest.raises(ValueError, match="no slot times"):
        bucket(())
    with pytest.raises(ValueError, match="sorted and unique"):
        bucket((time(5), time(4)))
    with pytest.raises(ValueError, match="sorted and unique"):
        bucket((time(4), time(4)))
    with pytest.raises(ValueError, match="whole minutes"):
        bucket((time(4, 0, 30),))
    with pytest.raises(ValueError, match="whole minutes"):
        bucket((time(4, 0, 0, 1),))
    with pytest.raises(ValueError, match="positive"):
        bucket((time(4),), timedelta(0))


def test_schedule_config_validation() -> None:
    spec = BucketSpec("day", (time(4),), (0,), timedelta(minutes=30))

    with pytest.raises(ValueError, match="unique"):
        ScheduleConfig(buckets=(spec, spec), units=())
    with pytest.raises(ValueError, match="unknown buckets"):
        ScheduleConfig(buckets=(spec,), units=(Unit("s", "night"),))
    with pytest.raises(ValueError, match="units must be unique"):
        ScheduleConfig(buckets=(spec,), units=(Unit("s", "day"), Unit("s", "day")))
    for bad in ({"stale_after": timedelta(0)}, {"backoff_base": -timedelta(minutes=1)}):
        with pytest.raises(ValueError, match="positive"):
            ScheduleConfig(buckets=(spec,), units=(), **bad)
    with pytest.raises(ValueError, match="at least backoff_base"):
        ScheduleConfig(
            buckets=(spec,), units=(), backoff_base=timedelta(hours=1), backoff_cap=timedelta(0, 60)
        )


def test_the_timing_defaults_are_adr_0004s() -> None:
    cfg = ScheduleConfig(buckets=(), units=())

    assert (cfg.backoff_base, cfg.backoff_cap, cfg.stale_after) == (
        timedelta(minutes=10),
        timedelta(hours=4),
        timedelta(minutes=15),
    )


def test_an_aware_slot_time_is_refused() -> None:
    with pytest.raises(ValueError, match="whole minutes"):
        BucketSpec("b", (time(4, tzinfo=UTC),), (0,), timedelta(minutes=30))


def test_backoff_names_the_bad_count() -> None:
    with pytest.raises(ValueError, match="n must be"):
        backoff(-1, timedelta(minutes=30), CFG.backoff_base, CFG.backoff_cap)


def test_in_progress_wins_over_backoff() -> None:
    # A failure, then a young run: fold builds this state; the unit waits for the run.
    now = local(NORMAL_DAY, "14:15")
    m = timedelta(minutes=1)
    state = UnitState(n_failures=1, last_attempt_at=now - m, in_progress_since=now - 2 * m)

    plan = due(now, snapshot({DAY: state}, now), CFG)

    assert Waiting(DAY, WaitReason.IN_PROGRESS) in plan.waiting


def test_due_units_follow_source_then_bucket_order() -> None:
    now = local(NORMAL_DAY, "14:15")

    plan = due(now, snapshot({}, now), CFG)

    assert [d.unit for d in plan.due] == [NOTICES, DAWN, DAY, NIGHT, FAR]
    assert plan.now.tzinfo is UTC


def test_dates_by_source_merges_sorts_and_orders_sources() -> None:
    now = local(NORMAL_DAY, "14:15")
    merged = due(now, snapshot({}, now), CFG).dates_by_source()
    days = tuple(NORMAL_DAY + timedelta(days=k) for k in range(40))
    units = (
        DueUnit(Unit("b", "day"), now, days[::-1]),
        DueUnit(Unit("a", "day"), now, ()),
    )

    by_source = DuePlan(now, units, ()).dates_by_source()

    assert merged["srbijavoz"] == tuple(NORMAL_DAY + timedelta(days=k) for k in range(8))
    assert list(by_source) == ["a", "b"]
    assert by_source["b"] == days
