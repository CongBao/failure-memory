from __future__ import annotations

import re
from datetime import datetime
from typing import Final

RFC3339_DATE_TIME_PATTERN: Final = (
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"[Tt](?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
    r"(?![\s\S])"
)
_RFC3339_DATE_TIME = re.compile(RFC3339_DATE_TIME_PATTERN)


def parse_rfc3339_date_time(value: str) -> datetime:
    """Parse exactly the RFC3339 wire subset advertised by the MCP schemas."""
    if _RFC3339_DATE_TIME.fullmatch(value) is None:
        raise ValueError("not an RFC3339 date-time")
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    return datetime.fromisoformat(normalized)
