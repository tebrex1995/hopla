"""The `RunLedger` port: the run ledger as read from the store (ADR-0004 S1, ADR-0003).

The manifest-backed reader arrives with the raw store (T-R0-03); tests use
`tests.fakes.ledger.InMemoryRunLedger`. Both build the ledger with `hopla.core.ledger.fold`.
"""

from datetime import datetime
from typing import Protocol

from hopla.core.ledger import LedgerSnapshot


class RunLedger(Protocol):
    async def snapshot(self, *, server_now: datetime) -> LedgerSnapshot:
        """The ledger as the store sees it at `server_now`.

        `server_now` is store time: `tick` takes it from the LastModified of its preflight PUT
        (ADR-0003), so staleness (S4) never depends on the runner's clock.
        """
        ...
