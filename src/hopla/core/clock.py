"""The clock port. Core never reads the time itself: it's given `now` (ADR-0006).

The shell passes a `Clock`: `hopla.system_clock.SystemClock` in production and
`tests.fakes.clock.FakeClock` in tests.
"""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, aware UTC."""
        ...
