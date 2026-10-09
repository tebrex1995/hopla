"""Manifest lines (ADR-0003 schema v1): the codec and what a line refuses to hold (NFR-051)."""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from hopla.storage.manifest_lines import (
    LineKind,
    ManifestCorrupt,
    RunEnd,
    RunEndStatus,
    UnitSlot,
    decode_part,
    encode_part,
)
from tests.fakes.manifests import end, fetch, start

T0 = datetime(2026, 10, 9, 10, 35, tzinfo=UTC)
RUN = "01J9ZK3M8Q0000000000000000"


def test_every_line_type_round_trips() -> None:
    lines = (
        start(RUN, T0, units=[("day", T0), ("dawn", T0 - timedelta(hours=6))]),
        fetch(RUN, T0 + timedelta(seconds=3), headers={"etag": '"x"'}),
        fetch(RUN, T0 + timedelta(seconds=6), stored=False, status=None, error="transport:timeout"),
        RunEnd(RUN, RunEndStatus.PARTIAL, T0 + timedelta(seconds=9), 2, 3, "store:server"),
    )

    assert decode_part(encode_part(lines)) == lines


def test_encoding_is_deterministic_json_lines() -> None:
    data = encode_part([start(RUN, T0), end(RUN, T0)])

    assert data == encode_part([start(RUN, T0), end(RUN, T0)])
    assert data.endswith(b"\n") and data.count(b"\n") == 2
    first = data.split(b"\n")[0]
    assert first.index(b'"collector_version"') < first.index(b'"hosts"')  # sorted keys
    assert json.loads(first)["type"] == "run_start" and json.loads(first)["v"] == 1


def test_non_ascii_text_is_kept_as_utf8() -> None:
    line = replace(fetch(RUN, T0), query={"od": "Нови Сад"})

    assert "Нови Сад".encode() in encode_part([line])


def test_times_are_stored_as_utc() -> None:
    plus_two = T0.astimezone(timezone(timedelta(hours=2)))

    decoded = decode_part(encode_part([start(RUN, plus_two)]))[0]

    assert decoded.started_at == T0 and decoded.started_at.tzinfo is UTC  # type: ignore[union-attr]


def test_unknown_fields_are_ignored_when_read() -> None:
    obj = json.loads(encode_part([end(RUN, T0)]))
    obj["added_in_v1_1"] = "whatever"

    assert decode_part(json.dumps(obj).encode()) == (end(RUN, T0),)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"type": "mystery", "v": 1}',
        json.dumps({**json.loads(encode_part([end(RUN, T0)])), "v": 2}).encode(),
        b"not json",
        b'{"type": "run_end", "v": 1, "run_id": "x"}',  # required fields missing
        json.dumps(
            {**json.loads(encode_part([end(RUN, T0)])), "ended_at": "2026-10-09T10:35:00"}
        ).encode(),
        b"\xff\xfe",
        b"[]",
        b"null",
        b"5",
        b"[" * 100_000 + b"]" * 100_000,
    ],
    ids=[
        "unknown-type",
        "newer-version",
        "not-json",
        "missing-fields",
        "naive-time",
        "not-utf8",
        "a-list",
        "null",
        "a-number",
        "too-deep",
    ],
)
def test_unreadable_parts_raise_manifest_corrupt(raw: bytes) -> None:
    with pytest.raises(ManifestCorrupt):
        decode_part(raw)


@pytest.mark.parametrize(
    "header", ["cookie", "set-cookie", "authorization", "x-request-id", "Etag"]
)
def test_a_line_refuses_headers_outside_the_allow_list(header: str) -> None:
    with pytest.raises(ValueError, match="headers not allowed"):
        fetch(RUN, T0, headers={header: "x"})


def test_wordpress_pagination_headers_only_on_notices() -> None:
    headers = {"x-wp-total": "20679", "link": "<…>; rel=next"}

    fetch(RUN, T0, kind=LineKind.NOTICES, headers=headers)
    with pytest.raises(ValueError, match="headers not allowed"):
        fetch(RUN, T0, headers=headers)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@w3.srbvoz.rs/x",
        "https://w3.srbvoz.rs/x#frag",
        "https://w3.srbvoz.rs/x?access_token=abc",
        "https://w3.srbvoz.rs/x?api_key=abc",
        "https://w3.srbvoz.rs/x?SessionId=abc",
    ],
)
def test_a_line_refuses_urls_that_could_carry_secrets(url: str) -> None:
    with pytest.raises(ValueError, match="no credentials|token-like"):
        fetch(RUN, T0, url=url)


@pytest.mark.parametrize(
    "params",
    ["per_page=100&page=2", "orderby=modified&order=desc", "modified_after=2026-10-09T00:00:00",
     "_fields=id,modified", "author=3&author_exclude=4", "search=kašnjenje",
     "keyword=x&side=a&design=b"],
)  # fmt: skip
def test_ordinary_parameters_pass(params: str) -> None:
    url = f"https://www.srbvoz.rs/wp-json/wp/v2/info_post?{params}"

    assert fetch(RUN, T0, host="www.srbvoz.rs", url=url).url == url


@pytest.mark.parametrize(
    "params",
    ["access_token=a", "api_key=a", "apikey=a", "client_secret=a", "csrf_token=a", "pwd=a",
     "X-Auth=a", "token", "access_token=", "accessToken=a", "authToken=a", "clientSecret=a",
     "refreshToken=a", "access+token=a", "auth[token]=a", "apiKey=a"],
)  # fmt: skip
def test_secret_parameter_names_are_refused_even_blank(params: str) -> None:
    with pytest.raises(ValueError, match="token-like"):
        fetch(RUN, T0, url=f"https://w3.srbvoz.rs/x?{params}")


def test_the_query_mapping_is_checked_too() -> None:
    with pytest.raises(ValueError, match="token-like"):
        replace(fetch(RUN, T0), query={"access_token": "SECRET"})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"url": "https://w3.srbvoz.rs/x;jsessionid=ABC"}, "`;` parameters"),
        ({"url": "https://w3.srbvoz.rs/x?a=1;jsessionid=ABC"}, "`;` parameters"),
        ({"url": "http://w3.srbvoz.rs/x"}, "https"),
        ({"host": "bas.rs"}, "isn't the URL's host"),
        ({"error": "see https://x.rs/?token=1"}, "an error is a code"),
        ({"status": None}, "came with a response status"),
        ({"key": "evil/2026-10-09/" + "b" * 64 + ".gz"}, "raw/"),
        ({"transport": "ftp"}, "unknown transport"),
        ({"status": 304}, "a 304 is exactly"),
        ({"requests_used": True}, "whole numbers"),
        ({"bytes": -1}, "whole numbers"),
        ({"requests_used": 4}, "at most 3 attempts"),
        ({"v": 0}, "unsupported manifest version"),
    ],
)
def test_a_line_refuses_inconsistent_or_leaky_values(
    change: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(fetch(RUN, T0), **change)  # type: ignore[arg-type]


def test_a_run_serves_its_own_source_only() -> None:
    with pytest.raises(ValueError, match="own source"):
        replace(start(RUN, T0), units=(UnitSlot("bas", "day", T0),))


@pytest.mark.parametrize(
    ("change", "message"),
    [({"fetches": -1}, "whole numbers"), ({"requests_used": True}, "whole numbers"),
     ({"error": "Traceback: …"}, "an error is a code")],
)  # fmt: skip
def test_run_end_refuses_bad_values(change: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(end(RUN, T0), **change)  # type: ignore[arg-type]


def test_stored_lines_point_at_their_body_and_unstored_ones_at_nothing() -> None:
    good = fetch(RUN, T0)
    assert good.key is not None

    with pytest.raises(ValueError, match="stored line has sha"):
        replace(good, key=None)
    with pytest.raises(ValueError, match="stored line has sha"):
        replace(good, stored=False)
    with pytest.raises(ValueError, match="Belgrade date"):
        replace(good, key=good.key.replace("2026-10-09", "2026-10-08"))


def test_a_store_error_line_is_never_stored() -> None:
    with pytest.raises(ValueError, match="store error"):
        fetch(RUN, T0, error="store:server")

    assert fetch(RUN, T0, stored=False, error="store:server").error == "store:server"


def test_not_modified_is_only_a_304_and_never_a_departure() -> None:
    fetch(RUN, T0, kind=LineKind.NOTICES, status=304, not_modified=True)

    with pytest.raises(ValueError, match="304"):
        fetch(RUN, T0, kind=LineKind.DEPARTURES, status=304, not_modified=True)
    with pytest.raises(ValueError, match="304"):
        fetch(RUN, T0, kind=LineKind.NOTICES, status=200, not_modified=True)


def test_counts_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        fetch(RUN, T0, requests=-1)


def test_headers_read_back_read_only() -> None:
    line = decode_part(encode_part([fetch(RUN, T0, headers={"etag": "1"})]))[0]

    assert isinstance(line.headers, MappingProxyType)  # type: ignore[union-attr]


def test_an_unstored_line_has_no_body_key() -> None:
    with pytest.raises(ValueError):
        replace(
            fetch(RUN, T0, stored=False, status=None), key=f"raw/srbijavoz/2026-10-09/{'b' * 64}.gz"
        )


def test_elapsed_time_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        replace(fetch(RUN, T0), elapsed_ms=-1)


def test_a_run_start_encodes_to_exactly_these_bytes() -> None:
    # The on-disk format is a contract with every reader that will ever exist (ADR-0003).
    assert encode_part([start(RUN, T0, units=[("day", T0)])]) == (
        b'{"collector_version":"0.1.0","hosts":["w3.srbvoz.rs"],'
        b'"run_id":"01J9ZK3M8Q0000000000000000","runner":"github","source":"srbijavoz",'
        b'"started_at":"2026-10-09T10:35:00+00:00","type":"run_start","units":[{"bucket":"day",'
        b'"slot":"2026-10-09T10:35:00+00:00","source":"srbijavoz"}],"v":1}\n'
    )


def test_an_unknown_line_type_is_corrupt() -> None:
    raw = encode_part([fetch(RUN, T0)]).replace(b'"type":"fetch"', b'"type":"probe"')

    with pytest.raises(ManifestCorrupt, match="unknown line type"):
        decode_part(raw)


def test_scheduled_for_round_trips_and_blank_lines_are_skipped() -> None:
    line = replace(fetch(RUN, T0), scheduled_for=T0 - timedelta(minutes=5))

    assert decode_part(encode_part([line]) + b"\n") == (line,)


def test_a_naive_scheduled_for_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(fetch(RUN, T0), scheduled_for=datetime(2026, 10, 9, 10, 30))


@pytest.mark.parametrize(
    "change",
    [{"status": "200"}, {"status": True}, {"status": 99}, {"status": 600}, {"stored": 1},
     {"not_modified": "no"}, {"host": 5}, {"headers": {"etag": 1}}, {"query": {"q": 1}},
     {"sha": 5}],
)  # fmt: skip
def test_a_line_refuses_wrongly_typed_fields(change: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="text|status|true or false"):
        replace(fetch(RUN, T0, stored=False, status=200), **change)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw",
    [b'"run_id":5', b'"hosts":[1]', b'"source":null'],
)
def test_a_run_start_refuses_wrongly_typed_fields(raw: bytes) -> None:
    good = encode_part([start(RUN, T0)])
    field = raw.split(b":")[0]
    bad = re.sub(field + rb':("[^"]*"|\[[^\]]*\])', raw, good, count=1)
    assert bad != good

    with pytest.raises(ManifestCorrupt):
        decode_part(bad)
