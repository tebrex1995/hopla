"""The clocks: the fake used by every time-driven test, and the real one (04 §6)."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from hopla.core.clock import Clock
from hopla.system_clock import SystemClock
from tests.fakes.clock import FakeClock

START = datetime(2026, 10, 9, 11, 0, tzinfo=UTC)


def test_the_fake_starts_where_it_is_told_and_reports_utc() -> None:
    clock = FakeClock(START.astimezone(timezone(timedelta(hours=2))))

    assert clock.now() == START
    assert clock.now().tzinfo is UTC


def test_the_fake_refuses_a_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 10, 9, 11, 0))


def test_advance_moves_forward_only() -> None:
    clock = FakeClock(START)

    assert clock.advance(minutes=30, seconds=5) == START + timedelta(minutes=30, seconds=5)
    with pytest.raises(ValueError, match="forward"):
        clock.advance(minutes=-1)


def test_set_may_jump_back_for_skew_tests() -> None:
    clock = FakeClock(START)

    assert clock.set(START - timedelta(minutes=10)) == START - timedelta(minutes=10)


def test_set_local_picks_either_occurrence_on_the_fall_back_night() -> None:
    clock = FakeClock(START)

    first = clock.set_local("02:05", on=date(2026, 10, 25))
    second = clock.set_local("02:05", on=date(2026, 10, 25), fold=1)

    assert second - first == timedelta(hours=1)
    assert first == datetime(2026, 10, 25, 0, 5, tzinfo=UTC)


def test_set_local_refuses_a_time_the_spring_forward_skips() -> None:
    with pytest.raises(ValueError, match="gap"):
        FakeClock(START).set_local("02:30", on=date(2027, 3, 28))


def test_both_clocks_satisfy_the_port() -> None:
    clocks: list[Clock] = [FakeClock(START), SystemClock()]  # mypy checks the protocol

    assert all(c.now().tzinfo is UTC for c in clocks)


def test_a_zero_advance_keeps_the_time() -> None:
    assert FakeClock(START).advance() == START


def test_set_normalises_to_utc_and_refuses_naive() -> None:
    clock = FakeClock(START)

    assert clock.set(START.astimezone(timezone(timedelta(hours=2)))).tzinfo is UTC
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(START.replace(tzinfo=None))
