"""Settings and schedule data (02 §6.3, §6.4; ADR-0004 profiles)."""

import dataclasses
import os
from datetime import time, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from hopla.config import BUCKETS, CRONS, RELAXATIONS, Profile, Settings, parse_time, parse_times
from hopla.core.ledger import Unit


def test_the_public_profile_is_the_default() -> None:
    settings = Settings()

    assert settings.profile is Profile.PUBLIC
    assert settings.crons() == ("5,15,35 * * * *", "18 2,3 * * *")


@pytest.mark.parametrize(
    ("profile", "alert", "down", "fresh"),
    [("public", 75, 120, 150), ("private", 120, 180, 180), ("target", 15, 60, 90)],
)
def test_relaxations_per_profile(profile: str, alert: int, down: int, fresh: int) -> None:
    relaxed = Settings(profile=Profile(profile)).relaxations()

    assert (relaxed.missed_run_alert, relaxed.source_down, relaxed.freshness_confirmed) == (
        timedelta(minutes=alert),
        timedelta(minutes=down),
        timedelta(minutes=fresh),
    )


def test_the_private_profile_crons_keep_both_dawn_triggers() -> None:
    assert Settings(profile=Profile.PRIVATE).crons() == (
        "7 3-21 * * *",
        "7 23 * * *",
        "18 2,3 * * *",
    )
    assert Settings(profile=Profile.TARGET).crons() == ()


def test_the_profile_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOPLA_PROFILE", "private")

    assert Settings().profile is Profile.PRIVATE


def test_unknown_settings_and_bad_values_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        Settings(profile="staging")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(budget_per_host_day=0)
    with pytest.raises(ValidationError):
        Settings(unknown_setting=1)  # type: ignore[call-arg]


def test_settings_are_frozen() -> None:
    with pytest.raises(ValidationError):
        Settings().profile = Profile.TARGET


@pytest.mark.parametrize(
    ("spec", "count", "first", "last"),
    [
        ("04:18", 1, time(4, 18), time(4, 18)),
        ("04:35-23:35/30", 39, time(4, 35), time(23, 35)),
        ("00:05-03:05/60", 4, time(0, 5), time(3, 5)),
        ("02:15-22:15/240", 6, time(2, 15), time(22, 15)),
        ("00:05-05:05/60,06:05-22:05/30,23:05", 40, time(0, 5), time(23, 5)),
        ("10:00, 09:00, 10:00", 2, time(9, 0), time(10, 0)),  # sorted, duplicates merged
    ],
)
def test_time_grammar(spec: str, count: int, first: time, last: time) -> None:
    times = parse_times(spec)

    assert (len(times), times[0], times[-1]) == (count, first, last)
    assert list(times) == sorted(set(times))


@pytest.mark.parametrize(
    "spec",
    [
        "25:00",
        "12:60",
        "4:5",
        "04-05",
        "05:00-04:00/30",
        "04:00-05:00/0",
        "04:00-05:00/7",
        "04:00-05:00/x",
        "",
    ],
)
def test_time_grammar_rejects(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_times(spec)


def test_the_schedule_has_the_02_6_3_buckets_and_the_trains_first_units() -> None:
    cfg = Settings().schedule_config()

    assert [b.name for b in cfg.buckets] == ["dawn", "day", "night", "far", "notices"]
    assert set(cfg.units) == {
        Unit("srbijavoz", "dawn"),
        Unit("srbijavoz", "day"),
        Unit("srbijavoz", "night"),
        Unit("srbijavoz", "far"),
        Unit("notices_srbijavoz", "notices"),
    }
    assert cfg.bucket("dawn").interval == timedelta(hours=24)
    assert cfg.bucket("far").date_offsets == (2, 3, 4, 5, 6, 7)
    assert set(BUCKETS) == {b.name for b in cfg.buckets}


def test_a_unit_on_an_unknown_bucket_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown buckets"):
        Settings(source_buckets={"srbijavoz": ("hourly",)}).schedule_config()


def test_the_schedule_is_frozen() -> None:
    cfg = Settings().schedule_config()

    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.stale_after = timedelta(0)  # type: ignore[misc]


def test_lowercase_settings_are_read_and_stripped_for_the_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pydantic-settings matches names case-insensitively, so conftest strips any case too.
    assert not [n for n in os.environ if n.upper().startswith("HOPLA_")]
    monkeypatch.setenv("hopla_profile", "private")

    assert Settings().profile is Profile.PRIVATE


def test_a_misspelt_setting_in_the_environment_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # A typo in a workflow must not silently fall back to the defaults.
    monkeypatch.setenv("HOPLA_PROFIL", "private")

    with pytest.raises(ValidationError, match="HOPLA_PROFIL"):
        Settings()


@pytest.mark.parametrize("text", ["\u0660\u0664:\u0663\u0665", "004:35", "4:35", "04:5", " 04:35x"])
def test_parse_time_takes_two_ascii_digits_each(text: str) -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        parse_time(text)


def test_the_schedule_tables_are_read_only() -> None:
    for table in (CRONS, RELAXATIONS, BUCKETS):
        with pytest.raises(TypeError):
            table["new"] = None  # type: ignore[index]


@pytest.mark.parametrize("spec", ["04:00-05:00/+30", "04:00-05:00/-30", "+4:00"])
def test_time_grammar_rejects_signs(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_times(spec)


@pytest.mark.parametrize(
    ("spec", "times"),
    [
        ("04:00-04:00/30", (time(4),)),  # a one-point range
        ("04:00-05:00/30 ,06:00", (time(4), time(4, 30), time(5), time(6))),  # spaces around items
    ],
)
def test_time_grammar_edges(spec: str, times: tuple[time, ...]) -> None:
    assert parse_times(spec) == times


def test_bucket_dates_and_budget_match_02_6_3() -> None:
    cfg = Settings().schedule_config()

    assert {b.name: b.date_offsets for b in cfg.buckets} == {
        "dawn": (0, 1),
        "day": (0, 1),
        "night": (0, 1),
        "far": (2, 3, 4, 5, 6, 7),
        "notices": (),
    }
    assert Settings().budget_per_host_day == 1000


def test_units_are_ordered_by_source() -> None:
    settings = Settings(source_buckets={"b": ("day",), "a": ("night",), "c": ("far",)})

    assert settings.schedule_config().units == (
        Unit("a", "night"),
        Unit("b", "day"),
        Unit("c", "far"),
    )


def test_a_live_runner_must_use_the_s3_store() -> None:
    # A local folder on a fresh runner would forget the ledger and the budget (NFR-060).
    with pytest.raises(ValidationError, match="must use the s3 store"):
        Settings(runner="github")


R2 = "https://acct.eu.r2.cloudflarestorage.com"


def _s3(
    endpoint: str | None = R2, key_id: str | None = "id-123", secret: str | None = "secret-456"
) -> Settings:
    return Settings(
        store_backend="s3",
        s3_endpoint_url=endpoint,
        s3_access_key_id=None if key_id is None else SecretStr(key_id),
        s3_secret_access_key=None if secret is None else SecretStr(secret),
    )


@pytest.mark.parametrize(
    ("key_id", "secret"), [(None, None), ("id-123", None), (None, "s"), ("id-123", " "), ("", "s")]
)
def test_the_s3_store_needs_both_keys_and_no_blank_one(
    key_id: str | None, secret: str | None
) -> None:
    with pytest.raises(ValidationError, match="access key id and a secret"):
        _s3(key_id=key_id, secret=secret)


@pytest.mark.parametrize(
    "endpoint",
    [None, "http://acct.r2.example.com", "https://user:pw@acct.r2.example.com", "acct.r2"],
)
def test_the_s3_endpoint_is_https_with_no_credentials(endpoint: str | None) -> None:
    with pytest.raises(ValidationError, match="https URL"):
        _s3(endpoint)


def test_a_settings_error_never_shows_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # A partial secret in a public Actions log isn't masked by GitHub.
    monkeypatch.setenv("HOPLA_S3_SECRET_ACCESS_KEY", "SUPERSECRET-9f8e7d")
    monkeypatch.setenv("HOPLA_RUNNER", "github")

    with pytest.raises(ValidationError) as caught:
        Settings()

    assert "9f8e7d" not in str(caught.value) and "SUPERSECRET" not in str(caught.value)


def test_a_complete_s3_store_is_accepted_and_hides_its_secrets() -> None:
    settings = Settings(
        runner="github",
        store_backend="s3",
        s3_endpoint_url="https://acct.eu.r2.cloudflarestorage.com",
        s3_access_key_id=SecretStr("id-123"),
        s3_secret_access_key=SecretStr("secret-456"),
    )

    assert settings.s3_bucket == "hopla-raw"
    assert "secret-456" not in repr(settings) and "id-123" not in repr(settings)


def test_the_store_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOPLA_STORE_ROOT", "/tmp/raw")
    monkeypatch.setenv("HOPLA_STORE_CALL_TIMEOUT_S", "12.5")

    settings = Settings()

    assert (str(settings.store_root), settings.store_call_timeout_s, settings.runner) == (
        "/tmp/raw",
        12.5,
        "dev",
    )


@pytest.mark.parametrize("runner", ["github", "mac", "vps"])
def test_every_live_runner_must_use_the_s3_store(runner: str) -> None:
    with pytest.raises(ValidationError, match="must use the s3 store"):
        Settings(runner=runner)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change", [{"store_call_timeout_s": 0}, {"runner": "laptop"}, {"store_backend": "gcs"}]
)
def test_store_settings_refuse_bad_values(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**change)  # type: ignore[arg-type]


def test_conditional_put_is_off_until_the_emulator_proves_it() -> None:
    assert Settings().s3_conditional_put is False
