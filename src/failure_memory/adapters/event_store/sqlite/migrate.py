from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import UTC, datetime
from importlib.resources import files

from failure_memory.adapters.event_store.sqlite.errors import (
    SQLITE_BUSY_RETRY_DELAYS_SECONDS,
    is_sqlite_busy_error,
)

_PACKAGE = "failure_memory.adapters.event_store.sqlite.migrations"


def _migration_files() -> list[tuple[int, str, str]]:
    result: list[tuple[int, str, str]] = []
    for item in files(_PACKAGE).iterdir():
        if item.name.endswith(".sql"):
            version = _version_from_filename(item.name)
            result.append((version, item.name, item.read_text(encoding="utf-8")))
    return sorted(result, key=lambda migration: migration[0])


def _version_from_filename(name: str) -> int:
    prefix, separator, remainder = name.partition("_")
    if not separator or not prefix.isdecimal() or not remainder or not name.endswith(".sql"):
        raise ValueError(f"invalid migration filename: {name}")
    return int(prefix)


def _validate_migrations(migrations: list[tuple[int, str, str]]) -> None:
    seen_versions: set[int] = set()
    for version, name, _sql in migrations:
        if _version_from_filename(name) != version:
            raise ValueError(f"migration version does not match filename: {name}")
        if version in seen_versions:
            raise ValueError(f"duplicate migration version: {version}")
        seen_versions.add(version)


def _execute_sql_script(connection: sqlite3.Connection, sql: str) -> None:
    statement = ""
    for character in sql:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if _contains_sql_tokens(statement):
        raise ValueError("incomplete migration SQL")


def _contains_sql_tokens(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index].isspace():
            index += 1
        elif value.startswith("--", index):
            newline = value.find("\n", index + 2)
            index = len(value) if newline == -1 else newline + 1
        elif value.startswith("/*", index):
            end = value.find("*/", index + 2)
            index = len(value) if end == -1 else end + 2
        else:
            return True
    return False


def _migration_checksums(
    migrations: list[tuple[int, str, str]],
) -> dict[int, tuple[str, str]]:
    return {
        version: (name, hashlib.sha256(sql.encode("utf-8")).hexdigest())
        for version, name, sql in migrations
    }


def _migration_ledger_exists(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM sqlite_schema
            WHERE type = 'table' AND name = 'schema_migration'
            """
        ).fetchone()
        is not None
    )


def _applied_migrations(connection: sqlite3.Connection) -> dict[int, str]:
    return {
        int(row["version"]): str(row["checksum"])
        for row in connection.execute("SELECT version, checksum FROM schema_migration")
    }


def _validate_applied_migrations(
    connection: sqlite3.Connection,
    applied: dict[int, str],
    expected: dict[int, tuple[str, str]],
) -> None:
    unknown_versions = sorted(set(applied) - set(expected))
    if unknown_versions:
        known_maximum = max(expected, default=0)
        expected_missing = sorted(set(expected) - set(applied))
        if expected_missing:
            rendered = ", ".join(str(version) for version in expected_missing)
            raise ValueError(f"known migrations missing before newer schema: {rendered}")
        if not _compatible_unknown_migrations(connection, unknown_versions, known_maximum):
            rendered = ", ".join(str(version) for version in unknown_versions)
            raise ValueError(f"unknown applied migration versions: {rendered}")
    for version, applied_checksum in applied.items():
        if version not in expected:
            continue
        name, expected_checksum = expected[version]
        if applied_checksum != expected_checksum:
            raise ValueError(f"migration checksum mismatch: {name}")


def _compatible_unknown_migrations(
    connection: sqlite3.Connection,
    unknown_versions: list[int],
    known_maximum: int,
) -> bool:
    capability_table = connection.execute(
        """
        SELECT 1
        FROM sqlite_schema
        WHERE type = 'table' AND name = 'store_schema_capability'
        """
    ).fetchone()
    if capability_table is None:
        return False
    rows = connection.execute(
        """
        SELECT migration_version, schema_kind, minimum_writer_migration
        FROM store_schema_capability
        WHERE migration_version IN ({})
        """.format(",".join("?" for _version in unknown_versions)),
        tuple(unknown_versions),
    ).fetchall()
    capabilities = {
        int(row["migration_version"]): (
            str(row["schema_kind"]),
            int(row["minimum_writer_migration"]),
        )
        for row in rows
    }
    for version in unknown_versions:
        capability = capabilities.get(version)
        if capability is None or capability[0] != "additive" or capability[1] > known_maximum:
            return False
    return True


def _apply_migrations_once(
    connection: sqlite3.Connection,
    migrations: list[tuple[int, str, str]],
    expected: dict[int, tuple[str, str]],
) -> tuple[int, ...]:
    if _migration_ledger_exists(connection):
        applied = _applied_migrations(connection)
        _validate_applied_migrations(connection, applied, expected)
        if set(applied) == set(expected):
            return ()
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        ) STRICT
        """
    )
    completed: list[int] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        applied = _applied_migrations(connection)
        _validate_applied_migrations(connection, applied, expected)
        for version, name, sql in migrations:
            checksum = expected[version][1]
            if version in applied:
                continue
            _execute_sql_script(connection, sql)
            connection.execute(
                """
                INSERT INTO schema_migration(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (version, name, checksum, datetime.now(UTC).isoformat()),
            )
            completed.append(version)
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    return tuple(completed)


def apply_migrations(connection: sqlite3.Connection) -> tuple[int, ...]:
    migrations = _migration_files()
    _validate_migrations(migrations)
    expected = _migration_checksums(migrations)
    if connection.in_transaction:
        raise ValueError("cannot apply migrations within a caller-owned transaction")
    for attempt in range(len(SQLITE_BUSY_RETRY_DELAYS_SECONDS) + 1):
        try:
            return _apply_migrations_once(connection, migrations, expected)
        except sqlite3.Error as error:
            if not is_sqlite_busy_error(error):
                raise
            if connection.in_transaction:
                connection.rollback()
            if attempt == len(SQLITE_BUSY_RETRY_DELAYS_SECONDS):
                raise
            time.sleep(SQLITE_BUSY_RETRY_DELAYS_SECONDS[attempt])
    raise AssertionError("unreachable migration retry state")
