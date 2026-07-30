from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from failure_memory.adapters.retrieval.sqlite import SQLiteRetrievalIndex
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
