"""Store keys (ADR-0003 layout, Belgrade dates) and run ids (ULIDs, ADR-0004 S8)."""

from datetime import UTC, datetime, timedelta

import pytest

from hopla.storage import keys
from hopla.storage.ids import new_ulid
from hopla.storage.raw_store import validate_key

SHA = "a" * 64
RUN = "01J9ZK3M8Q0000000000000000"


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("fetched_at", "day"),
    [
        (utc(2026, 10, 9, 21, 59), "2026-10-09"),  # 23:59 CEST
        (utc(2026, 10, 9, 22, 30), "2026-10-10"),  # 00:30 CEST belongs to the new day (ADR-0003)
        (utc(2027, 1, 15, 22, 59), "2027-01-15"),  # 23:59 CET
        (utc(2027, 1, 15, 23, 30), "2027-01-16"),  # 00:30 CET
        (utc(2026, 10, 25, 0, 30), "2026-10-25"),  # the first 02:30 of the fall-back night
        (utc(2026, 10, 25, 1, 30), "2026-10-25"),  # the second one
    ],
)
def test_a_body_is_filed_under_the_belgrade_date_of_its_fetch(
    fetched_at: datetime, day: str
) -> None:
    assert keys.body_key("srbijavoz", fetched_at, SHA) == f"raw/srbijavoz/{day}/{SHA}.gz"


def test_a_run_part_is_filed_under_the_belgrade_date_of_its_start() -> None:
    started = utc(2026, 10, 9, 22, 5)  # 00:05 Belgrade

    assert (
        keys.part_key("srbijavoz", started, RUN, 0) == f"runs/srbijavoz/2026-10-10/{RUN}/0000.jsonl"
    )
    assert keys.part_key("srbijavoz", started, RUN, 9999).endswith("/9999.jsonl")
    assert keys.runs_day_prefix("srbijavoz", started.date()) == "runs/srbijavoz/2026-10-09/"


@pytest.mark.parametrize("seq", [-1, 10_000])
def test_part_numbers_have_four_digits(seq: int) -> None:
    with pytest.raises(ValueError, match="part number"):
        keys.part_key("srbijavoz", utc(2026, 10, 9, 10), RUN, seq)


def test_host_and_job_prefixes() -> None:
    assert keys.host_prefix("w3.srbvoz.rs") == "_host/w3.srbvoz.rs"
    assert keys.job_prefix("digest.weekly") == "_job/digest.weekly"
    assert keys.body_key(keys.host_prefix("www.srbvoz.rs"), utc(2026, 10, 9, 10), SHA).startswith(
        "raw/_host/www.srbvoz.rs/2026-10-09/"
    )


def test_ops_keys() -> None:
    at = utc(2026, 10, 9, 22, 30)

    assert keys.FLAGS_KEY == "ops/flags.json"
    assert keys.disable_key("bas") == "ops/disabled/bas.json"
    assert keys.audit_key(at, RUN) == f"ops/audit/2026-10-10/{RUN}.json"
    assert keys.preflight_key("github") == "_preflight/github"


@pytest.mark.parametrize(
    ("make", "bad"),
    [
        (keys.source_prefix, "Srbijavoz"),
        (keys.source_prefix, "../ops"),
        (keys.host_prefix, "w3.srbvoz.rs/x"),
        (keys.host_prefix, "localhost"),
        (keys.host_prefix, "W3.SRBVOZ.RS"),
        (keys.job_prefix, "Digest"),
        (keys.preflight_key, "GitHub"),
        (keys.disable_key, "bas/../x"),
    ],
)
def test_names_that_could_escape_or_collide_are_refused(make: object, bad: str) -> None:
    with pytest.raises(ValueError):
        make(bad)  # type: ignore[operator]


@pytest.mark.parametrize("bad", ["A" * 64, "a" * 63, "g" * 64])
def test_a_body_key_needs_a_lowercase_sha256(bad: str) -> None:
    with pytest.raises(ValueError, match="sha256"):
        keys.body_key("srbijavoz", utc(2026, 10, 9, 10), bad)


def test_every_key_the_layout_makes_is_a_valid_store_key() -> None:
    at = utc(2026, 10, 9, 10)
    made = [
        keys.body_key("srbijavoz", at, SHA),
        keys.body_key(keys.host_prefix("w3.srbvoz.rs"), at, SHA),
        keys.part_key(keys.job_prefix("terms.snapshot"), at, RUN, 12),
        keys.disable_key("notices_srbijavoz"),
        keys.audit_key(at, RUN),
        keys.preflight_key("mac"),
    ]

    assert [validate_key(k) for k in made] == made


def test_a_ulid_has_26_crockford_characters_and_a_known_vector() -> None:
    at = utc(2026, 10, 9, 11, 0)

    ulid = new_ulid(at, rand=lambda n: b"\x00" * n)

    assert len(ulid) == 26
    assert ulid.endswith("0" * 16)  # 80 zero random bits
    assert (
        ulid[:10] == new_ulid(at, rand=lambda n: b"\xff" * n)[:10]
    )  # same millisecond, same prefix
    assert new_ulid(at, rand=lambda n: b"\xff" * n).endswith("Z" * 16)
    assert set(ulid) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_ulids_sort_by_time() -> None:
    at = utc(2026, 10, 9, 11, 0)
    later = [new_ulid(at + timedelta(milliseconds=ms)) for ms in (0, 1, 1000, 86_400_000)]

    assert later == sorted(later)


def test_a_ulid_is_a_valid_run_id() -> None:
    run_id = new_ulid(utc(2026, 10, 9, 11, 0))

    assert keys.part_key("srbijavoz", utc(2026, 10, 9, 11, 0), run_id, 0).count("/") == 4


def test_a_ulid_needs_an_aware_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        new_ulid(datetime(2026, 10, 9, 11, 0))


def test_a_ulid_matches_a_fixed_vector_and_truncates_to_the_millisecond() -> None:
    zero = lambda n: b"\x00" * n  # noqa: E731

    assert new_ulid(utc(2026, 10, 9, 11, 0), rand=zero) == "01M4G53RW00000000000000000"
    assert new_ulid(utc(2026, 10, 9, 11, 0) + timedelta(microseconds=1999), rand=zero) == (
        "01M4G53RW10000000000000000"
    )


def test_a_ulid_before_the_epoch_is_refused() -> None:
    with pytest.raises(ValueError, match="ULID range"):
        new_ulid(utc(1969, 12, 31, 23, 59))


@pytest.mark.parametrize(
    ("make", "bad"),
    [
        (keys.source_prefix, "9bas"),
        (keys.source_prefix, "bas/raw"),
        (keys.host_prefix, ".".join(["a" * 63] * 4) + ".rs"),  # 258 characters
        (keys.host_prefix, "10.0.0.1"),
        (keys.host_prefix, "w3_x.srbvoz.rs"),
        (keys.job_prefix, "digest/weekly"),
        (keys.preflight_key, "-mac"),
        (lambda v: keys.audit_key(utc(2026, 10, 9, 10), v), "../../x"),
        (lambda v: keys.part_key("s", utc(2026, 10, 9, 10), v, 0), "01J9ZK3M8Q00000000000000IU"),
        (lambda v: keys.part_key("s", utc(2026, 10, 9, 10), v, 0), RUN + "0"),
        (keys.check_prefix, "_host/localhost"),
        (keys.check_prefix, "_job/Digest"),
        (keys.check_prefix, "_other/x"),
        (keys.check_prefix, "srbijavoz/x"),
    ],
)
def test_more_names_that_are_refused(make: object, bad: str) -> None:
    with pytest.raises(ValueError):
        make(bad)  # type: ignore[operator]
