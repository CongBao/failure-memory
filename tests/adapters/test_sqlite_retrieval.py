from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from failure_memory.adapters.retrieval.sqlite import SQLiteRetrievalIndex
from failure_memory.domain.causal import CauseLayer, FailureMode
from failure_memory.domain.records import LessonDraft, LessonState, LessonVersionRecord
from failure_memory.domain.retrieval import (
    EmbeddingSpec,
    RecallMode,
    RecallQuery,
    RetrievalDocument,
)

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class _FakeEmbeddingProvider:
    @property
    def spec(self) -> EmbeddingSpec:
        return EmbeddingSpec(
            provider="test",
            model="keyword-vectors",
            revision="1",
            dimensions=3,
        )

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._embed(text) for text in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._embed(text)

    def _embed(self, text: str) -> tuple[float, ...]:
        lowered = text.casefold()
        if "migration" in lowered or "schema" in lowered:
            return (1.0, 0.0, 0.0)
        if "invoice" in lowered or "ledger" in lowered:
            return (0.0, 1.0, 0.0)
        return (0.0, 0.0, 1.0)


def _document(
    identifier: str,
    *,
    workspace: str,
    title: str,
    rule: str,
    invariant: str,
    cause: str,
    cause_layer: CauseLayer | None = None,
    failure_mode: FailureMode | None = None,
    repair_target_layer: CauseLayer | None = None,
) -> RetrievalDocument:
    lesson = LessonVersionRecord(
        id=identifier,
        lesson_id=f"lesson-{identifier}",
        version_number=1,
        created_at=NOW,
        state=LessonState.PROPOSED,
        signature=f"signature-{identifier}",
        draft=LessonDraft(
            title=title,
            rule=rule,
            prevention_action=f"Prevent {cause}",
            verification_action=f"Verify {invariant}",
            applicability="Relevant engineering tasks.",
            counterexamples="Unrelated tasks.",
        ),
    )
    return RetrievalDocument(
        lesson_version=lesson,
        workspace_fingerprint=workspace,
        expected_invariant=invariant,
        controllable_cause=cause,
        outcome_summary=f"Observed outcome for {title}",
        material_impact="Material delay.",
        recurrence_risk="Can recur.",
        cause_layer=cause_layer,
        failure_mode=failure_mode,
        repair_target_layer=repair_target_layer,
    )


def test_fts5_index_is_rebuildable_and_global_across_origins(tmp_path: Path) -> None:
    index = SQLiteRetrievalIndex(tmp_path / "index.sqlite3")
    migration = _document(
        "lv-migration",
        workspace="workspace-a",
        title="Run schema migration preflight",
        rule="Validate schema compatibility before migration writes.",
        invariant="Migration writes preserve the schema contract.",
        cause="The schema preflight was skipped.",
    )
    invoice = _document(
        "lv-invoice",
        workspace="workspace-b",
        title="Balance invoice ledgers",
        rule="Balance every currency before posting.",
        invariant="Invoice ledger totals balance.",
        cause="A currency was omitted.",
    )

    assert index.sync([migration, invoice]) == 2
    query = RecallQuery(
        mode=RecallMode.LEXICAL,
        text="Prepare a schema migration.",
        component="migration",
    )

    matches = index.search_lexical(query, limit=5)

    assert [match.lesson_version_id for match in matches] == ["lv-migration"]
    assert index.sync([migration, invoice]) == 0
    assert index.sync([invoice]) == 1
    assert index.status().indexed_documents == 1
    index.close()


def test_sqlite_vec_runs_exact_cosine_knn_and_hybrid_channels(tmp_path: Path) -> None:
    pytest.importorskip("sqlite_vec")
    index = SQLiteRetrievalIndex(
        tmp_path / "vectors.sqlite3",
        embedding_provider=_FakeEmbeddingProvider(),
    )
    documents = [
        _document(
            "lv-migration",
            workspace="workspace-a",
            title="Run schema migration preflight",
            rule="Validate schema compatibility before migration writes.",
            invariant="Migration writes preserve the schema contract.",
            cause="The schema preflight was skipped.",
        ),
        _document(
            "lv-invoice",
            workspace="workspace-a",
            title="Balance invoice ledgers",
            rule="Balance every currency before posting.",
            invariant="Invoice ledger totals balance.",
            cause="A currency was omitted.",
        ),
        _document(
            "lv-schema-copy",
            workspace="workspace-b",
            title="Check schema compatibility",
            rule="Run compatibility checks before database changes.",
            invariant="Schema changes remain compatible.",
            cause="The migration compatibility check was skipped.",
        ),
    ]
    index.sync(documents)
    query = RecallQuery(
        mode=RecallMode.HYBRID,
        text="Deploy a database schema change.",
        component="migration",
    )

    semantic = index.search_semantic(query, limit=2)
    lexical = index.search_lexical(query, limit=2)

    assert index.status().semantic_available is True
    assert {match.lesson_version_id for match in semantic} == {
        "lv-migration",
        "lv-schema-copy",
    }
    assert all(match.distance == pytest.approx(0.0) for match in semantic)
    assert {match.lesson_version_id for match in lexical} == {
        "lv-migration",
        "lv-schema-copy",
    }
    pairs = index.similar_pairs(documents, distance_threshold=0.01)
    assert [
        (pair.left_lesson_version_id, pair.right_lesson_version_id, pair.distance) for pair in pairs
    ] == [("lv-migration", "lv-schema-copy", pytest.approx(0.0))]
    index.close()


def test_causal_filters_are_applied_inside_lexical_and_vector_search(tmp_path: Path) -> None:
    pytest.importorskip("sqlite_vec")
    index = SQLiteRetrievalIndex(
        tmp_path / "causal-filters.sqlite3",
        embedding_provider=_FakeEmbeddingProvider(),
    )
    documents = [
        _document(
            "lv-external",
            workspace="workspace-a",
            title="Run schema migration preflight",
            rule="Validate schema compatibility before migration writes.",
            invariant="Migration writes preserve the schema contract.",
            cause="The external service was unavailable.",
            cause_layer=CauseLayer.EXTERNAL_DEPENDENCY,
            failure_mode=FailureMode.UNKNOWN,
            repair_target_layer=CauseLayer.EXTERNAL_DEPENDENCY,
        ),
        _document(
            "lv-skill",
            workspace="workspace-a",
            title="Run schema migration preflight",
            rule="Validate schema compatibility before migration writes.",
            invariant="Migration writes preserve the schema contract.",
            cause="The skill instruction was ambiguous.",
            cause_layer=CauseLayer.SKILL_INSTRUCTION,
            failure_mode=FailureMode.AMBIGUOUS,
            repair_target_layer=CauseLayer.SKILL_INSTRUCTION,
        ),
    ]
    index.sync(documents)
    query = RecallQuery(
        mode=RecallMode.HYBRID,
        text="Deploy a database schema migration.",
        cause_layer=CauseLayer.SKILL_INSTRUCTION,
        failure_mode=FailureMode.AMBIGUOUS,
        repair_target_layer=CauseLayer.SKILL_INSTRUCTION,
    )

    assert [match.lesson_version_id for match in index.search_lexical(query, limit=1)] == [
        "lv-skill"
    ]
    assert [match.lesson_version_id for match in index.search_semantic(query, limit=1)] == [
        "lv-skill"
    ]
    index.close()


def test_version_two_index_is_upgraded_and_vectors_are_rebuilt(tmp_path: Path) -> None:
    sqlite_vec = pytest.importorskip("sqlite_vec")
    database = tmp_path / "upgraded.sqlite3"
    document = _document(
        "lv-skill",
        workspace="workspace-a",
        title="Run schema migration preflight",
        rule="Validate schema compatibility before migration writes.",
        invariant="Migration writes preserve the schema contract.",
        cause="The skill instruction was ambiguous.",
        cause_layer=CauseLayer.SKILL_INSTRUCTION,
        failure_mode=FailureMode.AMBIGUOUS,
        repair_target_layer=CauseLayer.SKILL_INSTRUCTION,
    )
    current = SQLiteRetrievalIndex(database, embedding_provider=_FakeEmbeddingProvider())
    current.sync([document])
    current.close()

    connection = sqlite3.connect(database)
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)
    connection.execute("UPDATE retrieval_metadata SET value = '2' WHERE key = 'schema_version'")
    connection.execute("DROP TABLE lesson_vec")
    connection.execute(
        """
        CREATE VIRTUAL TABLE lesson_vec USING vec0(
            embedding float[3] distance_metric=cosine,
            origin_workspace_fingerprint text,
            lesson_version_id text
        )
        """
    )
    connection.commit()
    connection.close()

    upgraded = SQLiteRetrievalIndex(database, embedding_provider=_FakeEmbeddingProvider())
    query = RecallQuery(
        mode=RecallMode.SEMANTIC,
        text="Deploy a database schema migration.",
        cause_layer=CauseLayer.SKILL_INSTRUCTION,
    )

    assert [match.lesson_version_id for match in upgraded.search_semantic(query, limit=1)] == [
        document.lesson_version.id
    ]
    upgraded.close()


def test_embedding_profile_mismatch_requires_a_separate_index(tmp_path: Path) -> None:
    pytest.importorskip("sqlite_vec")
    database = tmp_path / "profile.sqlite3"
    first = SQLiteRetrievalIndex(database, embedding_provider=_FakeEmbeddingProvider())
    first.close()

    class _OtherProvider(_FakeEmbeddingProvider):
        @property
        def spec(self) -> EmbeddingSpec:
            return EmbeddingSpec(
                provider="test",
                model="different",
                revision="1",
                dimensions=3,
            )

    second = SQLiteRetrievalIndex(database, embedding_provider=_OtherProvider())

    assert second.status().semantic_available is False
    assert "profile differs" in str(second.status().detail)
    second.close()
