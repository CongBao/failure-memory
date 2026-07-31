from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from failure_memory.domain.ids import new_id
from failure_memory.domain.store import SourceStore, StoreImportPlan, StoreImportResult

_CORE_TABLES = (
    "capture_attempt",
    "incident",
    "lesson",
    "lesson_version",
    "lesson_head",
    "incident_lesson_relation",
    "lesson_signature_alias",
    "adapter_profile",
    "adapter_health_event",
)
_RECALL_TABLES = (
    "retrieval_profile_snapshot",
    "recall_attempt",
    "recall_candidate",
    "recall_selection",
    "recall_outcome_event",
    "recall_miss_event",
)
_LEARNING_TABLES = (
    "lesson_lifecycle_event",
    "ranking_experiment",
    "lesson_cluster_run",
    "lesson_cluster_member",
    "lesson_generalization_proposal",
    "failure_generalization_review",
    "failure_generalization_decision_event",
    "lesson_generalization_proposal_review",
    "lesson_generalization_source",
    "learning_evaluation_run",
)
_CAUSAL_TABLES = (
    "failure_causal_assessment",
    "failure_causal_factor",
    "failure_repair_recommendation",
    "failure_causal_review_relation",
    "failure_causal_incident_relation",
    "failure_repair_outcome_event",
)
_FAST_RECORDING_TABLES = ("failure_recording_operation",)
_TABLES = (
    *_CORE_TABLES,
    *_RECALL_TABLES,
    *_LEARNING_TABLES,
    *_CAUSAL_TABLES,
    *_FAST_RECORDING_TABLES,
)


class GlobalStoreImporter:
    """Copy immutable records from a source ledger without modifying that source."""

    def __init__(
        self,
        target: sqlite3.Connection,
        target_database: Path,
        target_data_root: Path,
    ) -> None:
        self.target = target
        self.target_database = target_database.resolve()
        self.target_data_root = target_data_root.resolve()

    def inspect(self, source_database: Path) -> SourceStore:
        source_path = source_database.expanduser().resolve()
        if source_path == self.target_database:
            raise ValueError("source store is the active global store")
        source = _connect_read_only(source_path)
        try:
            schema_version = _schema_version(source)
            tables = _existing_tables(source, _TABLES)
            counts = {table: _count(source, table) for table in tables}
            source_store_id, fingerprint_domain = _source_identity(source_path)
            content_fingerprint = _logical_fingerprint(source, tables)
            return SourceStore(
                database=source_path,
                source_store_id=source_store_id,
                fingerprint_domain=fingerprint_domain,
                schema_version=schema_version,
                content_fingerprint=content_fingerprint,
                counts=counts,
            )
        finally:
            source.close()

    def plan(self, source_database: Path) -> StoreImportPlan:
        source_info = self.inspect(source_database)
        imported = self.target.execute(
            """
            SELECT 1
            FROM source_store_import
            WHERE source_store_id = ? AND source_content_fingerprint = ?
            """,
            (source_info.source_store_id, source_info.content_fingerprint),
        ).fetchone()
        source = _connect_read_only(source_info.database)
        try:
            importable: dict[str, int] = {}
            skipped: dict[str, int] = {}
            conflicts: list[str] = (
                ["schema:source_newer_than_target"]
                if source_info.schema_version > _schema_version(self.target)
                else []
            )
            for table in _existing_tables(source, _TABLES):
                if table == "retrieval_profile_snapshot":
                    importable[table], skipped[table], table_conflicts = (
                        self._plan_retrieval_profiles(source)
                    )
                else:
                    importable[table], skipped[table], table_conflicts = self._plan_table(
                        source, table
                    )
                conflicts.extend(table_conflicts)
            return StoreImportPlan(
                source=source_info,
                target_store_id=_target_store_id(self.target_data_root),
                already_imported=imported is not None,
                importable_counts=importable,
                skipped_counts=skipped,
                conflicts=tuple(conflicts),
            )
        finally:
            source.close()

    def apply(self, source_database: Path) -> StoreImportResult:
        plan = self.plan(source_database)
        if plan.already_imported:
            return StoreImportResult(
                import_id="",
                source_store_id=plan.source.source_store_id,
                content_fingerprint=plan.source.content_fingerprint,
                imported_counts={table: 0 for table in plan.importable_counts},
                skipped_counts=dict(plan.skipped_counts),
                already_imported=True,
            )
        if plan.conflicts:
            raise ValueError("source store contains record ID collisions")
        source = _connect_read_only(plan.source.database)
        imported_counts: dict[str, int] = {}
        skipped_counts: dict[str, int] = {}
        import_id = new_id("imp")
        try:
            self.target.execute("BEGIN IMMEDIATE")
            profile_map = self._import_retrieval_profiles(source, imported_counts, skipped_counts)
            for table in _CORE_TABLES:
                if table == "retrieval_profile_snapshot" or not _table_exists(source, table):
                    continue
                self._import_table(source, table, imported_counts, skipped_counts)
            for table in _RECALL_TABLES:
                if table == "retrieval_profile_snapshot" or not _table_exists(source, table):
                    continue
                overrides = (
                    {"retrieval_profile_id": profile_map} if table == "recall_attempt" else None
                )
                self._import_table(
                    source,
                    table,
                    imported_counts,
                    skipped_counts,
                    value_maps=overrides,
                )
            for table in _LEARNING_TABLES:
                if not _table_exists(source, table):
                    continue
                overrides = (
                    {"retrieval_profile_id": profile_map} if table == "lesson_cluster_run" else None
                )
                self._import_table(
                    source,
                    table,
                    imported_counts,
                    skipped_counts,
                    value_maps=overrides,
                )
            for table in _CAUSAL_TABLES:
                if not _table_exists(source, table):
                    continue
                self._import_table(
                    source,
                    table,
                    imported_counts,
                    skipped_counts,
                )
            for table in _FAST_RECORDING_TABLES:
                if not _table_exists(source, table):
                    continue
                self._import_table(
                    source,
                    table,
                    imported_counts,
                    skipped_counts,
                )
            self.target.execute(
                """
                INSERT INTO source_store_import(
                    id, schema_version, created_at, source_store_id,
                    source_content_fingerprint, source_schema_version,
                    source_fingerprint_domain, imported_counts_json,
                    skipped_counts_json, state
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 'completed')
                """,
                (
                    import_id,
                    datetime.now(UTC).isoformat(),
                    plan.source.source_store_id,
                    plan.source.content_fingerprint,
                    plan.source.schema_version,
                    plan.source.fingerprint_domain,
                    json.dumps(imported_counts, sort_keys=True, separators=(",", ":")),
                    json.dumps(skipped_counts, sort_keys=True, separators=(",", ":")),
                ),
            )
            self.target.execute("COMMIT")
        except BaseException:
            if self.target.in_transaction:
                self.target.execute("ROLLBACK")
            raise
        finally:
            source.close()
        return StoreImportResult(
            import_id=import_id,
            source_store_id=plan.source.source_store_id,
            content_fingerprint=plan.source.content_fingerprint,
            imported_counts=imported_counts,
            skipped_counts=skipped_counts,
            already_imported=False,
        )

    def status(self) -> dict[str, object]:
        rows = self.target.execute(
            """
            SELECT id, created_at, source_store_id, source_content_fingerprint,
                   source_schema_version, imported_counts_json, skipped_counts_json
            FROM source_store_import
            ORDER BY created_at, id
            """
        ).fetchall()
        return {
            "scope": "global_personal",
            "target_store_id": _target_store_id(self.target_data_root),
            "import_count": len(rows),
            "imports": [
                {
                    "import_id": str(row["id"]),
                    "created_at": str(row["created_at"]),
                    "source_store_id": str(row["source_store_id"]),
                    "content_fingerprint": str(row["source_content_fingerprint"]),
                    "source_schema_version": int(row["source_schema_version"]),
                    "imported_counts": json.loads(str(row["imported_counts_json"])),
                    "skipped_counts": json.loads(str(row["skipped_counts_json"])),
                }
                for row in rows
            ],
        }

    def _plan_table(self, source: sqlite3.Connection, table: str) -> tuple[int, int, list[str]]:
        columns = _columns(source, table)
        primary_key = _primary_key(columns)
        importable = 0
        skipped = 0
        conflicts: list[str] = []
        for row in _rows(source, table, primary_key):
            existing = self.target.execute(
                f"SELECT * FROM {table} WHERE {primary_key} = ?",
                (row[primary_key],),
            ).fetchone()
            if existing is None:
                importable += 1
            elif _shared_row_equal(row, existing):
                skipped += 1
            else:
                conflicts.append(f"{table}:{row[primary_key]}")
        return importable, skipped, conflicts

    def _plan_retrieval_profiles(self, source: sqlite3.Connection) -> tuple[int, int, list[str]]:
        if not _table_exists(source, "retrieval_profile_snapshot"):
            return 0, 0, []
        importable = 0
        skipped = 0
        conflicts: list[str] = []
        for row in _rows(source, "retrieval_profile_snapshot", "id"):
            by_id = self.target.execute(
                "SELECT * FROM retrieval_profile_snapshot WHERE id = ?",
                (row["id"],),
            ).fetchone()
            by_config = self.target.execute(
                """
                SELECT * FROM retrieval_profile_snapshot
                WHERE config_fingerprint = ?
                """,
                (row["config_fingerprint"],),
            ).fetchone()
            existing = by_id or by_config
            if existing is None:
                importable += 1
            elif _profile_equal(row, existing):
                skipped += 1
            else:
                conflicts.append(f"retrieval_profile_snapshot:{row['id']}")
        return importable, skipped, conflicts

    def _import_retrieval_profiles(
        self,
        source: sqlite3.Connection,
        imported: dict[str, int],
        skipped: dict[str, int],
    ) -> dict[object, object]:
        table = "retrieval_profile_snapshot"
        mapping: dict[object, object] = {}
        imported[table] = 0
        skipped[table] = 0
        if not _table_exists(source, table):
            return mapping
        for row in _rows(source, table, "id"):
            existing = self.target.execute(
                "SELECT id FROM retrieval_profile_snapshot WHERE config_fingerprint = ?",
                (row["config_fingerprint"],),
            ).fetchone()
            if existing is not None:
                mapping[row["id"]] = existing["id"]
                skipped[table] += 1
                continue
            self._insert_row(table, row)
            mapping[row["id"]] = row["id"]
            imported[table] += 1
        return mapping

    def _import_table(
        self,
        source: sqlite3.Connection,
        table: str,
        imported: dict[str, int],
        skipped: dict[str, int],
        *,
        value_maps: dict[str, dict[object, object]] | None = None,
    ) -> None:
        columns = _columns(source, table)
        primary_key = _primary_key(columns)
        imported[table] = 0
        skipped[table] = 0
        for source_row in _rows(source, table, primary_key):
            row = dict(source_row)
            for column, mapping in (value_maps or {}).items():
                if column in row and row[column] in mapping:
                    row[column] = mapping[row[column]]
            existing = self.target.execute(
                f"SELECT * FROM {table} WHERE {primary_key} = ?",
                (row[primary_key],),
            ).fetchone()
            if existing is not None:
                if not _shared_row_equal(row, existing):
                    raise ValueError(f"record collision in {table}")
                skipped[table] += 1
                continue
            self._insert_row(table, row)
            imported[table] += 1

    def _insert_row(self, table: str, row: sqlite3.Row | dict[str, object]) -> None:
        values = dict(row)
        columns = tuple(values)
        placeholders = ",".join("?" for _ in columns)
        self.target.execute(
            f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
            tuple(values[column] for column in columns),
        )


def discover_legacy_databases(
    target_database: Path,
    *,
    home: Path | None = None,
    env_plugin_data: str | None = None,
    env_claude_plugin_data: str | None = None,
) -> tuple[Path, ...]:
    user_home = Path.home() if home is None else home
    candidates = [
        user_home
        / ".codex"
        / "plugin-data"
        / "failure-memory"
        / "adapters"
        / "event-store"
        / "sqlite"
        / "primary"
        / "failure-memory.sqlite3",
    ]
    for raw in (env_plugin_data, env_claude_plugin_data):
        if raw:
            candidates.append(
                Path(raw)
                / "failure-memory"
                / "adapters"
                / "event-store"
                / "sqlite"
                / "primary"
                / "failure-memory.sqlite3"
            )
    target = target_database.resolve()
    return tuple(
        dict.fromkeys(
            path.resolve() for path in candidates if path.is_file() and path.resolve() != target
        )
    )


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("source store database does not exist")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _existing_tables(connection: sqlite3.Connection, candidates: Iterable[str]) -> tuple[str, ...]:
    return tuple(table for table in candidates if _table_exists(connection, table))


def _schema_version(connection: sqlite3.Connection) -> int:
    if not _table_exists(connection, "schema_migration"):
        raise ValueError("source store has no migration ledger")
    row = connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()
    if row is None or row[0] is None:
        raise ValueError("source store has no applied schema version")
    return int(row[0])


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _columns(connection: sqlite3.Connection, table: str) -> tuple[sqlite3.Row, ...]:
    return tuple(connection.execute(f"PRAGMA table_info({table})").fetchall())


def _primary_key(columns: tuple[sqlite3.Row, ...]) -> str:
    primary = [str(row["name"]) for row in columns if int(row["pk"]) > 0]
    if len(primary) != 1:
        raise ValueError("source table requires one primary key")
    return primary[0]


def _rows(connection: sqlite3.Connection, table: str, primary_key: str) -> tuple[sqlite3.Row, ...]:
    return tuple(connection.execute(f"SELECT * FROM {table} ORDER BY {primary_key}").fetchall())


def _shared_row_equal(
    source: sqlite3.Row | dict[str, object],
    target: sqlite3.Row,
) -> bool:
    source_values = dict(source)
    target_values = dict(target)
    shared = set(source_values) & set(target_values)
    return all(source_values[key] == target_values[key] for key in shared)


def _profile_equal(source: sqlite3.Row, target: sqlite3.Row) -> bool:
    ignored = {"id", "created_at"}
    source_values = dict(source)
    target_values = dict(target)
    shared = (set(source_values) & set(target_values)) - ignored
    return all(source_values[key] == target_values[key] for key in shared)


def _logical_fingerprint(connection: sqlite3.Connection, tables: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for table in sorted(tables):
        columns = _columns(connection, table)
        primary_key = _primary_key(columns)
        digest.update(table.encode())
        for row in _rows(connection, table, primary_key):
            digest.update(
                json.dumps(
                    dict(row),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            )
    return digest.hexdigest()


def _source_identity(database: Path) -> tuple[str, str]:
    try:
        root = database.parents[4]
        key = (root / "bootstrap" / "identity.key").read_bytes()
    except (IndexError, OSError):
        fingerprint_domain = hashlib.sha256(str(database).encode()).hexdigest()
    else:
        fingerprint_domain = hashlib.sha256(key).hexdigest()
    return (
        f"src_{hashlib.sha256(fingerprint_domain.encode()).hexdigest()[:26]}",
        fingerprint_domain,
    )


def _target_store_id(data_root: Path) -> str:
    key_path = data_root / "bootstrap" / "identity.key"
    key = key_path.read_bytes()
    return f"gpm_{hashlib.sha256(key).hexdigest()[:26]}"
