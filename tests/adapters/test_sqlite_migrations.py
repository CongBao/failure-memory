import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, get_ident

import pytest

from failure_memory.adapters.event_store.sqlite import migrate
from failure_memory.adapters.event_store.sqlite.connection import connect_sqlite
from failure_memory.adapters.event_store.sqlite.migrate import apply_migrations


@pytest.fixture(autouse=True)
def close_migration_test_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep every test-only SQLite handle out of coverage warning output."""
    connections: list[tuple[sqlite3.Connection, int]] = []
    real_connect = connect_sqlite

    def tracked_connect(path: Path) -> sqlite3.Connection:
        connection = real_connect(path)
        connections.append((connection, get_ident()))
        return connection

    monkeypatch.setitem(globals(), "connect_sqlite", tracked_connect)
    yield
    for connection, creator_thread in connections:
        if creator_thread == get_ident():
            connection.close()


def test_initial_migration_creates_core_tables_and_is_idempotent(tmp_path: Path) -> None:
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    assert apply_migrations(connection) == (1, 2, 3, 4)
    assert apply_migrations(connection) == ()
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "schema_migration",
        "capture_attempt",
        "incident",
        "lesson",
        "lesson_version",
        "lesson_head",
        "incident_lesson_relation",
        "adapter_profile",
        "adapter_health_event",
    } <= tables


def test_current_schema_validation_does_not_take_a_writer_lock(
    tmp_path: Path,
) -> None:
    """Would fail if every service startup competed with ordinary SQLite writers."""
    database = tmp_path / "failure-memory.sqlite3"
    blocker = connect_sqlite(database)
    apply_migrations(blocker)
    contender = connect_sqlite(database)
    contender.execute("PRAGMA busy_timeout = 1")
    blocker.execute("BEGIN IMMEDIATE")
    try:
        assert apply_migrations(contender) == ()
    finally:
        blocker.rollback()
        contender.close()
        blocker.close()


def test_pending_migration_retries_busy_transaction_without_partial_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Would fail if pending migrations skipped the bounded SQLite write policy."""
    database = tmp_path / "failure-memory.sqlite3"
    writer = connect_sqlite(database)
    apply_migrations(writer)
    migrations = migrate._migration_files()
    monkeypatch.setattr(
        migrate,
        "_migration_files",
        lambda: [
            *migrations,
            (5, "0005_pending.sql", "CREATE TABLE pending_migration (id INTEGER) STRICT;"),
        ],
    )
    writer.execute("PRAGMA busy_timeout = 1")
    traces: list[str] = []
    writer.set_trace_callback(traces.append)
    blocker = connect_sqlite(database)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(writer)
    finally:
        blocker.rollback()
        blocker.close()

    assert sum(statement == "BEGIN IMMEDIATE" for statement in traces) == 3
    assert (
        writer.execute("SELECT name FROM sqlite_schema WHERE name = 'pending_migration'").fetchone()
        is None
    )
    assert (
        writer.execute("SELECT version FROM schema_migration WHERE version = 5").fetchone() is None
    )
    writer.close()


def test_authoritative_rows_reject_update_and_delete(tmp_path: Path) -> None:
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    apply_migrations(connection)
    connection.execute(
        """
        INSERT INTO lesson(id, schema_version, created_at, source_harness,
                           workspace_fingerprint, session_fingerprint, provenance,
                           redaction_state)
        VALUES ('les_1', 1, '2026-07-29T00:00:00Z', 'test', 'w', NULL, 'test', 'clean')
        """
    )
    with pytest.raises(Exception, match="append-only"):
        connection.execute("UPDATE lesson SET provenance = 'changed' WHERE id = 'les_1'")
    with pytest.raises(Exception, match="append-only"):
        connection.execute("DELETE FROM lesson WHERE id = 'les_1'")


@pytest.mark.parametrize(
    ("table", "row_id"),
    [
        ("capture_attempt", "cap_1"),
        ("incident", "inc_1"),
        ("lesson", "les_1"),
        ("lesson_version", "lev_1"),
        ("incident_lesson_relation", "rel_1"),
    ],
)
def test_insert_or_replace_cannot_overwrite_authoritative_rows(
    tmp_path: Path,
    table: str,
    row_id: str,
) -> None:
    """Would fail if SQLite's implicit REPLACE delete bypassed append-only triggers."""
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    try:
        apply_migrations(connection)
        _insert_incident(connection)
        _insert_lesson(connection, "les_1")
        _insert_lesson_version(connection, "lev_1", "les_1")
        connection.execute(
            """
            INSERT INTO incident_lesson_relation(
                id, schema_version, created_at, source_harness, workspace_fingerprint,
                session_fingerprint, provenance, redaction_state, incident_id, lesson_id,
                lesson_version_id, relation_type, confidence
            ) VALUES (
                'rel_1', 1, '2026-07-29T00:00:00Z', 'test', 'workspace', NULL,
                'test', 'clean', 'inc_1', 'les_1', 'lev_1', 'novel', 1
            )
            """
        )
        before = dict(
            connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        )

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                f"""
                INSERT OR REPLACE INTO {table}
                SELECT * FROM {table} WHERE id = ?
                """,
                (row_id,),
            )

        after = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        assert after is not None
        assert dict(after) == before
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("table", "replacement_id"),
    [
        ("incident", "inc_replacement"),
        ("lesson_version", "lev_replacement"),
        ("incident_lesson_relation", "rel_replacement"),
    ],
)
def test_insert_or_replace_cannot_overwrite_alternate_unique_conflicts(
    tmp_path: Path,
    table: str,
    replacement_id: str,
) -> None:
    """Protect append-only rows when REPLACE collides on a non-primary unique key."""
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    try:
        apply_migrations(connection)
        _insert_incident(connection)
        _insert_lesson(connection, "les_1")
        _insert_lesson_version(connection, "lev_1", "les_1")
        connection.execute(
            """
            INSERT INTO incident_lesson_relation(
                id, schema_version, created_at, source_harness, workspace_fingerprint,
                session_fingerprint, provenance, redaction_state, incident_id, lesson_id,
                lesson_version_id, relation_type, confidence
            ) VALUES (
                'rel_1', 1, '2026-07-29T00:00:00Z', 'test', 'workspace', NULL,
                'test', 'clean', 'inc_1', 'les_1', 'lev_1', 'novel', 1
            )
            """
        )
        original = connection.execute(f"SELECT * FROM {table}").fetchone()
        assert original is not None
        columns = [str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")]
        values = dict(original)
        values["id"] = replacement_id

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                f"""
                INSERT OR REPLACE INTO {table}({", ".join(columns)})
                VALUES ({", ".join("?" for _column in columns)})
                """,
                tuple(values[column] for column in columns),
            )

        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        assert len(rows) == 1
        assert dict(rows[0]) == dict(original)
    finally:
        connection.close()


def test_migration_rejects_changed_checksum(tmp_path: Path) -> None:
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    apply_migrations(connection)
    connection.execute("UPDATE schema_migration SET checksum = 'tampered' WHERE version = 1")

    with pytest.raises(ValueError, match=r"migration checksum mismatch: 0001_initial\.sql"):
        apply_migrations(connection)


def test_failed_migration_rolls_back_schema_and_ledger_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    monkeypatch.setattr(
        migrate,
        "_migration_files",
        lambda: [(1, "0001_broken.sql", "CREATE TABLE partial (id INTEGER); INVALID SQL;")],
    )

    with pytest.raises(sqlite3.OperationalError, match='near "INVALID"'):
        apply_migrations(connection)

    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "partial" not in tables
    assert connection.execute("SELECT * FROM schema_migration").fetchall() == []


def test_migration_rolls_back_schema_on_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Would fail if KeyboardInterrupt left a caller's connection mid-migration."""
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    monkeypatch.setattr(
        migrate,
        "_migration_files",
        lambda: [(1, "0001_interrupted.sql", "SELECT 1;")],
    )

    def interrupt_after_schema_write(value: sqlite3.Connection, _sql: str) -> None:
        value.execute("CREATE TABLE partial (id INTEGER PRIMARY KEY)")
        raise KeyboardInterrupt("migration interrupted")

    monkeypatch.setattr(migrate, "_execute_sql_script", interrupt_after_schema_write)

    with pytest.raises(KeyboardInterrupt, match="migration interrupted"):
        apply_migrations(connection)

    assert connection.in_transaction is False
    assert (
        connection.execute("SELECT name FROM sqlite_master WHERE name = 'partial'").fetchone()
        is None
    )
    assert connection.execute("SELECT * FROM schema_migration").fetchall() == []
    connection.close()


def test_migration_rejects_versions_unknown_to_the_current_package(
    tmp_path: Path,
) -> None:
    """Would fail if a downgraded binary wrote through a newer migration ledger."""
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    try:
        apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO schema_migration(version, name, checksum, applied_at)
            VALUES (999, '0999_future.sql', 'future-checksum', '2026-07-30T00:00:00Z')
            """
        )

        with pytest.raises(ValueError, match=r"unknown applied migration versions: 999"):
            apply_migrations(connection)

        assert connection.in_transaction is False
    finally:
        connection.close()


def test_lesson_head_rejects_a_version_owned_by_a_different_lesson(tmp_path: Path) -> None:
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    apply_migrations(connection)
    _insert_lesson(connection, "les_a")
    _insert_lesson(connection, "les_b")
    _insert_lesson_version(connection, "lev_b", "les_b")

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        connection.execute(
            """
            INSERT INTO lesson_head(lesson_id, lesson_version_id, updated_at)
            VALUES ('les_a', 'lev_b', '2026-07-29T00:00:00Z')
            """
        )


def test_relation_rejects_a_version_owned_by_a_different_lesson(tmp_path: Path) -> None:
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    apply_migrations(connection)
    _insert_lesson(connection, "les_a")
    _insert_lesson(connection, "les_b")
    _insert_lesson_version(connection, "lev_b", "les_b")
    _insert_incident(connection)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        connection.execute(
            """
            INSERT INTO incident_lesson_relation(
                id, schema_version, created_at, source_harness, workspace_fingerprint,
                session_fingerprint, provenance, redaction_state, incident_id, lesson_id,
                lesson_version_id, relation_type, confidence
            ) VALUES (
                'rel_1', 1, '2026-07-29T00:00:00Z', 'test', 'workspace', NULL, 'test', 'clean',
                'inc_1', 'les_a', 'lev_b', 'novel', 1
            )
            """
        )


def test_concurrent_initial_migration_is_applied_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "failure-memory.sqlite3"
    migration_barrier = Barrier(2)
    first_connection_ready = Event()
    allow_first_migration = Event()
    real_migration_files = migrate._migration_files

    def synchronized_migration_files() -> list[tuple[int, str, str]]:
        migration_barrier.wait(timeout=5)
        return real_migration_files()

    monkeypatch.setattr(migrate, "_migration_files", synchronized_migration_files)

    def apply_on_new_connection(wait_before_migrating: bool) -> tuple[int, ...]:
        connection = connect_sqlite(database_path)
        try:
            if wait_before_migrating:
                first_connection_ready.set()
                assert allow_first_migration.wait(timeout=5)
            return apply_migrations(connection)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(apply_on_new_connection, True)
        assert first_connection_ready.wait(timeout=5)
        second = executor.submit(apply_on_new_connection, False)
        allow_first_migration.set()
        results = [first.result(), second.result()]

    assert sorted(results) == [(), (1, 2, 3, 4)]
    connection = connect_sqlite(database_path)
    assert connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0] == 4
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'capture_attempt'"
        ).fetchone()
        is not None
    )


def test_duplicate_migration_versions_fail_before_database_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    monkeypatch.setattr(
        migrate,
        "_migration_files",
        lambda: [
            (1, "0001_first.sql", "CREATE TABLE first_table (id INTEGER PRIMARY KEY) STRICT;"),
            (1, "0001_second.sql", "CREATE TABLE second_table (id INTEGER PRIMARY KEY) STRICT;"),
        ],
    )

    with pytest.raises(ValueError, match="duplicate migration version: 1"):
        apply_migrations(connection)

    assert (
        connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == []
    )


def test_migrations_reject_a_caller_owned_transaction_without_rolling_it_back(
    tmp_path: Path,
) -> None:
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    connection.execute("CREATE TABLE caller_data (id INTEGER PRIMARY KEY)")
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_data VALUES (1)")

    with pytest.raises(ValueError, match="caller-owned transaction"):
        apply_migrations(connection)

    assert connection.in_transaction
    assert connection.execute("SELECT id FROM caller_data").fetchone()[0] == 1
    connection.rollback()
    assert connection.execute("SELECT id FROM caller_data").fetchone() is None


@pytest.mark.parametrize(
    "comment",
    ["-- migration note\n", "/* migration note */"],
    ids=["line-comment", "block-comment"],
)
def test_migration_allows_a_trailing_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, comment: str
) -> None:
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    monkeypatch.setattr(
        migrate,
        "_migration_files",
        lambda: [
            (
                1,
                "0001_comment.sql",
                "CREATE TABLE comment_table (id INTEGER PRIMARY KEY) STRICT;\n" + comment,
            )
        ],
    )

    assert apply_migrations(connection) == (1,)
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE name = 'comment_table'"
    ).fetchone()


def test_incomplete_migration_rolls_back_schema_and_ledger_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    monkeypatch.setattr(
        migrate,
        "_migration_files",
        lambda: [
            (
                1,
                "0001_incomplete.sql",
                "CREATE TABLE partial (id INTEGER); CREATE TABLE incomplete (id INTEGER",
            )
        ],
    )

    with pytest.raises(ValueError, match="incomplete migration SQL"):
        apply_migrations(connection)

    tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert [row[0] for row in tables] == ["schema_migration"]


def _insert_lesson(connection: sqlite3.Connection, lesson_id: str) -> None:
    connection.execute(
        """
        INSERT INTO lesson(
            id, schema_version, created_at, source_harness, workspace_fingerprint,
            session_fingerprint, provenance, redaction_state
        ) VALUES (?, 1, '2026-07-29T00:00:00Z', 'test', 'workspace', NULL, 'test', 'clean')
        """,
        (lesson_id,),
    )


def _insert_lesson_version(connection: sqlite3.Connection, version_id: str, lesson_id: str) -> None:
    connection.execute(
        """
        INSERT INTO lesson_version(
            id, schema_version, created_at, source_harness, workspace_fingerprint,
            session_fingerprint, provenance, redaction_state, lesson_id, version_number,
            lifecycle_state, signature, title, rule, prevention_action, verification_action,
            applicability, counterexamples
        ) VALUES (
            ?, 1, '2026-07-29T00:00:00Z', 'test', 'workspace', NULL, 'test', 'clean', ?, 1,
            'proposed', 'signature', 'title', 'rule', 'prevent', 'verify', 'all', 'none'
        )
        """,
        (version_id, lesson_id),
    )


def _insert_incident(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO capture_attempt(
            id, schema_version, created_at, source_harness, workspace_fingerprint,
            session_fingerprint, provenance, redaction_state, summary, classification, decision,
            confidence, reason_codes_json, expectation_source, expectation_established_at,
            observed_outcome_at, failure_portion_summary, policy_version
        ) VALUES (
            'cap_1', 1, '2026-07-29T00:00:00Z', 'test', 'workspace', NULL, 'test', 'clean',
            'summary', 'real_failure', 'accept', 1, '[]', 'test', NULL,
            '2026-07-29T00:00:00Z', NULL, 'v1'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO incident(
            id, schema_version, created_at, source_harness, workspace_fingerprint,
            session_fingerprint, provenance, redaction_state, capture_attempt_id,
            outcome_summary, expected_invariant, controllable_cause, material_impact,
            recurrence_risk
        ) VALUES (
            'inc_1', 1, '2026-07-29T00:00:00Z', 'test', 'workspace', NULL, 'test', 'clean',
            'cap_1', 'outcome', 'invariant', 'cause', 'impact', 'risk'
        )
        """
    )
