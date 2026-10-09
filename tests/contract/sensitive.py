"""A scanner for personal data, secrets and session tokens in committed test data (04 §6).

It catches what a pattern can: email addresses, phone numbers, IP addresses, tokens and
session cookies. It can't catch base64 secrets, obfuscated addresses ("ime [at] domen") or
people's names, so new test data still gets a human read before it's committed.

The rules are tuned against what the sources' bodies contain: dd.MM.yyyy dates, HH:MM times,
train and station numbers, script versions and WordPress JSON. Known false-positive traps if a
new source brings them: `?ver=<md5>` and hashed bundle names (hex_token), 4-part versions such
as 10.0.0.1 (ipv4), a public Maps `?key=` (token_param). Allow-list a reviewed exact string
rather than weaken a rule.
"""

import contextlib
import html
import ipaddress
import json
import re
import urllib.parse
from collections.abc import Iterator

SCRUBBED = "SCRUBBED"  # what the scrubber writes in place of a token value (04 §6)

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_SEP = r"[\s./()-]*"  # \s covers the NBSP WordPress writes as &nbsp;
_FORM_TOKENS = r"__RequestVerificationToken|__VIEWSTATE|__EVENTVALIDATION|_wpnonce"
_COOKIES = (
    r"ASP\.NET_SessionId|\.ASPXAUTH|PHPSESSID|mc_session_ids(?:\[[^\]=]*\])*"
    r"|wordpress_logged_in_\w*|_ga|_gid"
)
SENSITIVE_PATTERNS = {
    # Serbian names are Unicode (đorđe@…, petrović@…). `logo@2x.png` is a file name.
    "email": re.compile(
        r"(?<![\w.%+-])[\w.%+-]+@(?!\d+x\.(?:png|jpe?g|webp|svg)\b)"
        r"[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}"
    ),
    # Serbian mobiles in any grouping (064 123 45 67, 064/12-34-567, +381 (0)64 …, 381641234567).
    # A digit must follow "06" directly, so dates (06.10.2026) and times (06:45) don't match;
    # landlines (011 360 28 99) are the operator's public numbers and don't match either.
    "phone": re.compile(
        rf"(?<![\d+])(?:(?:\+|00)?381{_SEP}(?:\(?0\)?{_SEP})?|0)6\d(?:{_SEP}\d){{6,7}}(?!\d)"
    ),
    "ipv4": re.compile(rf"(?<![\d.]){_OCTET}(?:\.{_OCTET}){{3}}(?!\.?\d)"),
    "hex_token": re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])"),
    "uuid": re.compile(
        r"(?<![0-9A-Fa-f-])[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f-])"
    ),
    "jwt": re.compile(r"(?<![\w-])eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"),
    "bearer": re.compile(r"(?i)\bbearer\s+[\w.~+/=-]{8,}"),
    # A form or JSON field that carries a live anti-forgery token. The name alone is fine:
    # login and challenge fixtures (TS-016) keep the field with a scrubbed or empty value.
    # The attribute gap is bounded, so a long run without ">" can't make the scan quadratic.
    "form_token": re.compile(
        rf'(?i)name="(?:{_FORM_TOKENS})"[^>]{{0,1024}}\bvalue="(?!{SCRUBBED}")[^"]+"'
        rf'|value="(?!{SCRUBBED}")[^"]+"[^>]{{0,1024}}\bname="(?:{_FORM_TOKENS})"'
        rf'|"(?:{_FORM_TOKENS}|nonce)"\s*:\s*"(?!{SCRUBBED}")[^"]+"'
    ),
    "session_cookie": re.compile(rf"(?<![\w.])(?:{_COOKIES})=(?!{SCRUBBED}\b)[^;\s\"&]+"),
    "token_param": re.compile(
        r"(?i)[?&#;](?:access_token|id_token|token|sid|jsessionid|session_?id|session|auth"
        r"|api_?key|key|sig|signature|nonce)="
    ),
    # [ \t], not \s: \s would cross newlines and make a run of blank lines quadratic.
    "header_line": re.compile(r"(?im)^[ \t]*(?:set-cookie|cookie|authorization)[ \t]*:"),
}
# IPv6 candidates must hold a hex digit and pass ipaddress, so times (12:36:18), CSS
# (a::before) and a bare "::" don't count.
_IPV6_CANDIDATE = re.compile(r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")


def sensitive_matches(text: str) -> list[tuple[str, str]]:
    """Every (rule, match) for personal data, secrets or session tokens in `text`."""
    found = [
        (rule, m.group()) for rule, rx in SENSITIVE_PATTERNS.items() for m in rx.finditer(text)
    ]
    for m in _IPV6_CANDIDATE.finditer(text):
        candidate = m.group()
        if not any(c in "0123456789abcdefABCDEF" for c in candidate):
            continue
        with contextlib.suppress(ValueError):  # not an address
            if ipaddress.ip_address(candidate).version == 6:
                found.append(("ipv6", candidate))
    return found


def text_views(text: str) -> list[str]:
    """`text` as stored, HTML-unescaped and percent-decoded: escapes hide data from a plain scan."""
    return [text, html.unescape(text), urllib.parse.unquote(text)]


def json_strings(value: object) -> Iterator[str]:
    """Every key and scalar value of a decoded JSON document (as text), plus each `key: value`.

    Numbers are included (a phone or an IPv4 can be stored as an int). The pair form lets the
    header rule see `{"cookie": "…"}` the way it sees a header line.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield str(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            if isinstance(item, str):
                yield f"{key}: {item}"
            yield from json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_strings(item)


def decode_text(raw: bytes) -> str:
    """The data as UTF-8 text. Binary or other encodings can't be scanned, so they're refused."""
    if raw.startswith(b"\x1f\x8b") or b"\x00" in raw:
        raise ValueError("gzip or binary content: store test data decompressed, as UTF-8 text")
    return raw.decode("utf-8")  # strict: a wrong encoding would hide data from every rule


def scan_texts(raw: bytes) -> list[str]:
    """What to scan in a body: its text views and, for JSON, the views of every decoded value."""
    text = decode_text(raw)
    views = text_views(text)
    with contextlib.suppress(ValueError):  # not JSON
        views += [view for value in json_strings(json.loads(text)) for view in text_views(value)]
    return views


def hits(texts: list[str]) -> set[tuple[str, str]]:
    return {hit for text in texts for hit in sensitive_matches(text)}
