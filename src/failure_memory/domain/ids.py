from __future__ import annotations

import re
import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PREFIX = re.compile(r"^[a-z][a-z0-9_]{1,15}$")


def _encode_crockford(value: int, width: int) -> str:
    chars = ["0"] * width
    for index in range(width - 1, -1, -1):
        value, remainder = divmod(value, 32)
        chars[index] = _ALPHABET[remainder]
    if value:
        raise ValueError("value does not fit requested width")
    return "".join(chars)


def new_id(
    prefix: str,
    *,
    timestamp_ms: int | None = None,
    randomness: bytes | None = None,
) -> str:
    if _PREFIX.fullmatch(prefix) is None:
        raise ValueError("prefix must match [a-z][a-z0-9_]{1,15}")
    millis = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    entropy = secrets.token_bytes(10) if randomness is None else randomness
    if not 0 <= millis < 2**48:
        raise ValueError("timestamp_ms must fit 48 bits")
    if len(entropy) != 10:
        raise ValueError("randomness must be exactly 10 bytes")
    encoded = _encode_crockford((millis << 80) | int.from_bytes(entropy), 26)
    return f"{prefix}_{encoded}"
