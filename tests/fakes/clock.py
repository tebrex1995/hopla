"""A controllable clock for tests (04 §6). Server time (store LastModified) is a second instance."""

from datetime import UTC, date, datetime, timedelta

from hopla.config import parse_time
from hopla.core.time import LocalKind, belgrade, classify_local, require_aware


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = require_aware(start, "start")

    def now(self) -> datetime:
        return self._now

    def advance(self, *, minutes: float = 0, seconds: float = 0) -> datetime:
        step = timedelta(minutes=minutes, seconds=seconds)
        if step < timedelta(0):
            raise ValueError("a clock only moves forward; use set() to jump back")
        self._now += step
        return self._now

    def set(self, instant: datetime) -> datetime:
        """Jump to `instant`, backwards too (clock-skew tests)."""
        self._now = require_aware(instant, "instant")
        return self._now

    def set_local(self, hhmm: str, *, on: date, fold: int = 0) -> datetime:
        """Jump to a Belgrade wall-clock time; `fold=1` is the second 02:xx of a fall-back night."""
        at = parse_time(hhmm)
        if classify_local(on, at) is LocalKind.GAP:
            raise ValueError(f"{hhmm} doesn't exist in Belgrade on {on} (spring-forward gap)")
        local = datetime.combine(on, at.replace(fold=fold), belgrade())
        return self.set(local.astimezone(UTC))
