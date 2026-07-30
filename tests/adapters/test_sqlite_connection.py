from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from failure_memory.adapters.event_store.sqlite.connection import connect_sqlite


def test_connect_sqlite_applies_every_effective_runtime_setting(tmp_path: Path) -> None:
    """Would fail if a requested SQLite setting was silently ignored."""
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("setting", "effective"),
    [
        ("foreign_keys", 0),
        ("busy_timeout", 4999),
        ("journal_mode", "delete"),
        ("synchronous", 2),
        ("recursive_triggers", 0),
    ],
)
def test_connect_sqlite_rejects_ineffective_configuration_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    effective: object,
) -> None:
    """Would fail if SQLite returned a different effective setting without detection."""

    class Result:
        def __init__(self, value: object = None) -> None:
            self.value = value

        def fetchone(self) -> tuple[object]:
            return (self.value,)

    expected: dict[str, object] = {
        "foreign_keys": 1,
        "busy_timeout": 5000,
        "journal_mode": "wal",
        "synchronous": 1,
        "recursive_triggers": 1,
    }
    expected[setting] = effective

    class Connection:
        row_factory: object = None
        close_calls = 0

        def execute(self, statement: str) -> Result:
            pragma = statement.removeprefix("PRAGMA ").split(" ", 1)[0]
            return Result(expected.get(pragma))

        def close(self) -> None:
            self.close_calls += 1

    connection = Connection()
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connection)

    with pytest.raises(RuntimeError, match=setting):
        connect_sqlite(tmp_path / "failure-memory.sqlite3")

    assert connection.close_calls == 1


def test_connect_sqlite_hardens_owned_directory_database_and_sidecars(
    tmp_path: Path,
) -> None:
    """Would fail if a permissive umask exposed Failure Memory's SQLite files."""
    database_parent = tmp_path / "owned"
    database_parent.mkdir(mode=0o777)
    database_parent.chmod(0o777)
    database = database_parent / "failure-memory.sqlite3"
    previous_umask = os.umask(0)
    try:
        connection = connect_sqlite(database)
        connection.execute("CREATE TABLE privacy_probe (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO privacy_probe VALUES (1)")
        modes = {
            path.name: stat.S_IMODE(path.lstat().st_mode)
            for path in database_parent.iterdir()
            if path.is_file()
        }
    finally:
        os.umask(previous_umask)
        connection.close()

    assert stat.S_IMODE(database_parent.stat().st_mode) == 0o700
    assert modes["failure-memory.sqlite3"] == 0o600
    for sidecar in ("failure-memory.sqlite3-wal", "failure-memory.sqlite3-shm"):
        if sidecar in modes:
            assert modes[sidecar] == 0o600


def test_connect_sqlite_rejects_database_symlink_without_hardening_target(
    tmp_path: Path,
) -> None:
    """Would fail if a pre-existing database symlink redirected chmod or SQLite writes."""
    target = tmp_path / "unrelated.sqlite3"
    target.write_bytes(b"unrelated")
    target.chmod(0o644)
    owned = tmp_path / "owned"
    owned.mkdir()
    database = owned / "failure-memory.sqlite3"
    database.symlink_to(target)

    with pytest.raises(OSError, match="symbolic link"):
        connect_sqlite(database)

    assert target.read_bytes() == b"unrelated"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("failure_position", "failure_statement"),
    [
        (1, "PRAGMA foreign_keys = ON"),
        (2, "PRAGMA busy_timeout = 5000"),
        (3, "PRAGMA journal_mode = WAL"),
        (4, "PRAGMA synchronous = NORMAL"),
        (5, "PRAGMA recursive_triggers = ON"),
    ],
    ids=[
        "foreign-keys",
        "busy-timeout",
        "journal-mode",
        "synchronous",
        "recursive-triggers",
    ],
)
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_connect_sqlite_closes_connection_when_pragma_configuration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_position: int,
    failure_statement: str,
    error_type: type[BaseException],
) -> None:
    failure = error_type("configuration stopped")

    class Connection:
        def __init__(self) -> None:
            self.row_factory: object = None
            self.execute_calls = 0
            self.close_calls = 0
            self.statements: list[str] = []

        def execute(self, statement: str) -> None:
            self.execute_calls += 1
            self.statements.append(statement)
            if self.execute_calls == failure_position:
                raise failure

        def close(self) -> None:
            self.close_calls += 1

    connection = Connection()
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connection)

    with pytest.raises(error_type) as raised:
        connect_sqlite(tmp_path / "failure-memory.sqlite3")

    assert raised.value is failure
    assert connection.execute_calls == failure_position
    assert connection.statements[-1] == failure_statement
    assert connection.close_calls == 1


def test_connect_sqlite_preserves_configuration_failure_when_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = KeyboardInterrupt("configuration stopped")

    class Connection:
        def __init__(self) -> None:
            self.row_factory: object = None
            self.close_calls = 0

        def execute(self, statement: str) -> None:
            raise failure

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("secondary close failure")

    connection = Connection()
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connection)

    with pytest.raises(KeyboardInterrupt) as raised:
        connect_sqlite(tmp_path / "failure-memory.sqlite3")

    assert raised.value is failure
    assert connection.close_calls == 1
