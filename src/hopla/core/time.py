"""Europe/Belgrade time: a local date, time and day offset to and from UTC instants (NFR-044).

Every rule in Hopla is stated in Belgrade time, and every instant that crosses a module
boundary is aware UTC. The zone comes only from the pinned `tzdata` package: entry points
empty the tz search path (`zoneinfo.reset_tzpath(())`, ADR-0006), so the answer is the same on
every machine. The zone loads lazily, after that reset.

Daylight-saving rules (02 §6.3):
- a local time inside the spring-forward gap moves forward by the gap (02:15 → 03:15 CEST);
- a local time inside the fall-back fold means its first occurrence (summer time).
"""

import functools
import zoneinfo
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

TZ_NAME = "Europe/Belgrade"


@functools.cache
def belgrade() -> ZoneInfo:
    # Read once, fresh (no_cache: the shared cache could hold an OS copy loaded earlier), and
    # only with an empty search path, so the data can only come from the pinned tzdata.
    if zoneinfo.TZPATH:
        raise RuntimeError("empty the tz search path first: zoneinfo.reset_tzpath(to=())")
    return ZoneInfo.no_cache(TZ_NAME)


class LocalKind(Enum):
    NORMAL = "normal"
    GAP = "gap"  # skipped by the spring-forward change
    FOLD = "fold"  # repeated by the fall-back change


def require_aware(value: datetime, name: str = "value") -> datetime:
    """`value` in UTC. A naive datetime has no defined instant, so it's refused."""
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got {value!r}")
    return value.astimezone(UTC)


def to_instant(day: date, at: time, day_offset: int = 0) -> datetime:
    """The UTC instant of local time `at` on `day + day_offset` (an overnight arrival uses 1)."""
    if at.tzinfo is not None:
        raise ValueError(f"at must be a local (naive) time, got {at!r}")
    local = datetime.combine(day + timedelta(days=day_offset), at.replace(fold=0), belgrade())
    return local.astimezone(UTC)


def to_local(instant: datetime) -> tuple[date, time]:
    """The Belgrade date and wall-clock time of `instant`."""
    local = require_aware(instant, "instant").astimezone(belgrade())
    return local.date(), local.time().replace(fold=0)


def local_date(instant: datetime) -> date:
    """The Belgrade date of `instant`: the day a fetch and its budget belong to (ADR-0003)."""
    return to_local(instant)[0]


def classify_local(day: date, at: time) -> LocalKind:
    if to_local(to_instant(day, at)) != (day, at):
        return LocalKind.GAP
    first = datetime.combine(day, at.replace(fold=0), belgrade()).utcoffset()
    second = datetime.combine(day, at.replace(fold=1), belgrade()).utcoffset()
    return LocalKind.FOLD if first != second else LocalKind.NORMAL
