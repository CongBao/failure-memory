"""Newline-delimited UTF-8 JSON-RPC transport helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TextIO

from failure_memory.json_codec import decode_json as decode_json


def read_message(stream: TextIO) -> dict[str, object] | None:
    """Read exactly one JSON object line, returning ``None`` at EOF."""
    line = stream.readline()
    if line == "":
        return None
    value = decode_json(line)
    if not isinstance(value, dict):
        raise ValueError("JSON-RPC message must be an object")
    return value


def write_message(stream: TextIO, message: Mapping[str, object]) -> None:
    """Write and flush one compact JSON-RPC message line."""
    stream.write(json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    stream.write("\n")
    stream.flush()
