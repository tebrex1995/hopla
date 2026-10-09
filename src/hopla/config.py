"""Runtime settings (pydantic-settings, `HOPLA_*` environment variables) and the schedule data.

Core never imports this module: the shell builds frozen core objects from it
(`Settings.schedule_config()`) and passes them in (ADR-0001).

The buckets and their slot times are 02 §6.3, written in a compact grammar: comma-separated
items, each `HH:MM` or `HH:MM-HH:MM/N` (every N minutes from the first time to the last).
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hopla.core.ledger import Unit
from hopla.core.schedule import BucketSpec, ScheduleConfig


class Profile(StrEnum):
    """Where `hopla tick` runs (ADR-0004): GitHub cron while public or private, or the VPS."""

    PUBLIC = "public"
    PRIVATE = "private"
    TARGET = "target"


# UTC crons per profile. Belgrade times need both DST offsets, so the dawn run has two triggers
# and `due()` lets only one of them through (S9). The target is a worker, not cron.
CRONS: Mapping[Profile, tuple[str, ...]] = MappingProxyType(
    {
        Profile.PUBLIC: ("5,15,35 * * * *", "18 2,3 * * *"),
        Profile.PRIVATE: ("7 3-21 * * *", "7 23 * * *", "18 2,3 * * *"),
        Profile.TARGET: (),
    }
)


@dataclass(frozen=True)
class Relaxations:
    """The thresholds the POC relaxes (02 §6.4, ADR-0004)."""

    missed_run_alert: timedelta
    source_down: timedelta
    freshness_confirmed: timedelta


def _minutes(alert: int, down: int, fresh: int) -> Relaxations:
    return Relaxations(timedelta(minutes=alert), timedelta(minutes=down), timedelta(minutes=fresh))


RELAXATIONS: Mapping[Profile, Relaxations] = MappingProxyType(
    {
        Profile.PUBLIC: _minutes(75, 120, 150),
        Profile.PRIVATE: _minutes(120, 180, 180),
        Profile.TARGET: _minutes(15, 60, 90),
    }
)


def parse_times(spec: str) -> tuple[time, ...]:
    """The slot times of a grammar string, sorted and unique."""
    times: set[time] = set()
    for item in spec.split(","):
        item = item.strip()
        if "-" not in item:
            times.add(parse_time(item))
            continue
        span, _, step = item.partition("/")
        first, _, last = span.partition("-")
        start, end = _minute_of_day(parse_time(first)), _minute_of_day(parse_time(last))
        if not step.isdigit() or int(step) == 0:
            raise ValueError(f"{item!r}: the step must be a positive number of minutes")
        if end < start or (end - start) % int(step):
            raise ValueError(f"{item!r}: the range must end on a step after its start")
        times.update(time(m // 60, m % 60) for m in range(start, end + 1, int(step)))
    return tuple(sorted(times))


_HHMM = re.compile(r"(\d\d):(\d\d)", re.ASCII)


def parse_time(text: str) -> time:
    """A wall-clock `HH:MM` (two ASCII digits each)."""
    if not (m := _HHMM.fullmatch(text.strip())):
        raise ValueError(f"{text!r} is not HH:MM")
    return time(int(m[1]), int(m[2]))  # raises on 25:00 or 12:60


def _minute_of_day(at: time) -> int:
    return at.hour * 60 + at.minute


@dataclass(frozen=True)
class _BucketDef:
    times: str
    date_offsets: tuple[int, ...]
    interval_minutes: int


# 02 §6.3. Notices: every 30 min 06:05-22:05, hourly otherwise (40 slots a day). The dawn
# interval (a day) is above the 4 h backoff cap, so a failed dawn retries after 4 h (S3).
BUCKETS: Mapping[str, _BucketDef] = MappingProxyType(
    {
        "dawn": _BucketDef("04:18", (0, 1), 24 * 60),
        "day": _BucketDef("04:35-23:35/30", (0, 1), 30),
        "night": _BucketDef("00:05-03:05/60", (0, 1), 60),
        "far": _BucketDef("02:15-22:15/240", (2, 3, 4, 5, 6, 7), 240),
        "notices": _BucketDef("00:05-05:05/60,06:05-22:05/30,23:05", (), 30),
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOPLA_", frozen=True, extra="forbid")

    profile: Profile = Profile.PUBLIC
    # The units per source until `config/sources.yaml` arrives (T-R0-05). Trains first (D-25).
    source_buckets: dict[str, tuple[str, ...]] = {
        "srbijavoz": ("dawn", "day", "night", "far"),
        "notices_srbijavoz": ("notices",),
    }
    budget_per_host_day: PositiveInt = 1000  # requests per registrable domain (ADR-0002 §3)

    @model_validator(mode="before")
    @classmethod
    def _no_unknown_env(cls, data: Any) -> Any:
        # `extra="forbid"` doesn't cover environment variables: a misspelt HOPLA_* in a
        # workflow would be ignored and the defaults used. Refuse it instead.
        known = {f"HOPLA_{name}".upper() for name in cls.model_fields}
        if unknown := sorted(
            n for n in os.environ if n.upper().startswith("HOPLA_") and n.upper() not in known
        ):
            raise ValueError(f"unknown settings in the environment: {unknown}")
        return data

    def crons(self) -> tuple[str, ...]:
        return CRONS[self.profile]

    def relaxations(self) -> Relaxations:
        return RELAXATIONS[self.profile]

    def schedule_config(self) -> ScheduleConfig:
        buckets = tuple(
            BucketSpec(
                name=name,
                local_times=parse_times(spec.times),
                date_offsets=spec.date_offsets,
                interval=timedelta(minutes=spec.interval_minutes),
            )
            for name, spec in BUCKETS.items()
        )
        units = tuple(
            Unit(source, bucket)
            for source, names in sorted(self.source_buckets.items())
            for bucket in names
        )
        return ScheduleConfig(buckets=buckets, units=units)
