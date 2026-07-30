from __future__ import annotations

import sqlite3

SQLITE_BUSY_RETRY_DELAYS_SECONDS = (0.01, 0.05)


def is_sqlite_busy_error(error: sqlite3.Error) -> bool:
    """Recognize primary and extended SQLite BUSY/LOCKED result codes."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }
