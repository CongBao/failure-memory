from __future__ import annotations

import sqlite3
from contextlib import suppress
from pathlib import Path

from failure_memory.adapters.storage_permissions import (
    absolute_path,
    ensure_private_directory,
    ensure_private_file,
)

_SQLITE_SETTINGS: tuple[tuple[str, str, object], ...] = (
    ("foreign_keys", "PRAGMA foreign_keys = ON", 1),
    ("busy_timeout", "PRAGMA busy_timeout = 5000", 5000),
    ("journal_mode", "PRAGMA journal_mode = WAL", "wal"),
    ("synchronous", "PRAGMA synchronous = NORMAL", 1),
    ("recursive_triggers", "PRAGMA recursive_triggers = ON", 1),
)


def connect_sqlite(path: Path) -> sqlite3.Connection:
    database = absolute_path(path)
    ensure_private_directory(database.parent, create_parents=True)
    secure_sqlite_files(database)
    connection = sqlite3.connect(database, timeout=5.0, isolation_level=None)
    try:
        ensure_private_file(database, required=False)
        connection.row_factory = sqlite3.Row
        for _name, statement, _expected in _SQLITE_SETTINGS:
            connection.execute(statement)
        for name, _statement, expected in _SQLITE_SETTINGS:
            row = connection.execute(f"PRAGMA {name}").fetchone()
            effective = None if row is None else row[0]
            if effective != expected:
                raise RuntimeError(
                    f"SQLite {name} configuration ineffective: "
                    f"expected {expected!r}, got {effective!r}"
                )
        secure_sqlite_files(database)
        return connection
    except BaseException:
        with suppress(BaseException):
            connection.close()
        raise


def secure_sqlite_files(database: Path) -> None:
    """Harden the main database and any SQLite-owned WAL/SHM sidecars."""
    for path in (
        database,
        database.with_name(f"{database.name}-wal"),
        database.with_name(f"{database.name}-shm"),
    ):
        ensure_private_file(path, required=False)
