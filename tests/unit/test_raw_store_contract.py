"""The `RawStore` contract (ADR-0003), run against every backend (the local folder for now)."""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hopla.storage.raw_store import (
    LocalFsRawStore,
    ObjectMissing,
    RawStore,
    StoreError,
    StoreTimeout,
    validate_key,
)
from tests.fakes.clock import FakeClock
from tests.fakes.raw_store import FaultyRawStore

T0 = datetime(2026, 10, 9, 11, 0, 0, 123456, tzinfo=UTC)
CT = "application/octet-stream"
DISABLE = "ops/disabled/bas.json"


@pytest.fixture
def store_clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture(params=["local"])
async def store(
    request: pytest.FixtureRequest, tmp_path: Path, store_clock: FakeClock
) -> AsyncIterator[RawStore]:
    yield LocalFsRawStore(tmp_path / "store", store_clock)


async def test_a_missing_object_has_no_head_and_get_raises(store: RawStore) -> None:
    assert await store.head("raw/s/2026-10-09/x.gz") is None
    with pytest.raises(ObjectMissing):
        await store.get("raw/s/2026-10-09/x.gz")


async def test_put_if_absent_creates_once_and_never_overwrites(store: RawStore) -> None:
    key = "raw/s/2026-10-09/a.gz"

    assert await store.put_if_absent(key, b"first", content_type=CT) is True
    assert await store.put_if_absent(key, b"second", content_type=CT) is False
    assert await store.get(key) == b"first"


async def test_last_modified_comes_from_the_store_clock_exactly(
    store: RawStore, store_clock: FakeClock
) -> None:
    # The store's clock is the time base for staleness (S4); the runner's clock has no say.
    await store.put_if_absent("runs/s/2026-10-09/r/0000.jsonl", b"x", content_type=CT)
    info = await store.head("runs/s/2026-10-09/r/0000.jsonl")

    assert info is not None
    assert (info.last_modified, info.size) == (T0, 1)
    assert info.last_modified.tzinfo is UTC


async def test_list_is_sorted_by_key_and_filtered_by_prefix(store: RawStore) -> None:
    for key in ("runs/b/x", "runs/a/y", "raw/a/z", "runs/a/x"):
        await store.put_if_absent(key, b".", content_type=CT)

    listing = await store.list("runs/")

    assert [o.key for o in listing.objects] == ["runs/a/x", "runs/a/y", "runs/b/x"]
    assert listing.prefixes == ()


async def test_list_with_a_delimiter_returns_the_next_level(store: RawStore) -> None:
    for key in ("runs/a/1", "runs/a/2", "runs/_host/w3.srbvoz.rs/3", "runs/top"):
        await store.put_if_absent(key, b".", content_type=CT)

    listing = await store.list("runs/", delimiter="/")

    assert listing.prefixes == ("runs/_host/", "runs/a/")
    assert [o.key for o in listing.objects] == ["runs/top"]


async def test_copy_gives_the_copy_a_new_last_modified(
    store: RawStore, store_clock: FakeClock
) -> None:
    await store.put_if_absent("raw/s/2026-10-09/a.gz", b"body", content_type=CT)
    store_clock.advance(minutes=5)

    await store.copy("raw/s/2026-10-09/a.gz", "raw/s/2026-10-10/a.gz")
    info = await store.head("raw/s/2026-10-10/a.gz")

    assert await store.get("raw/s/2026-10-10/a.gz") == b"body"
    assert info is not None and info.last_modified == T0 + timedelta(minutes=5)


async def test_copy_of_a_missing_object_raises(store: RawStore) -> None:
    with pytest.raises(ObjectMissing):
        await store.copy("raw/s/2026-10-09/gone.gz", "raw/s/2026-10-10/gone.gz")


async def test_put_overwrites_ops_and_preflight_objects(
    store: RawStore, store_clock: FakeClock
) -> None:
    await store.put("ops/flags.json", b"1", content_type=CT)
    store_clock.advance(seconds=1)
    await store.put("ops/flags.json", b"2", content_type=CT)
    await store.put("_preflight/github", b"p", content_type=CT)

    info = await store.head("ops/flags.json")

    assert await store.get("ops/flags.json") == b"2"
    assert info is not None and info.last_modified == T0 + timedelta(seconds=1)


@pytest.mark.parametrize("key", ["raw/s/2026-10-09/a.gz", "runs/s/2026-10-09/r/0000.jsonl"])
async def test_bodies_and_manifests_are_never_overwritten_or_deleted(
    store: RawStore, key: str
) -> None:
    with pytest.raises(StoreError, match="only allowed under"):
        await store.put(key, b"x", content_type=CT)
    with pytest.raises(StoreError, match="only allowed under"):
        await store.delete(key)


async def test_delete_is_idempotent_for_disables(store: RawStore) -> None:
    await store.put_if_absent(DISABLE, b"{}", content_type=CT)

    await store.delete(DISABLE)
    await store.delete(DISABLE)

    assert await store.head(DISABLE) is None


async def test_a_disable_is_only_created_never_overwritten(store: RawStore) -> None:
    with pytest.raises(StoreError, match="only allowed under"):
        await store.put(DISABLE, b"{}", content_type=CT)


async def test_delete_if_match_deletes_only_the_object_that_was_read(store: RawStore) -> None:
    await store.put_if_absent(DISABLE, b'{"reason": "robots"}', content_type=CT)
    read = await store.head(DISABLE)
    assert read is not None and read.etag

    with pytest.raises(StoreError) as caught:
        await store.delete(DISABLE, if_match="0" * 32)
    assert caught.value.kind == "conflict"
    assert await store.head(DISABLE) == read

    await store.delete(DISABLE, if_match=read.etag)
    assert await store.head(DISABLE) is None


async def test_delete_if_match_of_a_missing_object_does_nothing(store: RawStore) -> None:
    await store.delete(DISABLE, if_match="0" * 32)

    assert await store.head(DISABLE) is None


async def test_copy_is_create_only(store: RawStore) -> None:
    await store.put_if_absent("raw/s/2026-10-09/a.gz", b"new", content_type=CT)
    await store.put_if_absent("raw/s/2026-10-10/a.gz", b"old", content_type=CT)

    assert await store.copy("raw/s/2026-10-09/a.gz", "raw/s/2026-10-10/a.gz") is False
    assert await store.get("raw/s/2026-10-10/a.gz") == b"old"


@pytest.mark.parametrize("dst", ["runs/s/2026-10-09/r/0000.jsonl", "ops/flags.json", DISABLE])
async def test_copy_only_targets_bodies(store: RawStore, dst: str) -> None:
    await store.put_if_absent("raw/s/2026-10-09/a.gz", b"x", content_type=CT)

    with pytest.raises(StoreError, match="only allowed under raw/"):
        await store.copy("raw/s/2026-10-09/a.gz", dst)
    assert await store.head(dst) is None


async def test_a_key_is_never_both_an_object_and_a_folder(store: RawStore) -> None:
    await store.put_if_absent("raw/s/2026-10-09/a", b"x", content_type=CT)
    await store.put_if_absent("raw/s/2026-10-10/b/c", b"x", content_type=CT)

    for key in ("raw/s/2026-10-09/a/b", "raw/s/2026-10-10/b"):
        with pytest.raises(StoreError) as caught:
            await store.put_if_absent(key, b"y", content_type=CT)
        assert caught.value.kind == "invalid"
    with pytest.raises(ObjectMissing):
        await store.get("raw/s/2026-10-10/b")


@pytest.mark.parametrize("prefix", ["etc/", ".tmp/", "raw/../", "../"])
async def test_list_refuses_prefixes_outside_the_roots(store: RawStore, prefix: str) -> None:
    with pytest.raises(StoreError) as caught:
        await store.list(prefix)

    assert caught.value.kind == "invalid"


async def test_list_matches_a_partial_last_segment(store: RawStore) -> None:
    for key in ("raw/s/2026-10-09/ab.gz", "raw/s/2026-10-09/b.gz", "raw/s/2026-10-10/a.gz"):
        await store.put_if_absent(key, b".", content_type=CT)

    listing = await store.list("raw/s/2026-10-09/a")

    assert [o.key for o in listing.objects] == ["raw/s/2026-10-09/ab.gz"]


async def test_temporary_files_never_show_up(store: RawStore) -> None:
    await store.put_if_absent("raw/s/2026-10-09/a.gz", b"x", content_type=CT)

    assert [o.key for o in (await store.list("")).objects] == ["raw/s/2026-10-09/a.gz"]


@pytest.mark.parametrize(
    "key",
    ["", "/raw/a", "raw//a", "raw/../ops/x", "raw/./a", "raw\\a", "raw/čvor", "a" * 513, ".tmp/x",
     "etc/passwd", "raw"],
)  # fmt: skip
def test_bad_keys_are_refused(key: str) -> None:
    with pytest.raises(StoreError) as caught:
        validate_key(key)

    assert caught.value.kind == "invalid"


async def test_a_bad_key_is_refused_before_anything_is_written(store: RawStore) -> None:
    with pytest.raises(StoreError):
        await store.put_if_absent("raw/../escape", b"x", content_type=CT)

    assert (await store.list("")).objects == ()


def test_store_errors_name_what_the_manifest_records() -> None:
    assert StoreError("server", "put", "k").manifest_error() == "store:server"
    assert StoreError("throttled", "put", "k").manifest_error() == "store:throttled"
    assert StoreTimeout("put", "k").manifest_error() == "store_timeout"
    assert ObjectMissing("get", "k").kind == "missing"
    with pytest.raises(ValueError, match="unknown store error kind"):
        StoreError("weird", "put")


async def test_the_faulty_store_fails_where_told_and_can_land_first(store: RawStore) -> None:
    faulty = FaultyRawStore(store, fail=lambda op, key: key.endswith("b.gz"), land_then_fail=True)

    assert await faulty.put_if_absent("raw/s/2026-10-09/a.gz", b"a", content_type=CT)
    with pytest.raises(StoreError):
        await faulty.put_if_absent("raw/s/2026-10-09/b.gz", b"b", content_type=CT)

    assert await store.get("raw/s/2026-10-09/b.gz") == b"b"  # it landed anyway
    assert faulty.calls == [
        ("put_if_absent", "raw/s/2026-10-09/a.gz"),
        ("put_if_absent", "raw/s/2026-10-09/b.gz"),
    ]


async def test_a_symlink_never_leads_outside_the_local_store(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "store"
    local = LocalFsRawStore(root, FakeClock(T0))
    (root / "raw").mkdir()
    (root / "raw" / "s").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StoreError, match="outside the store"):
        await local.put_if_absent("raw/s/2026-10-09/a.gz", b"x", content_type=CT)
    with pytest.raises(StoreError, match="outside the store"):
        await local.get("raw/s/2026-10-09/a.gz")
    assert list(outside.iterdir()) == []


async def test_local_file_system_errors_become_store_errors(tmp_path: Path) -> None:
    root = tmp_path / "store"
    local = LocalFsRawStore(root, FakeClock(T0))
    await local.put_if_absent("raw/s/2026-10-09/a.gz", b"x", content_type=CT)
    (root / "raw" / "s" / "2026-10-09" / "a.gz").chmod(0)

    try:
        with pytest.raises(StoreError) as caught:
            await local.get("raw/s/2026-10-09/a.gz")
    finally:
        (root / "raw" / "s" / "2026-10-09" / "a.gz").chmod(0o644)

    assert caught.value.kind == "denied"


def test_a_key_with_a_space_is_refused() -> None:
    with pytest.raises(StoreError):
        validate_key("ops/a b")


async def test_preflight_objects_are_never_deleted(store: RawStore) -> None:
    await store.put("_preflight/github", b"x", content_type=CT)

    with pytest.raises(StoreError, match="only allowed under"):
        await store.delete("_preflight/github")


async def test_last_modified_is_exact_at_an_awkward_microsecond(
    store: RawStore, store_clock: FakeClock
) -> None:
    store_clock.set(datetime(2026, 10, 9, 11, 0, 0, 1994, tzinfo=UTC))
    await store.put_if_absent("raw/s/2026-10-09/a.gz", b"a", content_type=CT)

    info = await store.head("raw/s/2026-10-09/a.gz")

    assert info is not None and info.last_modified == store_clock.now()


async def test_a_leftover_temporary_file_is_never_listed(tmp_path: Path) -> None:
    local = LocalFsRawStore(tmp_path, FakeClock(T0))
    (tmp_path / ".tmp" / "leftover").write_bytes(b"x")

    assert (await local.list("")).objects == ()


async def test_listings_are_sorted_at_both_levels(store: RawStore) -> None:
    for key in ["raw/b", "raw/a/x", "raw/q/1", "raw/c/1", "raw/z/1", "raw/m/1", "raw/e/1"]:
        await store.put_if_absent(key, b"x", content_type=CT)

    flat = [o.key for o in (await store.list("raw/")).objects]
    folders = (await store.list("raw/", delimiter="/")).prefixes

    assert flat == sorted(flat) and len(flat) == 7
    assert list(folders) == sorted(folders) and len(folders) == 6


async def test_a_prefix_matches_from_the_start_of_the_key(store: RawStore) -> None:
    await store.put_if_absent("runs/raw/x", b"x", content_type=CT)

    assert (await store.list("raw/")).objects == ()


async def test_head_gives_the_md5_etag_and_nothing_for_a_folder(store: RawStore) -> None:
    await store.put_if_absent("raw/s/2026-10-09/a.gz", b"x", content_type=CT)

    info = await store.head("raw/s/2026-10-09/a.gz")

    assert info is not None and info.etag == hashlib.md5(b"x", usedforsecurity=False).hexdigest()
    assert await store.head("raw/s/2026-10-09") is None


async def test_the_faulty_store_wraps_every_call(store: RawStore) -> None:
    await store.put_if_absent("raw/s/2026-10-09/a.gz", b"a", content_type=CT)
    faulty = FaultyRawStore(
        store, fail=lambda op, key: key.endswith(("b.gz", "flags.json")), land_then_fail=True
    )

    with pytest.raises(StoreError):
        await faulty.copy("raw/s/2026-10-09/a.gz", "raw/s/2026-10-10/b.gz")
    with pytest.raises(StoreError):
        await faulty.put("ops/flags.json", b"{}", content_type=CT)
    await faulty.put_if_absent("raw/s/2026-10-10/c/d", b".", content_type=CT)

    assert await store.get("ops/flags.json") == b"{}"  # landed, then failed
    assert await store.get("raw/s/2026-10-10/b.gz") == b"a"  # the copy landed too
    assert (await faulty.list("raw/s/2026-10-10/", delimiter="/")).prefixes == (
        "raw/s/2026-10-10/c/",
    )
    with pytest.raises(StoreError):
        await FaultyRawStore(store, fail=lambda op, key: op == "get").get("raw/s/2026-10-09/a.gz")


async def test_list_never_follows_a_symlink_out_of_the_local_store(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"s")
    root = tmp_path / "store"
    local = LocalFsRawStore(root, FakeClock(T0))
    (root / "ops").mkdir()
    (root / "ops" / "disabled").symlink_to(outside, target_is_directory=True)
    (root / "ops" / "link.json").symlink_to(outside / "secret.txt")

    assert (await local.list("ops/disabled/")).objects == ()
    assert (await local.list("ops/")).objects == ()


async def test_a_tmp_folder_below_the_root_is_an_ordinary_folder(store: RawStore) -> None:
    await store.put_if_absent("ops/disabled/.tmp/x.json", b"{}", content_type=CT)

    assert [o.key for o in (await store.list("ops/disabled/")).objects] == [
        "ops/disabled/.tmp/x.json"
    ]
