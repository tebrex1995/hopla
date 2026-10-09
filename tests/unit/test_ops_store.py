"""Ops state in the store (ADR-0004 S7, S11; ADR-0002 §4; TS-015, TS-016)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hopla.storage.keys import FLAGS_KEY, audit_key, disable_key
from hopla.storage.ops_store import (
    Disable,
    DisableReason,
    Flags,
    OpsStore,
    OpsStoreError,
)
from hopla.storage.raw_store import LocalFsRawStore, ObjectMissing, StoreError
from tests.fakes.clock import FakeClock
from tests.fakes.manifests import minutes
from tests.fakes.raw_store import FaultyRawStore

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


@pytest.fixture
def ops(store: LocalFsRawStore, clock: FakeClock) -> OpsStore:
    return OpsStore(store, clock)


async def test_absent_flags_mean_the_defaults(ops: OpsStore) -> None:
    assert await ops.read_flags() == Flags()


async def test_flags_round_trip_through_a_fresh_reader(
    ops: OpsStore, store: LocalFsRawStore, clock: FakeClock
) -> None:
    await ops.write_flags(Flags(pause_collection=True, active_runner={"srbvoz.rs": "mac"}))

    flags = await OpsStore(store, clock).read_flags()  # a fresh runner, no cache

    assert (flags.pause_collection, dict(flags.active_runner)) == (True, {"srbvoz.rs": "mac"})


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json",
        b"[]",
        b'{"pause_collection": "yes"}',
        b'{"pause_collection": 1}',
        b'{"active_runner": {"srbvoz.rs": 1}}',
        b'{"active_runner": {"srbvoz.rs": "laptop"}}',
        b'{"active_runner": ["mac"]}',
        b'{"pause": true}',  # a typo in the kill switch stops the run, never passes as "not paused"
    ],
)
async def test_malformed_flags_fail_closed(
    ops: OpsStore, store: LocalFsRawStore, raw: bytes
) -> None:
    await store.put(FLAGS_KEY, raw, content_type="application/json")

    with pytest.raises(OpsStoreError):
        await ops.read_flags()


async def test_a_disable_is_create_only_and_keeps_the_first_reason(
    ops: OpsStore, clock: FakeClock
) -> None:
    first = await ops.disable(
        "bas",
        DisableReason.ACCESS_CONTROL,
        at=T0,
        run_id=R1,
        detail="access_control:unexpected",
        raw_key="raw/bas/2026-10-09/x.gz",
    )
    second = await ops.disable("bas", DisableReason.ROBOTS, at=T0 + minutes(30))

    assert (first, second) == (True, False)
    assert (await ops.disabled_sources())["bas"] == Disable(
        "bas",
        DisableReason.ACCESS_CONTROL,
        T0,
        R1,
        "access_control:unexpected",
        "raw/bas/2026-10-09/x.gz",
    )


async def test_enable_deletes_any_disable_and_leaves_an_audit_record(
    ops: OpsStore, store: LocalFsRawStore, clock: FakeClock
) -> None:
    await ops.disable("bas", DisableReason.TERMS, at=T0)
    clock.advance(minutes=60)

    assert await ops.enable("bas", actor="owner") is True
    assert await ops.disabled_sources() == {}
    audits = (await store.list("ops/audit/")).objects
    assert len(audits) == 1 and audits[0].key.startswith("ops/audit/2026-10-09/")
    record = json.loads(await store.get(audits[0].key))
    assert (record["actor"], record["previous"]["reason"], record["source"]) == (
        "owner",
        "terms",
        "bas",
    )


async def test_enable_without_a_disable_does_nothing(ops: OpsStore, store: LocalFsRawStore) -> None:
    assert await ops.enable("bas", actor="owner") is False
    assert (await store.list("ops/audit/")).objects == ()


@pytest.mark.parametrize(
    ("reason", "cleared"),
    [
        (DisableReason.ROBOTS, True),
        (DisableReason.TERMS, False),
        (DisableReason.ACCESS_CONTROL, False),
    ],
)
async def test_the_robots_self_clear_removes_only_robots_disables(
    ops: OpsStore, reason: DisableReason, cleared: bool
) -> None:
    await ops.disable("srbijavoz", reason, at=T0)

    assert await ops.clear_robots_disable("srbijavoz", run_id=R1) is cleared
    assert ("srbijavoz" in await ops.disabled_sources()) is not cleared


async def test_a_malformed_disable_fails_closed(ops: OpsStore, store: LocalFsRawStore) -> None:
    await store.put_if_absent(
        disable_key("bas"), b'{"source": "bas", "reason": "bored"}', content_type="application/json"
    )

    with pytest.raises(OpsStoreError, match="not a disable record"):
        await ops.disabled_sources()


async def test_a_disable_naming_another_source_fails_closed(
    ops: OpsStore, store: LocalFsRawStore
) -> None:
    body = json.dumps({"source": "srbijavoz", "reason": "robots", "at": T0.isoformat()})
    await store.put_if_absent(disable_key("bas"), body.encode(), content_type="application/json")

    with pytest.raises(OpsStoreError, match="not its own"):
        await ops.disabled_sources()


async def test_objects_that_arent_disables_are_ignored(
    ops: OpsStore, store: LocalFsRawStore
) -> None:
    await store.put_if_absent("ops/disabled/README.txt", b"hi", content_type="text/plain")
    await ops.disable("bas", DisableReason.TERMS, at=T0)

    assert set(await ops.disabled_sources()) == {"bas"}


async def test_a_disable_needs_an_aware_time(ops: OpsStore) -> None:
    with pytest.raises(ValueError, match="at"):
        await ops.disable("bas", DisableReason.TERMS, at=datetime(2026, 10, 9, 12))


async def test_the_robots_self_clear_without_a_disable_does_nothing(
    ops: OpsStore, store: LocalFsRawStore
) -> None:
    assert await ops.clear_robots_disable("srbijavoz", run_id=R1) is False
    assert (await store.list("ops/audit/")).objects == ()


class _ReplacedAfterHead(LocalFsRawStore):
    """A new trip replaces the disable between our head and our delete."""

    async def get(self, key: str) -> bytes:
        data = await super().get(key)
        if key == disable_key("bas"):
            path = self._root / key
            path.write_bytes(data.replace(b"robots", b"terms"))
        return data


async def test_enable_never_deletes_a_disable_written_after_it_was_read(
    tmp_path: Path, clock: FakeClock
) -> None:
    store = _ReplacedAfterHead(tmp_path, clock)
    ops = OpsStore(store, clock)
    await ops.disable("bas", DisableReason.ROBOTS, at=T0)

    with pytest.raises(StoreError) as caught:
        await ops.clear_robots_disable("bas", run_id=R1)

    assert caught.value.kind == "conflict"
    assert (await OpsStore(store, clock).disabled_sources())["bas"].reason is DisableReason.TERMS
    assert (await store.list("ops/audit/")).objects == ()  # nothing claims an enable


async def test_an_audit_collision_stops_before_the_delete(
    ops: OpsStore, store: LocalFsRawStore, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ops.disable("bas", DisableReason.TERMS, at=T0)
    monkeypatch.setattr("hopla.storage.ops_store.new_ulid", lambda now: R1)
    await store.put_if_absent(audit_key(T0, R1), b"{}", content_type="application/json")

    with pytest.raises(StoreError, match="audit id collision"):
        await ops.enable("bas", actor="owner")

    assert "bas" in await ops.disabled_sources()


async def test_the_audit_record_says_who_when_and_on_which_day(
    ops: OpsStore, store: LocalFsRawStore, clock: FakeClock
) -> None:
    await ops.disable("srbijavoz", DisableReason.ROBOTS, at=T0)
    clock.set(T0 + timedelta(hours=14))  # 02:35 Belgrade, the next day

    assert await ops.clear_robots_disable("srbijavoz", run_id=R1)

    [audit] = (await store.list("ops/audit/")).objects
    record = json.loads(await store.get(audit.key))
    assert audit.key.startswith("ops/audit/2026-10-10/")
    assert (record["action"], record["actor"], record["at"]) == (
        "enable",
        f"robots-self-clear:{R1}",
        clock.now().isoformat(),
    )


@pytest.mark.parametrize("at", ['"2026-10-09T10:00:00"', "5"], ids=["naive", "not-text"])
async def test_a_disable_with_a_bad_time_fails_closed(
    ops: OpsStore, store: LocalFsRawStore, at: str
) -> None:
    raw = f'{{"source": "bas", "reason": "robots", "at": {at}}}'.encode()
    await store.put_if_absent(disable_key("bas"), raw, content_type="application/json")

    with pytest.raises(OpsStoreError):
        await ops.disabled_sources()


async def test_a_failed_delete_leaves_an_audit_saying_so(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    faulty = FaultyRawStore(store, fail=lambda op, key: op == "delete")
    ops = OpsStore(faulty, clock)
    await ops.disable("bas", DisableReason.TERMS, at=T0)

    with pytest.raises(StoreError):
        await ops.enable("bas", actor="owner")

    records = [json.loads(await store.get(o.key)) for o in (await store.list("ops/audit/")).objects]
    enable = next(r for r in records if r["action"] == "enable")
    failed = next(r for r in records if r["action"] == "enable_failed")
    assert enable["etag"] == (await store.head(disable_key("bas"))).etag  # type: ignore[union-attr]
    assert (failed["error"], failed["source"]) == ("store:server", "bas")
    assert "bas" in await ops.disabled_sources()


async def test_a_disable_enabled_between_list_and_get_is_skipped(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    gone = FaultyRawStore(
        store,
        fail=lambda op, key: op == "get" and key == disable_key("bas"),
        error=lambda op, key: ObjectMissing(op, key),
    )
    await OpsStore(store, clock).disable("bas", DisableReason.TERMS, at=T0)
    await OpsStore(store, clock).disable("srbijavoz", DisableReason.TERMS, at=T0)

    assert set(await OpsStore(gone, clock).disabled_sources()) == {"srbijavoz"}
    assert await OpsStore(gone, clock).enable("bas", actor="owner") is False


async def test_duplicate_flag_keys_fail_closed(ops: OpsStore, store: LocalFsRawStore) -> None:
    raw = b'{"pause_collection": true, "pause_collection": false}'
    await store.put(FLAGS_KEY, raw, content_type="application/json")

    with pytest.raises(OpsStoreError, match="duplicate"):
        await ops.read_flags()


async def test_a_delete_that_landed_but_errored_counts_as_done(
    store: LocalFsRawStore, clock: FakeClock
) -> None:
    landed = FaultyRawStore(store, fail=lambda op, key: op == "delete", land_then_fail=True)
    ops = OpsStore(landed, clock)
    await ops.disable("bas", DisableReason.TERMS, at=T0)

    assert await ops.enable("bas", actor="owner") is True

    records = [json.loads(await store.get(o.key)) for o in (await store.list("ops/audit/")).objects]
    assert [r["action"] for r in records] == ["enable"]
    assert "bas" not in await ops.disabled_sources()
