from __future__ import annotations

import importlib
import re
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

from failure_memory.adapters.event_store.sqlite.connection import (
    connect_sqlite,
    secure_sqlite_files,
)
from failure_memory.application.errors import SemanticSetupRequiredError
from failure_memory.domain.learning import SimilarityPair
from failure_memory.domain.retrieval import (
    EmbeddingSpec,
    RecallQuery,
    RetrievalDocument,
    RetrievalIndexStatus,
    RetrievalMatch,
    RetrievalProfile,
    retrieval_profile_fingerprint,
)
from failure_memory.ports.retrieval import EmbeddingProviderPort, RetrievalIndexPort

_TOKEN = re.compile(r"[\w-]+", re.UNICODE)
_SCHEMA_VERSION = "2"


class SQLiteRetrievalIndex(RetrievalIndexPort):
    """A rebuildable FTS5 index with optional exact sqlite-vec KNN."""

    def __init__(
        self,
        database: Path,
        *,
        embedding_provider: EmbeddingProviderPort | None = None,
        sqlite_vec_loader: Callable[[sqlite3.Connection], ModuleType] | None = None,
    ) -> None:
        self.database = database
        self.embedding_provider = embedding_provider
        self.connection = connect_sqlite(database)
        self._vec_module: ModuleType | None = None
        self._semantic_error: str | None
        self._initialize_lexical_schema()
        if embedding_provider is not None:
            loader = sqlite_vec_loader or _load_sqlite_vec
            try:
                self._vec_module = loader(self.connection)
                self._initialize_vector_schema(embedding_provider.spec)
            except (ImportError, ModuleNotFoundError, sqlite3.Error, RuntimeError) as exc:
                self._vec_module = None
                self._semantic_error = str(exc)
            else:
                self._semantic_error = None
        else:
            self._semantic_error = "optional semantic adapter is not configured"
        secure_sqlite_files(database)

    @property
    def profile_name(self) -> str:
        if self.embedding_provider is None:
            return "sqlite-fts5"
        return f"sqlite-fts5-vec-{self.embedding_provider.spec.profile_name}"

    @property
    def profile(self) -> RetrievalProfile:
        embedding = None if self.embedding_provider is None else self.embedding_provider.spec
        capabilities = (
            ("lexical", "semantic", "hybrid") if self._vec_module is not None else ("lexical",)
        )
        return RetrievalProfile(
            name=self.profile_name,
            backend="sqlite-fts5-sqlite-vec",
            config_fingerprint=retrieval_profile_fingerprint(
                backend="sqlite-fts5-sqlite-vec",
                embedding=embedding,
                lexical="fts5-unicode61",
                fusion="rrf-k60",
            ),
            capabilities=capabilities,
            embedding=embedding,
        )

    def status(self) -> RetrievalIndexStatus:
        count = int(self.connection.execute("SELECT COUNT(*) FROM indexed_lesson").fetchone()[0])
        return RetrievalIndexStatus(
            state="ready" if self._vec_module is not None else "lexical_ready",
            profile=self.profile_name,
            lexical_available=True,
            semantic_available=self._vec_module is not None,
            indexed_documents=count,
            detail=self._semantic_error,
        )

    def sync(self, documents: Sequence[RetrievalDocument]) -> int:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            changed = self._sync_global(documents)
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        secure_sqlite_files(self.database)
        return changed

    def search_lexical(
        self,
        query: RecallQuery,
        *,
        limit: int,
    ) -> tuple[RetrievalMatch, ...]:
        expression = _fts_expression(query.canonical_text())
        if not expression:
            return ()
        rows = self.connection.execute(
            """
            SELECT lesson_version_id, bm25(lesson_fts) AS relevance
            FROM lesson_fts
            WHERE lesson_fts MATCH ?
            ORDER BY relevance, rowid
            LIMIT ?
            """,
            (expression, limit),
        ).fetchall()
        return tuple(
            RetrievalMatch(
                lesson_version_id=str(row["lesson_version_id"]),
                channel="lexical",
                rank=rank,
                score=1.0 / (1.0 + abs(float(row["relevance"]))),
            )
            for rank, row in enumerate(rows, start=1)
        )

    def search_semantic(
        self,
        query: RecallQuery,
        *,
        limit: int,
    ) -> tuple[RetrievalMatch, ...]:
        provider = self.embedding_provider
        vec_module = self._vec_module
        if provider is None or vec_module is None:
            raise SemanticSetupRequiredError(
                self._semantic_error or "optional semantic adapter is not configured"
            )
        vector = provider.embed_query(query.canonical_text())
        serialize = vec_module.serialize_float32
        rows = self.connection.execute(
            """
            SELECT lesson_version_id, distance
            FROM lesson_vec
            WHERE embedding MATCH ?
              AND k = ?
            ORDER BY distance
            """,
            (serialize(vector), limit),
        ).fetchall()
        return tuple(
            RetrievalMatch(
                lesson_version_id=str(row["lesson_version_id"]),
                channel="semantic",
                rank=rank,
                score=1.0 - float(row["distance"]),
                distance=float(row["distance"]),
            )
            for rank, row in enumerate(rows, start=1)
        )

    def similar_pairs(
        self,
        documents: Sequence[RetrievalDocument],
        *,
        distance_threshold: float,
    ) -> tuple[SimilarityPair, ...]:
        if not 0 <= distance_threshold <= 2:
            raise ValueError("distance threshold must be between 0 and 2")
        provider = self.embedding_provider
        vec_module = self._vec_module
        if provider is None or vec_module is None:
            raise SemanticSetupRequiredError(
                self._semantic_error or "optional semantic adapter is not configured"
            )
        self.sync(documents)
        serialize = vec_module.serialize_float32
        k = min(max(len(documents), 1), 50)
        pairs: dict[tuple[str, str], float] = {}
        for document in documents:
            vector = provider.embed_query(document.canonical_text())
            rows = self.connection.execute(
                """
                SELECT lesson_version_id, distance
                FROM lesson_vec
                WHERE embedding MATCH ? AND k = ?
                ORDER BY distance
                """,
                (serialize(vector), k),
            ).fetchall()
            source_id = document.lesson_version.id
            for row in rows:
                target_id = str(row["lesson_version_id"])
                distance = float(row["distance"])
                if target_id == source_id or distance > distance_threshold:
                    continue
                key = (
                    min(source_id, target_id),
                    max(source_id, target_id),
                )
                pairs[key] = min(distance, pairs.get(key, distance))
        return tuple(
            SimilarityPair(left, right, distance)
            for (left, right), distance in sorted(pairs.items())
        )

    def close(self) -> None:
        self.connection.close()

    def _initialize_lexical_schema(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS retrieval_metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) STRICT
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS indexed_lesson(
                rowid INTEGER PRIMARY KEY,
                lesson_version_id TEXT NOT NULL UNIQUE,
                origin_workspace_fingerprint TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                content TEXT NOT NULL
            ) STRICT
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS indexed_lesson_origin_workspace_idx
            ON indexed_lesson(origin_workspace_fingerprint)
            """
        )
        self.connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS lesson_fts USING fts5(
                lesson_version_id UNINDEXED,
                origin_workspace_fingerprint UNINDEXED,
                content,
                tokenize = 'unicode61'
            )
            """
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO retrieval_metadata(key, value)
            VALUES ('schema_version', ?)
            """,
            (_SCHEMA_VERSION,),
        )
        row = self.connection.execute(
            "SELECT value FROM retrieval_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or str(row["value"]) != _SCHEMA_VERSION:
            raise RuntimeError("unsupported retrieval index schema")

    def _initialize_vector_schema(self, spec: EmbeddingSpec) -> None:
        if spec.dimensions <= 0:
            raise ValueError("embedding dimensions must be positive")
        existing = self.connection.execute(
            "SELECT value FROM retrieval_metadata WHERE key = 'embedding_profile'"
        ).fetchone()
        expected = _embedding_profile(spec)
        if existing is not None and str(existing["value"]) != expected:
            raise RuntimeError(
                "retrieval index embedding profile differs; rebuild with the configured profile"
            )
        self.connection.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS lesson_vec USING vec0(
                embedding float[{spec.dimensions}] distance_metric=cosine,
                origin_workspace_fingerprint text,
                lesson_version_id text
            )
            """
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO retrieval_metadata(key, value)
            VALUES ('embedding_profile', ?)
            """,
            (expected,),
        )

    def _sync_global(self, documents: Sequence[RetrievalDocument]) -> int:
        expected_ids = {document.lesson_version.id for document in documents}
        existing_rows = self.connection.execute(
            """
            SELECT rowid, lesson_version_id, content_hash
            FROM indexed_lesson
            """,
        ).fetchall()
        existing = {
            str(row["lesson_version_id"]): (int(row["rowid"]), str(row["content_hash"]))
            for row in existing_rows
        }
        changed = 0
        for lesson_version_id, (rowid, _content_hash) in existing.items():
            if lesson_version_id not in expected_ids:
                self._delete_row(rowid)
                changed += 1
        pending_embeddings: list[tuple[int, RetrievalDocument]] = []
        for document in documents:
            lesson_version_id = document.lesson_version.id
            current = existing.get(lesson_version_id)
            if current is not None and current[1] == document.content_hash:
                continue
            if current is not None:
                rowid = current[0]
                self._delete_row(rowid)
                self.connection.execute(
                    """
                    INSERT INTO indexed_lesson(
                        rowid, lesson_version_id, origin_workspace_fingerprint,
                        content_hash, content
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        rowid,
                        lesson_version_id,
                        document.workspace_fingerprint,
                        document.content_hash,
                        document.canonical_text(),
                    ),
                )
            else:
                cursor = self.connection.execute(
                    """
                    INSERT INTO indexed_lesson(
                        lesson_version_id, origin_workspace_fingerprint, content_hash, content
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        lesson_version_id,
                        document.workspace_fingerprint,
                        document.content_hash,
                        document.canonical_text(),
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return an indexed lesson rowid")
                rowid = int(cursor.lastrowid)
            self.connection.execute(
                """
                INSERT INTO lesson_fts(
                    rowid, lesson_version_id, origin_workspace_fingerprint, content
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    rowid,
                    lesson_version_id,
                    document.workspace_fingerprint,
                    document.canonical_text(),
                ),
            )
            pending_embeddings.append((rowid, document))
            changed += 1
        self._embed_pending(pending_embeddings)
        return changed

    def _embed_pending(self, pending: Sequence[tuple[int, RetrievalDocument]]) -> None:
        provider = self.embedding_provider
        vec_module = self._vec_module
        if provider is None or vec_module is None or not pending:
            return
        vectors = provider.embed_documents(
            [document.canonical_text() for _rowid, document in pending]
        )
        if len(vectors) != len(pending):
            raise RuntimeError("embedding provider returned an unexpected vector count")
        serialize = vec_module.serialize_float32
        for (rowid, document), vector in zip(pending, vectors, strict=True):
            self.connection.execute(
                """
                INSERT INTO lesson_vec(
                    rowid, embedding, origin_workspace_fingerprint, lesson_version_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    rowid,
                    serialize(vector),
                    document.workspace_fingerprint,
                    document.lesson_version.id,
                ),
            )

    def _delete_row(self, rowid: int) -> None:
        self.connection.execute("DELETE FROM lesson_fts WHERE rowid = ?", (rowid,))
        if self._vec_module is not None:
            self.connection.execute("DELETE FROM lesson_vec WHERE rowid = ?", (rowid,))
        self.connection.execute("DELETE FROM indexed_lesson WHERE rowid = ?", (rowid,))


def _fts_expression(value: str) -> str:
    tokens = [token for token in _TOKEN.findall(value.casefold()) if len(token) > 1]
    return " OR ".join(f'"{token}"' for token in tokens[:64])


def _embedding_profile(spec: EmbeddingSpec) -> str:
    return f"{spec.provider}:{spec.model}:{spec.revision}:{spec.dimensions}:{spec.distance}"


def _load_sqlite_vec(connection: sqlite3.Connection) -> ModuleType:
    module = importlib.import_module("sqlite_vec")
    connection.enable_load_extension(True)
    try:
        module.load(connection)
    finally:
        connection.enable_load_extension(False)
    return module
