"""Strict JSON decoding shared by local protocol boundaries."""

from __future__ import annotations

import json
import math
from typing import TextIO, cast


def decode_json(value: str) -> object:
    """Decode one JSON value without duplicate keys or non-finite numbers."""
    return cast(
        object,
        json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
            parse_float=_parse_finite_float,
        ),
    )


def load_json(stream: TextIO) -> object:
    """Load one strict JSON value from a text stream."""
    return cast(
        object,
        json.load(
            stream,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
            parse_float=_parse_finite_float,
        ),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed
