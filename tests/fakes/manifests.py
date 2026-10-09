"""Builders for manifest lines and runs written straight into a store (tests only).

PR B's RunWriter writes runs for real; these write the same layout so the readers can be
tested on their own: part 0000 holds run_start, then parts of at most 10 lines, run_end last.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from hopla.storage.keys import body_key, part_key
from hopla.storage.manifest_lines import (
    FetchLine,
    Line,
    LineKind,
    RunEnd,
    RunEndStatus,
    RunStart,
    UnitSlot,
    encode_part,
)
from hopla.storage.raw_store import RawStore
from tests.fakes.clock import FakeClock

SHA = "b" * 64


def start(
    run_id: str,
    at: datetime,
    *,
    source: str = "srbijavoz",
    units: Sequence[tuple[str, datetime]] = (),
    hosts: tuple[str, ...] = ("w3.srbvoz.rs",),
) -> RunStart:
    slots = tuple(UnitSlot(source, bucket, slot) for bucket, slot in units)
    return RunStart(run_id, source, "github", "0.1.0", at, hosts, slots)


def fetch(
    run_id: str,
    at: datetime,
    *,
    host: str = "w3.srbvoz.rs",
    kind: LineKind = LineKind.DEPARTURES,
    requests: int = 1,
    status: int | None = 200,
    url: str = "https://w3.srbvoz.rs/redvoznje//direktni/X/1/Y/2/13.10.2026/0000/sr",
    stored: bool = True,
    prefix: str = "srbijavoz",
    sha: str = SHA,
    not_modified: bool = False,
    headers: dict[str, str] | None = None,
    error: str | None = None,
) -> FetchLine:
    key = body_key(prefix, at, sha) if stored else None
    return FetchLine(
        run_id=run_id,
        kind=kind,
        host=host,
        url=url,
        fetched_at=at,
        runner="github",
        collector_version="0.1.0",
        status=status,
        requests_used=requests,
        elapsed_ms=120,
        stored=stored,
        sha=sha if stored else None,
        key=key,
        bytes=100 if stored else None,
        gz_bytes=60 if stored else None,
        not_modified=not_modified,
        headers=headers or {},
        error=error,
    )


def end(run_id: str, at: datetime, status: RunEndStatus = RunEndStatus.COMPLETE) -> RunEnd:
    return RunEnd(run_id, status, at, fetches=0, requests_used=0)


async def write_run(
    store: RawStore,
    store_clock: FakeClock,
    run_start: RunStart,
    lines: Sequence[FetchLine] = (),
    run_end: RunEnd | None = None,
    *,
    server_start: datetime | None = None,
) -> None:
    """Writes the parts in order, setting the store clock to `server_start` for part 0000."""
    store_clock.set(server_start or run_start.started_at)
    parts: list[list[Line]] = [[run_start]]
    for i in range(0, len(lines), 10):
        parts.append(list(lines[i : i + 10]))
    if run_end is not None:
        if len(parts) == 1:
            parts.append([])
        parts[-1].append(run_end)
    for seq, part in enumerate(parts):
        key = part_key(run_start.source, run_start.started_at, run_start.run_id, seq)
        assert await store.put_if_absent(
            key, encode_part(part), content_type="application/x-ndjson"
        )
        store_clock.advance(seconds=1)


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)
