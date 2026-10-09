"""The real clock, for the shell (pipeline, scrapers). Core is given `now` instead (ADR-0006)."""

from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
