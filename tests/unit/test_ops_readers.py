"""The ops readers over one ManifestIndex: the budget (S7), robots answers and validators."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hopla.storage.manifest_lines import MAX_ATTEMPTS, FetchLine, LineKind, encode_part
from hopla.storage.manifests import ManifestIndex, RunSummary, load_manifests
from hopla.storage.ops_store import (
    CORRUPT_PART_CHARGE,
    RequestCounts,
    latest_robots,
    latest_validators,
    requests_by_host,
)
from hopla.storage.raw_store import LocalFsRawStore
from tests.fakes.clock import FakeClock
from tests.fakes.manifests import end, fetch, minutes, start, write_run

T0 = datetime(2026, 10, 9, 10, 35, tzinfo=UTC)
R1, R2 = "01J9ZK3M8Q000000000000000A", "01J9ZK3M8Q000000000000000B"
W3, WWW = "w3.srbvoz.rs", "www.srbvoz.rs"
NOTICES_URL = "https://www.srbvoz.rs/wp-json/wp/v2/info_post?per_page=100"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> LocalFsRawStore:
    return LocalFsRawStore(tmp_path, clock)


async def _index(store: LocalFsRawStore, *days: date) -> ManifestIndex:
    return await load_manifests(store, prefixes=None, days=days)


async def test_requests_are_summed_per_host_name_by_each_fetchs_belgrade_date(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    # A run that starts at 23:58 Belgrade fetches on both sides of midnight (S7).
    before = datetime(2026, 10, 9, 21, 58, tzinfo=UTC)
    lines = [
        fetch(R1, before + minutes(1), requests=3),  # 23:59 → 10-09
        fetch(R1, before + minutes(3), requests=2),  # 00:01 → 10-10
        fetch(R1, before + minutes(4), host=WWW, kind=LineKind.NOTICES, url=NOTICES_URL),
    ]
    await write_run(
        store, clock, start(R1, before, units=[("night", before)]), lines
    )  # still running
    await write_run(
        store,
        clock,
        start(R2, before, source="_host/w3.srbvoz.rs"),
        [fetch(R2, before + minutes(3), kind=LineKind.ROBOTS, prefix="_host/w3.srbvoz.rs")],
    )

    index = await _index(store, date(2026, 10, 9), date(2026, 10, 10))

    assert requests_by_host(index, date(2026, 10, 9)) == RequestCounts({W3: 3})
    assert requests_by_host(index, date(2026, 10, 10)) == RequestCounts({W3: 3, WWW: 1})


async def test_latest_robots_answers_only_with_a_fresh_definitive_fetch(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    def robots(at: datetime, status: int | None, stored: bool = True) -> FetchLine:
        return fetch(
            R1,
            at,
            kind=LineKind.ROBOTS,
            status=status,
            stored=stored and status is not None,
            prefix="_host/w3.srbvoz.rs",
            url="https://w3.srbvoz.rs/robots.txt",
        )

    lines = [
        robots(T0 - timedelta(hours=25), 200),
        robots(T0 - timedelta(hours=2), 404),
        robots(T0 - timedelta(hours=1), 503),
        robots(T0 - minutes(30), None),
    ]
    await write_run(
        store, clock, start(R1, T0 - timedelta(hours=25), source="_host/w3.srbvoz.rs"), lines
    )
    index = await _index(store, date(2026, 10, 8), date(2026, 10, 9))

    found = latest_robots(index, W3, now=T0)

    assert found is not None and found.status == 404  # 503 and the network error don't count
    assert latest_robots(index, W3, now=T0 + timedelta(hours=23)) is None  # 404 now too old
    assert latest_robots(index, WWW, now=T0) is None


async def test_latest_validators_skip_unstored_and_departure_fetches(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    lines = [
        fetch(R1, T0, host=WWW, kind=LineKind.NOTICES, url=NOTICES_URL, headers={"etag": '"1"'}),
        fetch(
            R1,
            T0 + minutes(30),
            host=WWW,
            kind=LineKind.NOTICES,
            url=NOTICES_URL,
            stored=False,
            status=None,
            error="transport:timeout",
        ),
        fetch(R1, T0 + minutes(40)),  # a departure fetch: never conditional
    ]
    await write_run(store, clock, start(R1, T0, source="notices_srbijavoz", hosts=(WWW,)), lines)
    index = await _index(store, date(2026, 10, 9))

    found = latest_validators(index, NOTICES_URL)

    assert found is not None and found.headers["etag"] == '"1"'
    assert latest_validators(index, lines[2].url) is None


async def test_a_corrupt_part_is_charged_to_the_hosts_its_run_contacts(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    await write_run(store, clock, start(R1, T0, hosts=(W3, WWW)), [fetch(R1, T0)])
    assert await store.put_if_absent(
        f"runs/srbijavoz/2026-10-09/{R1}/0002.jsonl", b"{torn", content_type="x"
    )

    index = await _index(store, date(2026, 10, 9))

    charge = CORRUPT_PART_CHARGE
    assert requests_by_host(index, date(2026, 10, 9)).by_host == {W3: 1 + charge, WWW: charge}
    # Its fetches may fall after midnight: the next day is charged too, never the one after.
    assert requests_by_host(index, date(2026, 10, 10)).by_host == {W3: charge, WWW: charge}
    assert requests_by_host(index, date(2026, 10, 11)) == RequestCounts({})


async def test_a_lost_run_start_is_charged_to_every_host_of_its_prefix(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    await write_run(store, clock, start(R1, T0, hosts=(W3,)), [fetch(R1, T0)])
    await write_run(store, clock, start(R2, T0, hosts=(WWW,)))
    lost = "01J9ZK3M8Q000000000000000C"
    assert await store.put_if_absent(
        f"runs/srbijavoz/2026-10-09/{lost}/0000.jsonl", b"\xff", content_type="x"
    )

    index = await _index(store, date(2026, 10, 9))

    charge = CORRUPT_PART_CHARGE
    assert requests_by_host(index, date(2026, 10, 9)) == RequestCounts(
        {W3: 1 + charge, WWW: charge}
    )


async def test_a_lost_host_run_is_charged_to_its_host(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    assert await store.put_if_absent(
        f"runs/_host/w3.srbvoz.rs/2026-10-09/{R1}/0000.jsonl", b"\xff", content_type="x"
    )

    index = await _index(store, date(2026, 10, 9))

    assert requests_by_host(index, date(2026, 10, 9)) == RequestCounts({W3: CORRUPT_PART_CHARGE})


async def test_a_run_whose_hosts_cant_be_known_is_charged_to_every_host(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    for seq in (0, 1):
        assert await store.put_if_absent(
            f"runs/srbijavoz/2026-10-09/{R1}/{seq:04d}.jsonl", b"{", content_type="x"
        )

    counts = requests_by_host(await _index(store, date(2026, 10, 9)), date(2026, 10, 9))

    assert counts == RequestCounts({}, unattributed=2 * CORRUPT_PART_CHARGE)
    assert counts.used(W3) == counts.used("bas.rs") == 2 * CORRUPT_PART_CHARGE


async def test_a_job_run_with_no_hosts_is_charged_nothing(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    await write_run(store, clock, start(R1, T0, source="_job/digest.weekly", hosts=()))
    assert await store.put_if_absent(
        f"runs/_job/digest.weekly/2026-10-09/{R1}/0001.jsonl", b"{", content_type="x"
    )

    index = await _index(store, date(2026, 10, 9))

    assert requests_by_host(index, date(2026, 10, 9)) == RequestCounts({})


def _robots(at: datetime, status: int) -> FetchLine:
    return fetch(
        R1,
        at,
        kind=LineKind.ROBOTS,
        status=status,
        prefix="_host/w3.srbvoz.rs",
        url="https://w3.srbvoz.rs/robots.txt",
    )


@pytest.mark.parametrize(
    ("status", "cached"), [(200, True), (404, True), (429, False), (500, False)]
)
async def test_only_a_2xx_or_a_4xx_other_than_429_is_a_robots_answer(
    store: LocalFsRawStore, clock: FakeClock, status: int, cached: bool
) -> None:
    await write_run(store, clock, start(R1, T0, source="_host/w3.srbvoz.rs"), [_robots(T0, status)])
    index = await _index(store, date(2026, 10, 9))

    assert (latest_robots(index, W3, now=T0 + minutes(1)) is not None) is cached


def _notice(at: datetime, **kw: object) -> FetchLine:
    return fetch(R1, at, host=WWW, kind=LineKind.NOTICES, url=NOTICES_URL, **kw)  # type: ignore[arg-type]


async def test_validators_come_from_a_good_answer_never_an_error_page(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    lines = [
        _notice(T0, headers={"etag": '"good"'}),
        _notice(T0 + minutes(10), status=304, not_modified=True, headers={"etag": '"good"'}),
        _notice(T0 + minutes(20), status=500, headers={"etag": '"error-page"'}),
    ]
    await write_run(store, clock, start(R1, T0, source="notices_srbijavoz", hosts=(WWW,)), lines)
    index = await _index(store, date(2026, 10, 9))

    found = latest_validators(index, NOTICES_URL)

    assert found is not None and found.fetched_at == T0 + minutes(10)


H = timedelta(hours=1)


@pytest.mark.parametrize(
    ("lines", "want"),
    [
        ([fetch(R1, T0 - H, kind=LineKind.ROBOTS, stored=False)], None),
        ([fetch(R1, T0 - H, kind=LineKind.ROBOTS, status=100)], None),
        ([fetch(R1, T0 - 24 * H, kind=LineKind.ROBOTS)], None),  # exactly 24 h: too old
        ([fetch(R1, T0 - 23.5 * H, kind=LineKind.ROBOTS)], 200),
        ([fetch(R1, T0 - H, kind=LineKind.TERMS)], None),
        (
            [
                fetch(R1, T0 - 3 * H, kind=LineKind.ROBOTS),
                fetch(R1, T0 - H, kind=LineKind.ROBOTS, status=404),
            ],
            404,
        ),
    ],
    ids=["unstored", "1xx", "24h", "23.5h", "terms", "newest"],
)
def test_latest_robots_edges(lines: list[FetchLine], want: int | None) -> None:
    index = ManifestIndex(
        (RunSummary(R1, "_host/w3.srbvoz.rs", T0.date(), None, None, None, tuple(lines)),)
    )

    found = latest_robots(index, W3, now=T0)

    assert (found.status if found else None) == want


async def test_fetches_after_run_end_still_count_exactly(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    # They're out of the ledger, but they were real requests: never a flat 30 instead of 50.
    await write_run(store, clock, start(R1, T0), [], end(R1, T0))
    after = [fetch(R1, T0 + minutes(n), requests=MAX_ATTEMPTS) for n in range(10)]
    assert await store.put_if_absent(
        f"runs/srbijavoz/2026-10-09/{R1}/0002.jsonl", encode_part(after), content_type="x"
    )

    index = await _index(store, date(2026, 10, 9))

    assert index.corrupt[0].error == "a part after run_end"
    assert requests_by_host(index, date(2026, 10, 9)) == RequestCounts({W3: 10 * MAX_ATTEMPTS})
    assert requests_by_host(index, date(2026, 10, 10)) == RequestCounts({})
