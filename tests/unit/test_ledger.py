"""The run ledger fold (ADR-0004 S1 shape, S4): what each run means for each unit it served."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from hopla.core.ledger import LedgerSnapshot, RunOutcome, RunRecord, Unit, UnitState, fold
from tests.fakes.ledger import InMemoryRunLedger

DAY = Unit("srbijavoz", "day")
FAR = Unit("srbijavoz", "far")
T0 = datetime(2026, 10, 9, 10, 35, tzinfo=UTC)
STALE = timedelta(minutes=15)


def run(
    run_id: str,
    outcome: RunOutcome | None,
    *,
    at: datetime = T0,
    server: datetime | None = None,
    slots: tuple[tuple[Unit, datetime], ...] = ((DAY, T0),),
) -> RunRecord:
    return RunRecord(run_id, slots, at, at if server is None else server, outcome)


def state_after(*records: RunRecord, server_now: datetime = T0 + timedelta(hours=1)) -> UnitState:
    return fold(records, server_now=server_now, stale_after=STALE).state(DAY)


def test_an_unknown_unit_has_the_initial_state() -> None:
    assert fold([], server_now=T0, stale_after=STALE).state(DAY) == UnitState()


def test_a_complete_run_is_a_success() -> None:
    state = state_after(run("r1", RunOutcome.COMPLETE))

    assert state == UnitState(
        last_success_slot=T0, last_success_at=T0, last_attempt_at=T0, n_failures=0
    )


@pytest.mark.parametrize("outcome", [RunOutcome.FAILED, RunOutcome.PARTIAL])
def test_a_failed_or_partial_run_counts_one_failure(outcome: RunOutcome) -> None:
    state = state_after(run("r1", outcome))

    assert (state.n_failures, state.last_success_slot, state.last_attempt_at) == (1, None, T0)


def test_failures_accumulate_and_a_success_resets_them() -> None:
    later = [T0 + timedelta(minutes=30 * i) for i in range(4)]
    failing = [run(f"r{i}", RunOutcome.FAILED, at=t) for i, t in enumerate(later[:3])]

    assert state_after(*failing).n_failures == 3
    assert state_after(*failing, run("r9", RunOutcome.COMPLETE, at=later[3])).n_failures == 0


@pytest.mark.parametrize(
    ("age", "in_progress", "failures"),
    [
        (timedelta(minutes=14, seconds=59), True, 0),  # younger than 15 min: in progress
        (timedelta(minutes=15), False, 1),  # S4: 15 min without run_end is a failure
        (timedelta(minutes=-2), True, 0),  # the store saw it "in the future": still young
    ],
)
def test_an_unfinished_run_is_in_progress_until_stale(
    age: timedelta, in_progress: bool, failures: int
) -> None:
    state = state_after(run("r1", None, server=T0), server_now=T0 + age)

    assert (state.in_progress_since is not None, state.n_failures) == (in_progress, failures)


@pytest.mark.parametrize("skew", [timedelta(minutes=-10), timedelta(0), timedelta(minutes=10)])
def test_staleness_is_judged_on_store_time_only(skew: timedelta) -> None:
    # The runner's clock is off by ±10 min; the verdict doesn't move (S4, S8).
    young = run("r1", None, at=T0 + skew, server=T0)
    stale = state_after(young, server_now=T0 + timedelta(minutes=15))

    assert state_after(young, server_now=T0 + timedelta(minutes=14)).in_progress_since == T0
    assert stale.n_failures == 1
    assert stale.last_attempt_at == T0 + skew  # attempts are runner time: backoff uses `now`


def test_success_times_are_runner_time_too() -> None:
    state = state_after(run("r1", RunOutcome.COMPLETE, at=T0, server=T0 + timedelta(minutes=10)))

    assert (state.last_success_at, state.last_attempt_at) == (T0, T0)


def test_a_young_run_does_not_count_as_an_attempt() -> None:
    state = state_after(run("r1", None), server_now=T0 + timedelta(minutes=5))

    assert state.last_attempt_at is None


def test_a_later_finished_run_clears_an_earlier_unfinished_one() -> None:
    open_run = run("r1", None, server=T0)
    done = run(
        "r2", RunOutcome.COMPLETE, at=T0 + timedelta(minutes=1), server=T0 + timedelta(minutes=1)
    )

    state = state_after(open_run, done, server_now=T0 + timedelta(minutes=2))

    assert state.in_progress_since is None
    assert state.last_success_slot == T0


def test_runs_apply_in_store_order_then_run_id() -> None:
    # Listed out of order; the store says the failure came last, so the unit is failing.
    success = run("b", RunOutcome.COMPLETE, server=T0)
    failure = run("a", RunOutcome.FAILED, server=T0 + timedelta(minutes=30))
    tie_failure = run("a", RunOutcome.FAILED, server=T0)
    tie_success = run("b", RunOutcome.COMPLETE, server=T0)

    assert state_after(failure, success).n_failures == 1
    assert state_after(tie_success, tie_failure).n_failures == 0  # same time: run_id "b" last


def test_the_served_slot_never_goes_back() -> None:
    newer = run("r2", RunOutcome.COMPLETE, slots=((DAY, T0),), server=T0)
    older = run(
        "r1",
        RunOutcome.COMPLETE,
        slots=((DAY, T0 - timedelta(hours=1)),),
        server=T0 + timedelta(minutes=1),
    )

    assert state_after(newer, older).last_success_slot == T0


def test_a_run_serving_several_units_updates_each() -> None:
    both = run("r1", RunOutcome.COMPLETE, slots=((DAY, T0), (FAR, T0 - timedelta(minutes=20))))

    snapshot = fold([both], server_now=T0, stale_after=STALE)

    assert snapshot.state(DAY).last_success_slot == T0
    assert snapshot.state(FAR).last_success_slot == T0 - timedelta(minutes=20)


async def test_the_fake_ledger_folds_what_it_recorded() -> None:
    ledger = InMemoryRunLedger(stale_after=STALE)
    ledger.begin("r1", ((DAY, T0),), started_at=T0)
    ledger.end("r1", RunOutcome.FAILED)
    ledger.begin("r2", ((DAY, T0),), started_at=T0 + timedelta(minutes=30))

    snapshot = await ledger.snapshot(server_now=T0 + timedelta(minutes=35))

    assert snapshot == fold(
        ledger.records(), server_now=T0 + timedelta(minutes=35), stale_after=STALE
    )
    assert snapshot.state(DAY).in_progress_since == T0 + timedelta(minutes=30)
    assert snapshot.state(DAY).n_failures == 1


def test_the_fake_ledger_refuses_a_double_start_or_end() -> None:
    ledger = InMemoryRunLedger()
    ledger.begin("r1", ((DAY, T0),), started_at=T0)
    ledger.end("r1", RunOutcome.COMPLETE)

    with pytest.raises(ValueError, match="already started"):
        ledger.begin("r1", ((DAY, T0),), started_at=T0)
    with pytest.raises(ValueError, match="already ended"):
        ledger.end("r1", RunOutcome.FAILED)


def test_run_times_become_utc_and_naive_ones_are_refused() -> None:
    plus_two = T0.astimezone(timezone(timedelta(hours=2)))
    record = run("r1", RunOutcome.COMPLETE, at=plus_two, slots=((DAY, plus_two),))

    assert record.started_at.tzinfo is UTC and record.slots[0][1].tzinfo is UTC
    with pytest.raises(ValueError, match="timezone-aware"):
        run("r2", RunOutcome.COMPLETE, at=T0.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        fold([], server_now=T0.replace(tzinfo=None), stale_after=STALE)


def test_a_run_may_not_list_a_unit_twice() -> None:
    with pytest.raises(ValueError, match="twice"):
        run("r1", RunOutcome.COMPLETE, slots=((DAY, T0), (DAY, T0)))


def test_unit_state_invariants() -> None:
    with pytest.raises(ValueError, match="negative"):
        UnitState(n_failures=-1, last_attempt_at=T0)
    with pytest.raises(ValueError, match="last attempt"):
        UnitState(n_failures=1)


def test_the_fake_ledger_refuses_to_end_an_unknown_run() -> None:
    with pytest.raises(ValueError, match="never started"):
        InMemoryRunLedger().end("r9", RunOutcome.COMPLETE)


def test_an_empty_snapshot_returns_initial_states() -> None:
    assert LedgerSnapshot(server_now=T0).state(FAR) == UnitState()


@pytest.mark.parametrize(
    ("outcome", "field"),
    [(RunOutcome.FAILED, "last_attempt_at"), (RunOutcome.COMPLETE, "last_success_at")],
)
def test_runner_times_keep_the_latest_whatever_the_store_order(
    outcome: RunOutcome, field: str
) -> None:
    # The store saw run a first, but the runner started it later: the times keep the latest.
    m = timedelta(minutes=1)
    a = RunRecord("a", ((DAY, T0),), T0 + 30 * m, T0, outcome)
    b = RunRecord("b", ((DAY, T0),), T0 + m, T0 + 5 * m, outcome)

    state = fold([a, b], server_now=T0 + 60 * m, stale_after=STALE).state(DAY)

    assert getattr(state, field) == T0 + 30 * m


async def test_the_fake_ledger_judges_staleness_on_server_time() -> None:
    ledger = InMemoryRunLedger()
    ledger.begin("r", ((DAY, T0),), started_at=T0, started_server=T0 - timedelta(minutes=20))

    state = (await ledger.snapshot(server_now=T0)).state(DAY)

    assert (state.n_failures, state.in_progress_since) == (1, None)


@pytest.mark.parametrize(
    ("stale_after", "age", "in_progress"),
    [(None, timedelta(minutes=12), True), (timedelta(minutes=5), timedelta(minutes=10), False)],
    ids=["default-15-min", "configured-5-min"],
)
async def test_the_fake_ledger_uses_its_stale_after(
    stale_after: timedelta | None, age: timedelta, in_progress: bool
) -> None:
    ledger = InMemoryRunLedger() if stale_after is None else InMemoryRunLedger(stale_after)
    ledger.begin("r", ((DAY, T0),), started_at=T0)

    state = (await ledger.snapshot(server_now=T0 + age)).state(DAY)

    assert (state.in_progress_since is not None) is in_progress
