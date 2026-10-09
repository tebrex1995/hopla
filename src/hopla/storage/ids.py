"""Run and audit ids: ULIDs (ADR-0004 S8), with no extra dependency.

48 bits of milliseconds since the epoch, then 80 random bits, in Crockford base32: 26
characters that sort by creation time. Ties at the same millisecond are broken by the
random part, which S8 uses only as a last resort after the store's LastModified.
"""

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from hopla.core.time import require_aware

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


def new_ulid(now: datetime, rand: Callable[[int], bytes] = os.urandom) -> str:
    millis = (require_aware(now, "now") - _EPOCH) // _MILLISECOND  # exact, no float
    if not 0 <= millis < 1 << 48:
        raise ValueError(f"time out of ULID range: {now!r}")
    value = (millis << 80) | int.from_bytes(rand(10), "big")
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))
