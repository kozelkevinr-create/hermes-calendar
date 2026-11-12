"""Identifier minting for calendar rows.

Internal identifiers are UUIDv7: 48 bits of Unix milliseconds followed by random
bits. They are time-sortable and safe to mint independently in several profile
processes, which is exactly what the two-store design needs (``core.md`` §4.6).
"""

from __future__ import annotations

import secrets
import time
import uuid

_UUID7_VERSION = 0x7
_RFC_4122_VARIANT = 0b10
_MAX_TIMESTAMP_MS = (1 << 48) - 1


def new_uuid7(now_ms: int | None = None) -> str:
    """Return a new UUIDv7 string.

    Args:
        now_ms: Unix milliseconds to embed. Defaults to the current clock. The
            argument exists so tests and deterministic callers can pin the
            timestamp component.

    Raises:
        ValueError: if ``now_ms`` is outside the 48-bit UUIDv7 timestamp range.

    The random component is drawn from :mod:`secrets`; this identifier is not a
    secret, but it must not be predictable across stores.
    """
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    if not 0 <= timestamp <= _MAX_TIMESTAMP_MS:
        raise ValueError("timestamp does not fit in a UUIDv7")

    random_bits = secrets.randbits(74)
    rand_a = random_bits >> 62
    rand_b = random_bits & ((1 << 62) - 1)
    value = (
        (timestamp << 80)
        | (_UUID7_VERSION << 76)
        | (rand_a << 64)
        | (_RFC_4122_VARIANT << 62)
        | rand_b
    )
    return str(uuid.UUID(int=value))
