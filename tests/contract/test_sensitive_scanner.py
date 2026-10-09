"""The sensitive-data scanner itself (`tests/contract/sensitive.py`), against real body content."""

import time

import pytest

from tests.contract.sensitive import decode_text, hits, json_strings, scan_texts, sensitive_matches


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("pisite na putnik.info@example.rs", "email"),
        ("Kontakt: Ana.Petrovic+hopla@MAIL.EXAMPLE.COM.", "email"),
        ("pišite đorđe@example.rs", "email"),
        ("petrović@example.rs", "email"),
        ("tel 064 123 4567", "phone"),
        ("tel 064 123 45 67", "phone"),
        ("064/123-45-67", "phone"),
        ("064/12-34-567", "phone"),
        ("064.123.4567", "phone"),
        ("+381 63 123 456", "phone"),
        ("+381641234567", "phone"),
        ("+381 (0)64 123 4567", "phone"),
        ("+381-64-123-45-67", "phone"),
        ("381641234567", "phone"),
        ("00381 65/123-4567", "phone"),
        ("066\xa0123\xa04567", "phone"),  # &nbsp; after unescaping
        ("klijent 192.168.1.20 ", "ipv4"),
        ("http://10.1.2.3/admin", "ipv4"),
        ("from 2001:db8::1 at", "ipv6"),
        ("fe80::1", "ipv6"),
        ("id=0123456789abcdef0123456789ABCDEF", "hex_token"),
        ("req 123e4567-e89b-12d3-a456-426614174000", "uuid"),
        ("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", "jwt"),
        ("Authorization: Bearer abcdef123456", "bearer"),
        ('<input name="__RequestVerificationToken" type="hidden" value="CfDJ8x">', "form_token"),
        ('<input type="hidden" value="dDwtMTA3" name="__VIEWSTATE" />', "form_token"),
        ('wpApiSettings = {"root": "/", "nonce": "a1b2c3d4e5"}', "form_token"),
        ("Cookie set: ASP.NET_SessionId=abc123; path=/", "session_cookie"),
        (".ASPXAUTH=0F2E3D", "session_cookie"),
        ("_ga=GA1.1.123456789.1696850000", "session_cookie"),
        ("_gid=GA1.2.123456789.1696850000", "session_cookie"),
        ("mc_session_ids[default]=199b4e9c", "session_cookie"),
        ("https://example.rs/a?b=1&token=abc", "token_param"),
        ("https://example.rs/cb#access_token=abc", "token_param"),
        ("https://example.rs/a;jsessionid=ABC", "token_param"),
        ("https://maps.example.com/js?key=AIzaSyA", "token_param"),
        ("https://example.rs/wp-admin/post.php?nonce=abc", "token_param"),
        ("https://example.rs/a?sid=1", "token_param"),
        ("HTTP/1.1 200\nSet-Cookie: a=b", "header_line"),
        ("HTTP/1.1 200\n\n  \n  Set-Cookie: a=b", "header_line"),  # after blank, indented lines
        ("authorization: Basic x", "header_line"),
    ],
)
def test_scanner_flags(text: str, rule: str) -> None:
    assert rule in {r for r, _ in sensitive_matches(text)}


@pytest.mark.parametrize(
    "text",
    [
        "Polazak: <b>04:10</b>  13.10.2026",  # times and dd.MM.yyyy dates
        'data-datum="06-10-2026"',  # a date that starts like a mobile prefix
        "06.10.2026. u 06:45",
        "od 06. oktobra",
        "Call centar 011 360 28 99",  # the public landline of the operator
        "voz br. 12541, stanica 24402, idvoza 1131",  # train, station and row ids
        "idvoza 120641234567",  # digits inside a longer number aren't a phone
        "ref 06412345678901",
        "/wp-includes/js/jquery/jquery.min.js?ver=3.7.1",  # versions are not addresses
        "x-aspnet-version: 4.0.30319.42000",
        "verzija 1.2.3.4.5",  # a 5-part version isn't an IPv4
        "2026-10-09T12:36:18",  # WP timestamps are not IPv6
        "a::before { content: '' }",
        "Napomena :: x",  # a bare "::" holds no address
        "https://www.srbvoz.rs/?post_type=info_post&#038;p=39340",
        "https://market.android.com/details?id=zs.redvoznje",
        "logo@2x.png",
        "deadbeef0123",  # short hex
        "f123e4567-e89b-12d3-a456-426614174000",  # a longer run isn't a UUID
        "13.10.2026.",
        # Form fields and cookies whose values were scrubbed (or never set) are fine (TS-016).
        '<input name="__RequestVerificationToken" type="hidden" value="">',
        '<input name="__VIEWSTATE" type="hidden" value="SCRUBBED" />',
        '<input type="hidden" value="SCRUBBED" name="__VIEWSTATE" />',
        "ASP.NET_SessionId=SCRUBBED; path=/",
        '"nonce": "SCRUBBED"',
        "tag_gid=42",  # a longer name that ends in a cookie's name
    ],
)
def test_scanner_ignores(text: str) -> None:
    assert sensitive_matches(text) == []


@pytest.mark.parametrize(
    ("raw", "email"),
    [
        (b'{"c": "pi\\u0161ite na a&#64;example.rs"}', "a@example.rs"),  # JSON + HTML escapes
        (b'{"c": "ili \\/b@example.rs"}', "b@example.rs"),
        (b"mailto:ana%40example.rs", "ana@example.rs"),  # percent-encoded
        (b'{"url": "https://x.rs/?m=ana%40example.rs"}', "ana@example.rs"),  # inside a JSON value
        # The WordPress feed is a top-level list; `\u0040` hides the @ from the raw view.
        (b'[{"content":{"rendered":"pisati na a\\u0040example.rs"}}]', "a@example.rs"),
        # JSON is decoded from the text as stored: unescaping &quot; first would break it.
        (b'{"t": "&quot;x&quot;", "c": "a\\u0040example.rs"}', "a@example.rs"),
    ],
)
def test_escaped_data_is_still_found(raw: bytes, email: str) -> None:
    assert ("email", email) in hits(scan_texts(raw))


def test_a_cookie_stored_as_a_json_field_is_found() -> None:
    rules = {rule for rule, _ in hits(scan_texts(b'{"request": {"cookie": "theme=dark"}}'))}

    assert "header_line" in rules


def test_json_strings_yields_keys_scalars_and_pairs() -> None:
    texts = list(json_strings({"k": 1, "c": "v", "l": ["x"], "f": 1.5, "b": True}))

    assert {"k", "1", "c", "v", "c: v", "l", "x", "1.5"} <= set(texts)
    assert "True" not in texts  # booleans aren't data


@pytest.mark.parametrize(
    "raw",
    [b"\x1f\x8b\x08\x00compressed", b"a\x00b", "ime@example.rs".encode("utf-16"), b"caf\xe9"],
    ids=["gzip", "nul", "utf-16", "latin-1"],
)
def test_undecodable_data_is_refused(raw: bytes) -> None:
    with pytest.raises(ValueError):  # UnicodeDecodeError is a ValueError
        decode_text(raw)


def test_adversarial_inputs_scan_in_linear_time() -> None:
    # Each input made a pattern quadratic before it was bounded (blank lines for header_line,
    # "-eyJ" runs for jwt, an attribute run without ">" for form_token): minutes at 100 KB.
    inputs = [
        "\n" * 100_000,
        "\n " * 50_000,
        "-eyJ" * 25_000,
        'name="__VIEWSTATE" ' + 'value="a" ' * 10_000,
    ]
    start = time.perf_counter()

    for text in inputs:
        sensitive_matches(text)

    assert time.perf_counter() - start < 2.0
