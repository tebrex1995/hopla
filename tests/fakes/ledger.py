"""An in-memory `RunLedger` (ADR-0004 S1): runs begin and end like manifest parts, then fold."""

from dataclasses import replace
from datetime import datetime, timedelta

from hopla.core.ledger import LedgerSnapshot, RunOutcome, RunRecord, Unit, fold


class InMemoryRunLedger:
    def __init__(self, stale_after: timedelta = timedelta(minutes=15)) -> None:
        self._records: dict[str, RunRecord] = {}
        self._stale_after = stale_after

    def begin(
        self,
        run_id: str,
        slots: tuple[tuple[Unit, datetime], ...],
        *,
        started_at: datetime,
        started_server: datetime | None = None,
    ) -> None:
        """A run_start line. The server time defaults to the runner time (no skew)."""
        if run_id in self._records:
            raise ValueError(f"run {run_id} already started")
        self._records[run_id] = RunRecord(
            run_id=run_id,
            slots=slots,
            started_at=started_at,
            started_server=started_at if started_server is None else started_server,
            outcome=None,
        )

    def end(self, run_id: str, outcome: RunOutcome) -> None:
        """A run_end line."""
        if run_id not in self._records:
            raise ValueError(f"run {run_id} never started")
        if self._records[run_id].outcome is not None:
            raise ValueError(f"run {run_id} already ended")
        self._records[run_id] = replace(self._records[run_id], outcome=outcome)

    def records(self) -> tuple[RunRecord, ...]:
        return tuple(self._records.values())

    async def snapshot(self, *, server_now: datetime) -> LedgerSnapshot:
        return fold(self._records.values(), server_now=server_now, stale_after=self._stale_after)
