"""The run ledger read from manifests (ADR-0004 S1, S4; TS-006): what a fresh runner learns."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hopla.core.ledger import RunOutcome, Unit
from hopla.storage.manifest_lines import Line, RunEndStatus, encode_part
from hopla.storage.manifests import ManifestRunLedger, discover_prefixes, load_manifests
from hopla.storage.raw_store import LocalFsRawStore, ObjectMissing, StoreError
from tests.fakes.clock import FakeClock
from tests.fakes.ledger import InMemoryRunLedger
from tests.fakes.manifests import end, fetch, minutes, start, write_run
from tests.fakes.raw_store import FaultyRawStore

T0 = datetime(2026, 10, 9, 10, 35, tzinfo=UTC)  # 12:35 Belgrade
DAY = Unit("srbijavoz", "day")
DAWN = Unit("srbijavoz", "dawn")
STALE = timedelta(minutes=15)
R1, R2, R3 = (
    "01J9ZK3M8Q000000000000000A",
    "01J9ZK3M8Q000000000000000B",
    "01J9ZK3M8Q000000000000000C",
)


@pytest.fixture
def store_clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def store(tmp_path: Path, store_clock: FakeClock) -> LocalFsRawStore:
    return LocalFsRawStore(tmp_path, store_clock)


async def test_the_manifest_ledger_equals_the_in_memory_one(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    # The same runs, recorded both ways, give the same ledger (S1).
    memory = InMemoryRunLedger(STALE)
    runs = [
        (R1, T0, [("dawn", T0 - minutes(377)), ("day", T0)], RunEndStatus.COMPLETE),
        (R2, T0 + minutes(30), [("day", T0 + minutes(30))], RunEndStatus.FAILED),
        (R3, T0 + minutes(60), [("day", T0 + minutes(60))], None),
    ]
    for run_id, at, units, status in runs:
        lines = [fetch(run_id, at + timedelta(seconds=s)) for s in range(12)]  # two parts
        await write_run(
            store,
            store_clock,
            start(run_id, at, units=units),
            lines,
            None if status is None else end(run_id, at + minutes(1), status),
        )
        memory.begin(run_id, tuple((Unit("srbijavoz", b), s) for b, s in units), started_at=at)
        if status is not None:
            memory.end(run_id, RunOutcome(status.value))
    server_now = T0 + minutes(65)

    from_store = await ManifestRunLedger(store, stale_after=STALE).snapshot(server_now=server_now)
    from_memory = await memory.snapshot(server_now=server_now)

    assert from_store == from_memory
    assert (
        from_store.state(DAY).n_failures == 1
        and from_store.state(DAY).in_progress_since is not None
    )


@pytest.mark.parametrize("skew", [minutes(-10), minutes(0), minutes(10)])
async def test_staleness_is_judged_on_store_time_whatever_the_runner_clock(
    store: LocalFsRawStore, store_clock: FakeClock, skew: timedelta
) -> None:
    # The runner's clock is off by `skew`; the store stamped part 0000 at T0 (S4, S8).
    await write_run(store, store_clock, start(R1, T0 + skew, units=[("day", T0)]), server_start=T0)
    ledger = ManifestRunLedger(store, stale_after=STALE)

    young = await ledger.snapshot(server_now=T0 + minutes(14))
    stale = await ledger.snapshot(server_now=T0 + minutes(15))

    assert young.state(DAY).in_progress_since == T0
    assert (stale.state(DAY).n_failures, stale.state(DAY).last_attempt_at) == (1, T0 + skew)


async def test_run_end_in_a_later_part_closes_the_run(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    lines = [fetch(R1, T0 + timedelta(seconds=s)) for s in range(25)]  # parts 0001-0003

    await write_run(
        store, store_clock, start(R1, T0, units=[("day", T0)]), lines, end(R1, T0 + minutes(2))
    )
    index = await load_manifests(store, prefixes=["srbijavoz"], days=[date(2026, 10, 9)])

    assert [len(r.lines) for r in index.runs] == [25]
    assert index.records()[0].outcome is RunOutcome.COMPLETE


async def test_a_skipped_run_changes_nothing(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    # Paused or not the active runner (S11): recorded, but no attempt and no failure.
    await write_run(
        store,
        store_clock,
        start(R1, T0, units=[("day", T0)]),
        [],
        end(R1, T0, RunEndStatus.SKIPPED),
    )

    snapshot = await ManifestRunLedger(store, stale_after=STALE).snapshot(
        server_now=T0 + minutes(30)
    )

    assert snapshot.state(DAY).last_attempt_at is None and snapshot.state(DAY).n_failures == 0


async def test_runs_on_the_neighbouring_belgrade_days_are_read(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    # Yesterday's dawn run serves today's ledger; a runner ahead of the store files its run
    # under tomorrow's date just before midnight.
    yesterday = T0 - timedelta(days=1)
    tomorrow_file = datetime(2026, 10, 9, 21, 58, tzinfo=UTC) + minutes(
        10
    )  # 00:08 Belgrade, runner +10
    await write_run(
        store,
        store_clock,
        start(R1, yesterday, units=[("dawn", yesterday)]),
        [],
        end(R1, yesterday),
    )
    await write_run(
        store,
        store_clock,
        start(R2, tomorrow_file, units=[("day", T0)]),
        [],
        end(R2, tomorrow_file),
        server_start=datetime(2026, 10, 9, 21, 58, tzinfo=UTC),
    )

    snapshot = await ManifestRunLedger(store, stale_after=STALE).snapshot(
        server_now=datetime(2026, 10, 9, 21, 59, tzinfo=UTC)
    )

    assert snapshot.state(DAWN).last_success_slot == yesterday
    assert snapshot.state(DAY).last_success_slot == T0


async def test_runs_older_than_yesterday_are_not_read(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    old = T0 - timedelta(days=2)
    await write_run(store, store_clock, start(R1, old, units=[("day", old)]), [], end(R1, old))

    snapshot = await ManifestRunLedger(store, stale_after=STALE).snapshot(server_now=T0)

    assert snapshot.state(DAY).last_success_slot is None


async def test_a_corrupt_part_is_reported_and_its_run_left_out(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    await write_run(store, store_clock, start(R1, T0, units=[("day", T0)]), [], end(R1, T0))
    await store.put_if_absent(
        f"runs/srbijavoz/2026-10-09/{R2}/0000.jsonl", b"{broken", content_type="x"
    )

    index = await load_manifests(store, prefixes=["srbijavoz"], days=[date(2026, 10, 9)])

    assert [c.key for c in index.corrupt] == [f"runs/srbijavoz/2026-10-09/{R2}/0000.jsonl"]
    assert [r.run_id for r in index.records()] == [R1]


async def test_a_corrupt_later_part_keeps_the_run(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    await write_run(store, store_clock, start(R1, T0, units=[("day", T0)]))
    await store.put_if_absent(
        f"runs/srbijavoz/2026-10-09/{R1}/0001.jsonl", b"\xff", content_type="x"
    )

    index = await load_manifests(store, prefixes=["srbijavoz"], days=[date(2026, 10, 9)])

    assert len(index.corrupt) == 1
    assert index.records()[0].outcome is None  # no readable run_end: in progress or died


async def test_stray_objects_under_runs_are_ignored(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    await write_run(store, store_clock, start(R1, T0, units=[("day", T0)]), [], end(R1, T0))
    await store.put_if_absent("runs/srbijavoz/2026-10-09/notes.txt", b"x", content_type="x")

    index = await load_manifests(store, prefixes=["srbijavoz"], days=[date(2026, 10, 9)])

    assert [r.run_id for r in index.runs] == [R1] and index.corrupt == ()


async def test_prefixes_are_discovered_including_hosts_and_jobs(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    await write_run(store, store_clock, start(R1, T0, units=[("day", T0)]))
    await write_run(store, store_clock, start(R2, T0, source="_host/w3.srbvoz.rs"))
    await write_run(store, store_clock, start(R3, T0, source="_job/digest.weekly", hosts=()))

    assert await discover_prefixes(store) == (
        "_host/w3.srbvoz.rs",
        "_job/digest.weekly",
        "srbijavoz",
    )
    index = await load_manifests(store, prefixes=None, days=[date(2026, 10, 9)])
    assert {r.prefix for r in index.runs} == {
        "_host/w3.srbvoz.rs",
        "_job/digest.weekly",
        "srbijavoz",
    }


def _part(run_id: str, seq: int = 0) -> str:
    return f"runs/srbijavoz/2026-10-09/{run_id}/{seq:04d}.jsonl"


@pytest.mark.parametrize(
    ("seq", "lines", "why"),
    [
        (0, lambda: [fetch(R1, T0)], "run_start alone"),
        (0, lambda: [start(R1, T0), start(R1, T0)], "run_start alone"),
        (0, lambda: [fetch(R1, T0), start(R1, T0)], "run_start alone"),
        (0, lambda: [start(R1, T0), fetch(R1, T0)], "run_start alone"),
        (0, lambda: [], "run_start alone"),
        (0, lambda: [start(R2, T0)], "another run"),
        (0, lambda: [start(R1, T0, source="bas")], "filed under 'srbijavoz'"),
        (1, lambda: [start(R1, T0)], "only in part 0000"),
        (1, lambda: [end(R1, T0), fetch(R1, T0)], "run_end is the last line"),
    ],
    ids=[
        "no-start",
        "two-starts",
        "start-not-first",
        "start-not-alone",
        "empty",
        "other-run",
        "misfiled",
        "late-start",
        "end",
    ],
)
async def test_a_part_that_doesnt_fit_its_place_is_corrupt(
    store: LocalFsRawStore,
    store_clock: FakeClock,
    seq: int,
    lines: Callable[[], list[Line]],
    why: str,
) -> None:
    if seq:
        await write_run(store, store_clock, start(R1, T0, units=[("day", T0)]))
    await store.put_if_absent(_part(R1, seq), encode_part(lines()), content_type="x")

    index = await load_manifests(store, prefixes=["srbijavoz"], days=[date(2026, 10, 9)])

    assert [(c.key, c.run_id, c.seq) for c in index.corrupt] == [(_part(R1, seq), R1, seq)]
    assert why in index.corrupt[0].error


async def test_a_store_failure_while_reading_fails_the_read(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    # A network blip must not make a finished run look missing (it would run again).
    await write_run(store, store_clock, start(R1, T0, units=[("day", T0)]), [], end(R1, T0))
    faulty = FaultyRawStore(store, fail=lambda op, key: op == "get")

    with pytest.raises(StoreError) as caught:
        await load_manifests(faulty, prefixes=["srbijavoz"], days=[date(2026, 10, 9)])

    assert caught.value.kind == "server"


async def test_a_part_listed_then_gone_is_corrupt(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    await write_run(store, store_clock, start(R1, T0, units=[("day", T0)]))
    gone = FaultyRawStore(
        store, fail=lambda op, key: op == "get", error=lambda op, key: ObjectMissing(op, key)
    )

    index = await load_manifests(gone, prefixes=["srbijavoz"], days=[date(2026, 10, 9)])

    assert [c.key for c in index.corrupt] == [_part(R1)]
    assert index.records() == ()


async def test_parts_after_run_end_are_corrupt_and_ignored(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    await write_run(store, store_clock, start(R1, T0), [], end(R1, T0, RunEndStatus.PARTIAL))
    later = encode_part([fetch(R1, T0), end(R1, T0)])
    assert await store.put_if_absent(_part(R1, 2), later, content_type="x")

    index = await load_manifests(store, prefixes=["srbijavoz"], days=[date(2026, 10, 9)])

    [run] = index.runs
    assert run.end is not None and run.end.status is RunEndStatus.PARTIAL and run.lines == ()
    assert [(c.key, c.error) for c in index.corrupt] == [(_part(R1, 2), "a part after run_end")]


async def test_a_wrongly_typed_field_makes_the_part_corrupt(
    store: LocalFsRawStore, store_clock: FakeClock
) -> None:
    # Else latest_robots would crash every tick comparing "200" with an int (NFR-064).
    await write_run(store, store_clock, start(R1, T0))
    raw = encode_part([fetch(R1, T0)]).replace(b'"status":200', b'"status":"200"')
    assert await store.put_if_absent(_part(R1, 1), raw, content_type="x")

    index = await load_manifests(store, prefixes=["srbijavoz"], days=[date(2026, 10, 9)])

    assert [c.key for c in index.corrupt] == [_part(R1, 1)] and index.runs[0].lines == ()


@pytest.mark.parametrize("stray", ["runs/bad-name/x", "runs/_host/localhost/x", "runs/_other/x"])
async def test_a_stray_folder_under_runs_is_skipped(
    store: LocalFsRawStore, store_clock: FakeClock, stray: str
) -> None:
    await write_run(store, store_clock, start(R1, T0, units=[("day", T0)]), [], end(R1, T0))
    assert await store.put_if_absent(stray, b"x", content_type="x")

    snapshot = await ManifestRunLedger(store, stale_after=STALE).snapshot(server_now=T0)

    assert await discover_prefixes(store) == ("srbijavoz",)
    assert snapshot.state(DAY).last_success_slot == T0


async def test_corrupt_parts_are_sorted_by_key_across_days(store: LocalFsRawStore) -> None:
    later = f"runs/srbijavoz/2026-10-10/{R1}/0000.jsonl"
    for key in (later, _part(R1)):
        assert await store.put_if_absent(key, b"{", content_type="x")

    index = await load_manifests(
        store, prefixes=["srbijavoz"], days=[date(2026, 10, 10), date(2026, 10, 9)]
    )

    assert [c.key for c in index.corrupt] == [_part(R1), later]


@pytest.mark.parametrize("tail", [f"{R1}/1.jsonl", f"extra/{R1}/0000.jsonl", f"{R1}/0000.json"])
async def test_keys_outside_the_part_layout_are_ignored(store: LocalFsRawStore, tail: str) -> None:
    raw = encode_part([start(R1, T0)])
    assert await store.put_if_absent(f"runs/srbijavoz/2026-10-09/{tail}", raw, content_type="x")

    index = await load_manifests(store, prefixes=["srbijavoz"], days=[date(2026, 10, 9)])

    assert (index.runs, index.corrupt) == ((), ())
