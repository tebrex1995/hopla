"""Belgrade time (TS-004, PR-12, NFR-044): conversions across midnight and both DST changes."""

import contextlib
import io
import zoneinfo
from datetime import UTC, date, datetime, time, timedelta
from importlib.resources import files
from pathlib import Path

import pytest

from hopla.core.time import (
    TZ_NAME,
    LocalKind,
    belgrade,
    classify_local,
    local_date,
    require_aware,
    to_instant,
    to_local,
)
from hopla.pipeline import cli

FALL_BACK = date(2026, 10, 25)  # 03:00 CEST → 02:00 CET at 01:00Z: 02:00-02:59 happens twice
SPRING_FORWARD = date(2027, 3, 28)  # 02:00 CET → 03:00 CEST at 01:00Z: 02:00-02:59 never happens


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def test_tz_search_path_is_empty() -> None:
    assert zoneinfo.TZPATH == ()


def test_belgrade_comes_from_the_tzdata_package_even_after_an_os_load(tmp_path: Path) -> None:
    # A fake OS zoneinfo whose "Belgrade" is Tokyo (+09:00), loaded into ZoneInfo's shared cache
    # before the CLI empties the search path: belgrade() must still read the pinned tzdata.
    fake = tmp_path / "Europe" / "Belgrade"
    fake.parent.mkdir()
    fake.write_bytes((files("tzdata.zoneinfo") / "Asia" / "Tokyo").read_bytes())
    summer = datetime(2026, 7, 1, tzinfo=UTC)
    try:
        zoneinfo.reset_tzpath(to=(str(tmp_path),))
        zoneinfo.ZoneInfo.clear_cache()  # so the fake, not an earlier test's load, is cached
        assert summer.astimezone(zoneinfo.ZoneInfo(TZ_NAME)).utcoffset() == timedelta(hours=9)
        belgrade.cache_clear()

        with contextlib.redirect_stdout(io.StringIO()):  # main() prints the help
            cli.main([])

        assert summer.astimezone(belgrade()).utcoffset() == timedelta(hours=2)
        assert belgrade().key == TZ_NAME
    finally:
        zoneinfo.reset_tzpath(to=())
        zoneinfo.ZoneInfo.clear_cache()
        belgrade.cache_clear()


def test_the_cli_pins_the_tz_path(monkeypatch: pytest.MonkeyPatch) -> None:
    zoneinfo.reset_tzpath(to=("/usr/share/zoneinfo",))
    monkeypatch.setattr(cli, "build_parser", lambda: _NoOpParser())
    try:
        cli.main([])

        assert zoneinfo.TZPATH == ()
    finally:
        zoneinfo.reset_tzpath(to=())


class _NoOpParser:
    def parse_args(self, argv: object) -> None:
        pass

    def print_help(self) -> None:
        pass


def test_belgrade_refuses_to_load_with_a_tz_search_path() -> None:
    belgrade.cache_clear()
    try:
        zoneinfo.reset_tzpath(to=("/usr/share/zoneinfo",))

        with pytest.raises(RuntimeError, match="empty the tz search path"):
            belgrade()
    finally:
        zoneinfo.reset_tzpath(to=())
        belgrade.cache_clear()


@pytest.mark.parametrize(
    ("instant", "offset_hours"),
    [
        (utc(2026, 10, 25, 0, 59, 59), 2),
        (utc(2026, 10, 25, 1, 0, 0), 1),
        (utc(2027, 3, 28, 0, 59, 59), 1),
        (utc(2027, 3, 28, 1, 0, 0), 2),
    ],
)
def test_tzdata_has_the_2026_and_2027_changes(instant: datetime, offset_hours: int) -> None:
    assert instant.astimezone(belgrade()).utcoffset() == timedelta(hours=offset_hours)


@pytest.mark.parametrize(
    ("day", "at", "expected", "kind"),
    [
        (date(2026, 7, 1), time(4, 18), utc(2026, 7, 1, 2, 18), LocalKind.NORMAL),
        (date(2027, 1, 15), time(4, 18), utc(2027, 1, 15, 3, 18), LocalKind.NORMAL),
        # The gap moves forward: 02:15 CET doesn't exist, it's 03:15 CEST.
        (SPRING_FORWARD, time(2, 15), utc(2027, 3, 28, 1, 15), LocalKind.GAP),
        (SPRING_FORWARD, time(2, 5), utc(2027, 3, 28, 1, 5), LocalKind.GAP),
        (SPRING_FORWARD, time(3, 0), utc(2027, 3, 28, 1, 0), LocalKind.NORMAL),
        (SPRING_FORWARD, time(1, 59), utc(2027, 3, 28, 0, 59), LocalKind.NORMAL),
        # The fold is its first occurrence (summer time); the second one is an hour later.
        (FALL_BACK, time(2, 5), utc(2026, 10, 25, 0, 5), LocalKind.FOLD),
        (FALL_BACK, time(2, 59), utc(2026, 10, 25, 0, 59), LocalKind.FOLD),
        (FALL_BACK, time(3, 0), utc(2026, 10, 25, 2, 0), LocalKind.NORMAL),
        (FALL_BACK, time(1, 59), utc(2026, 10, 24, 23, 59), LocalKind.NORMAL),
    ],
)
def test_local_time_to_instant(day: date, at: time, expected: datetime, kind: LocalKind) -> None:
    assert to_instant(day, at) == expected
    assert classify_local(day, at) is kind


@pytest.mark.parametrize(
    ("dep_day", "dep", "arr", "arr_offset", "duration"),
    [
        (date(2026, 10, 13), time(23, 40), time(0, 35), 1, timedelta(minutes=55)),  # RK-05
        (date(2026, 10, 24), time(23, 40), time(3, 40), 1, timedelta(hours=5)),  # + the fold hour
        (date(2027, 3, 27), time(23, 40), time(3, 40), 1, timedelta(hours=3)),  # − the gap hour
        (date(2026, 10, 13), time(4, 10), time(5, 19), 0, timedelta(minutes=69)),
    ],
)
def test_overnight_durations(
    dep_day: date, dep: time, arr: time, arr_offset: int, duration: timedelta
) -> None:
    assert to_instant(dep_day, arr, arr_offset) - to_instant(dep_day, dep) == duration


def test_the_second_fold_occurrence_maps_back_to_the_same_wall_clock() -> None:
    first, second = utc(2026, 10, 25, 0, 30), utc(2026, 10, 25, 1, 30)

    assert to_local(first) == to_local(second) == (FALL_BACK, time(2, 30))


@pytest.mark.parametrize(
    ("instant", "day"),
    [
        (utc(2026, 10, 24, 21, 59, 59), date(2026, 10, 24)),  # 23:59:59 CEST
        (utc(2026, 10, 24, 22, 0), date(2026, 10, 25)),  # 00:00 CEST: a new Belgrade day
        (utc(2026, 10, 24, 22, 30), date(2026, 10, 25)),  # ADR-0003: the fetch belongs here
        (utc(2027, 1, 15, 22, 59), date(2027, 1, 15)),
        (utc(2027, 1, 15, 23, 0), date(2027, 1, 16)),  # 00:00 CET
    ],
)
def test_local_date_changes_at_belgrade_midnight(instant: datetime, day: date) -> None:
    assert local_date(instant) == day


def test_the_same_instant_in_any_zone_gives_the_same_answer() -> None:
    instant = utc(2026, 10, 25, 0, 30)
    elsewhere = instant.astimezone(zoneinfo.ZoneInfo("Asia/Tokyo"))

    assert to_local(elsewhere) == to_local(instant)
    assert require_aware(elsewhere) == instant
    assert require_aware(elsewhere).tzinfo is UTC


def test_naive_datetimes_are_refused() -> None:
    naive = datetime(2026, 10, 25, 2, 30)
    for call in (to_local, local_date, require_aware):
        with pytest.raises(ValueError, match="timezone-aware"):
            call(naive)


def test_an_aware_local_time_is_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_instant(date(2026, 10, 13), time(4, 10, tzinfo=UTC))


def _gap_minutes(years: range) -> set[tuple[date, time]]:
    # The last Sunday of March, 02:00-02:59 (EU rule): the only local times that don't exist.
    gaps = set()
    for year in years:
        last_sunday = max(
            date(year, 3, d) for d in range(25, 32) if date(year, 3, d).weekday() == 6
        )
        gaps |= {(last_sunday, time(2, m)) for m in range(60)}
    return gaps


def test_pr12_every_local_minute_round_trips_2026_to_2030() -> None:
    # PR-12: to_local(to_instant(d, t)) == (d, t) for every local time that exists. Exhaustive
    # over 5 years (Hypothesis arrives with T-R0-30); the gap minutes are pinned, not skipped.
    years = range(2026, 2031)
    failures = set()
    day = date(2026, 1, 1)
    while day.year in years:
        for minute in range(24 * 60):
            at = time(minute // 60, minute % 60)
            if to_local(to_instant(day, at)) != (day, at):
                failures.add((day, at))
        day += timedelta(days=1)

    assert failures == _gap_minutes(years)
    assert len(failures) == 5 * 60


@pytest.mark.parametrize("offset", [1, 2])
def test_pr12_day_offsets_round_trip_onto_the_fall_back_day(offset: int) -> None:
    # An arrival `offset` days after departure lands on the fall-back day; every minute
    # (fold minutes included, as their first occurrence) maps back to the same wall clock.
    departure_day = FALL_BACK - timedelta(days=offset)
    for minute in range(24 * 60):
        at = time(minute // 60, minute % 60)

        assert to_local(to_instant(departure_day, at, offset)) == (FALL_BACK, at)


def test_a_fold_1_input_still_means_the_first_occurrence() -> None:
    assert to_instant(FALL_BACK, time(2, 30, fold=1)) == utc(2026, 10, 25, 0, 30)


def test_to_local_returns_fold_0_for_the_second_occurrence() -> None:
    _, at = to_local(utc(2026, 10, 25, 1, 30))  # the second 02:30

    assert (at, at.fold) == (time(2, 30), 0)


def test_the_cli_pins_the_tz_path_before_parsing_arguments() -> None:
    # --version exits inside argument parsing: the reset must already have happened.
    zoneinfo.reset_tzpath(to=("/usr/share/zoneinfo",))
    try:
        with pytest.raises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["--version"])

        assert zoneinfo.TZPATH == ()
    finally:
        zoneinfo.reset_tzpath(to=())
