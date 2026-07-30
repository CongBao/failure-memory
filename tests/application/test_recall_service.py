from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from failure_memory.adapters.event_store.sqlite.connection import connect_sqlite
from failure_memory.adapters.event_store.sqlite.migrate import apply_migrations
from failure_memory.adapters.event_store.sqlite.store import SQLiteEventStore
from failure_memory.adapters.harness.context import HarnessContext
from failure_memory.adapters.retrieval.sqlite import SQLiteRetrievalIndex
from failure_memory.application.service import FailureMemoryService
from failure_memory.domain.capture import (
    Classification,
    ExpectationSource,
    FailureCandidate,
)
from failure_memory.domain.learning import SimilarityPair
from failure_memory.domain.records import IncidentDraft, LessonDraft
from failure_memory.domain.retrieval import (
    RecallMode,
    RecallOutcome,
    RecallOutcomeKind,
    RecallQuery,
    RecallStatus,
    RetrievalIndexStatus,
    RetrievalProfile,
)

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class _ProposalIndex:
    profile_name = "proposal-test"

    @property
    def profile(self) -> RetrievalProfile:
        return RetrievalProfile(
            name=self.profile_name,
            backend="test",
            config_fingerprint="c" * 64,
            capabilities=("semantic",),
        )

    def status(self) -> RetrievalIndexStatus:
        return RetrievalIndexStatus(
            state="ready",
            profile=self.profile_name,
            lexical_available=False,
            semantic_available=True,
            indexed_documents=2,
        )

    def sync(self, documents: object) -> int:
        del documents
        return 0

    def similar_pairs(
        self, documents: object, *, distance_threshold: float
    ) -> tuple[SimilarityPair, ...]:
        assert distance_threshold == pytest.approx(0.2)
        identifiers = sorted(
            document.lesson_version.id
            for document in documents  # type: ignore[union-attr]
        )
        return (SimilarityPair(identifiers[0], identifiers[1], 0.05),)


@pytest.fixture
def recall_service(tmp_path: Path) -> FailureMemoryService:
    connection = connect_sqlite(tmp_path / "event-store.sqlite3")
    apply_migrations(connection)
    context = HarnessContext.create(
        data_root=tmp_path / "data",
        cwd=tmp_path / "workspace",
        harness="pytest",
        session_id="recall-tests",
    )
    index = SQLiteRetrievalIndex(tmp_path / "retrieval.sqlite3")

    def close() -> None:
        index.close()
        connection.close()

    service = FailureMemoryService(
        SQLiteEventStore(connection),
        context,
        clock=lambda: NOW,
        closer=close,
        retrieval_index=index,
    )
    yield service
    service.close()


def _candidate(
    *,
    classification: Classification = Classification.REAL_FAILURE,
) -> FailureCandidate:
    return FailureCandidate(
        summary="A known migration invariant was missed.",
        classification=classification,
        expectation_source=ExpectationSource.ACCEPTED_DESIGN,
        expectation_established_at=NOW - timedelta(minutes=1),
        observed_outcome_at=NOW,
        outcome_mismatch=True,
        material_impact_or_recurrence_risk=True,
        controllable_with_prior_information=True,
        durable_lesson=True,
    )


def _record_migration_lesson(service: FailureMemoryService) -> str:
    evaluated = service.evaluate_failure_candidate(_candidate())
    result = service.record_failure_incident(
        evaluated.capture_attempt_id,
        IncidentDraft(
            outcome_summary="The migration wrote incompatible rows.",
            expected_invariant="Migration writes preserve the schema contract.",
            controllable_cause="The required schema preflight was skipped.",
            material_impact="The release was delayed.",
            recurrence_risk="Future schema migrations can repeat the failure.",
        ),
        LessonDraft(
            title="Run schema migration preflight",
            rule="Validate compatibility before every schema migration write.",
            prevention_action="Run the schema preflight before migration writes.",
            verification_action="Confirm the schema preflight is clean.",
            applicability="Schema-changing migrations.",
            counterexamples="Read-only schema diagnostics.",
        ),
    )
    return result.lesson_version_id


def test_exact_first_recall_returns_one_lesson_and_records_trace(
    recall_service: FailureMemoryService,
) -> None:
    lesson_version_id = _record_migration_lesson(recall_service)

    result = recall_service.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.AUTO,
            expected_invariant="Migration writes preserve the schema contract.",
            controllable_cause="The required schema preflight was skipped.",
            prevention_action="Run the schema preflight before migration writes.",
        )
    )

    assert result.status is RecallStatus.OK
    assert result.executed_mode is RecallMode.EXACT
    assert [candidate.lesson.id for candidate in result.candidates] == [lesson_version_id]
    assert result.candidates[0].channels == ("exact",)
    assert recall_service.recall_metrics() == {
        "retrieval_profile_snapshot": 1,
        "recall_attempt": 1,
        "recall_candidate": 1,
        "recall_selection": 1,
        "recall_outcome_event": 0,
        "recall_miss_event": 0,
        "attempt_status_ok": 1,
        "attempt_status_no_match": 0,
        "attempt_status_degraded": 0,
        "attempt_status_setup_required": 0,
        "attempt_status_insufficient_evidence": 0,
        "outcome_useful": 0,
        "outcome_not_useful": 0,
        "outcome_false_positive": 0,
        "outcome_prevented_recurrence": 0,
        "outcome_contradicted_current_task": 0,
        "outcome_stale": 0,
        "outcome_ignored": 0,
        "outcome_unknown": 0,
        "outcome_missed_relevant": 0,
    }


def test_hybrid_degrades_to_fts5_without_silent_adapter_install(
    recall_service: FailureMemoryService,
) -> None:
    lesson_version_id = _record_migration_lesson(recall_service)

    result = recall_service.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.HYBRID,
            text="Prepare a database schema migration deployment.",
            component="migration",
        )
    )

    assert result.status is RecallStatus.DEGRADED
    assert result.executed_mode is RecallMode.LEXICAL
    assert [candidate.lesson.id for candidate in result.candidates] == [lesson_version_id]
    assert result.candidates[0].channels == ("lexical",)
    assert "not configured" in str(result.detail)


def test_semantic_only_returns_setup_required_without_downloading(
    recall_service: FailureMemoryService,
) -> None:
    _record_migration_lesson(recall_service)

    result = recall_service.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.SEMANTIC,
            text="Prepare a database schema migration deployment.",
            component="migration",
        )
    )

    assert result.status is RecallStatus.SETUP_REQUIRED
    assert result.candidates == ()
    assert recall_service.recall_metrics()["recall_attempt"] == 1


def test_similarity_recall_requires_task_context_and_an_explicit_discriminator(
    recall_service: FailureMemoryService,
) -> None:
    _record_migration_lesson(recall_service)

    result = recall_service.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.AUTO,
            text="Something went wrong; find anything similar.",
        )
    )

    assert result.status is RecallStatus.INSUFFICIENT_EVIDENCE
    assert result.candidates == ()
    assert "explicit invariant" in str(result.detail)


def test_recall_query_text_is_not_stored_and_false_positive_feedback_is_append_only(
    recall_service: FailureMemoryService,
) -> None:
    lesson_version_id = _record_migration_lesson(recall_service)
    secret_query_marker = "PRIVATE-QUERY-MARKER-DO-NOT-STORE"
    result = recall_service.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.LEXICAL,
            text=f"{secret_query_marker} schema migration",
            component="migration",
        )
    )
    assert result.candidates

    outcome_id = recall_service.record_recall_outcome(
        RecallOutcome(
            attempt_id=result.attempt_id,
            lesson_version_id=lesson_version_id,
            outcome=RecallOutcomeKind.FALSE_POSITIVE,
            detail_code="different_failure_mechanism",
            confidence=0.9,
        )
    )
    stored_text = " ".join(
        str(value)
        for table in (
            "retrieval_profile_snapshot",
            "recall_attempt",
            "recall_candidate",
            "recall_selection",
            "recall_outcome_event",
        )
        for row in recall_service.store.connection.execute(f"SELECT * FROM {table}")
        for value in row
    )

    assert outcome_id.startswith("ro_")
    assert secret_query_marker not in stored_text
    assert recall_service.recall_metrics()["recall_outcome_event"] == 1
    assert recall_service.recall_metrics()["outcome_false_positive"] == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only table"):
        recall_service.store.connection.execute(
            "UPDATE recall_outcome_event SET outcome = 'useful' WHERE id = ?",
            (outcome_id,),
        )


def test_rejected_requirement_update_never_enters_the_retrieval_index(
    recall_service: FailureMemoryService,
) -> None:
    evaluated = recall_service.evaluate_failure_candidate(
        _candidate(classification=Classification.REQUIREMENT_UPDATE)
    )

    report = recall_service.build_index()

    assert evaluated.assessment.decision.value == "reject"
    assert report["indexed_documents"] == 0
    assert recall_service.retrieval_index is not None
    assert (
        recall_service.retrieval_index.search_lexical(
            RecallQuery(
                mode=RecallMode.LEXICAL,
                text="migration invariant",
                component="migration",
            ),
            limit=5,
        )
        == ()
    )


def test_learning_metrics_include_positive_feedback_and_missed_relevant_lessons(
    recall_service: FailureMemoryService,
) -> None:
    lesson_version_id = _record_migration_lesson(recall_service)
    selected = recall_service.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.EXACT,
            expected_invariant="Migration writes preserve the schema contract.",
            controllable_cause="The required schema preflight was skipped.",
            prevention_action="Run the schema preflight before migration writes.",
        )
    )
    missed = recall_service.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.EXACT,
            expected_invariant="A different invariant.",
            controllable_cause="A different cause.",
            prevention_action="A different prevention.",
        )
    )
    recall_service.record_recall_outcome(
        RecallOutcome(
            attempt_id=selected.attempt_id,
            lesson_version_id=lesson_version_id,
            outcome=RecallOutcomeKind.USEFUL,
            confidence=0.9,
        )
    )
    recall_service.record_recall_outcome(
        RecallOutcome(
            attempt_id=missed.attempt_id,
            lesson_version_id=lesson_version_id,
            outcome=RecallOutcomeKind.MISSED_RELEVANT,
            detail_code="expected_relevant_lesson",
            confidence=0.8,
        )
    )

    metrics = recall_service.learning_metrics()

    assert metrics["scope"] == "global_personal"
    assert metrics["attempt_count"] == 2
    assert metrics["selection_count"] == 1
    assert metrics["labeled_attempt_count"] == 2
    assert metrics["positive_selection_count"] == 1
    assert metrics["missed_relevant_count"] == 1
    assert metrics["feedback_coverage"] == pytest.approx(1.0)
    assert metrics["selection_feedback_coverage"] == pytest.approx(1.0)
    assert metrics["precision_at"] == {"1": 1.0, "3": 1.0}
    assert metrics["attempts_by_harness"] == {"pytest": 2}

    recall_service.record_recall_outcome(
        RecallOutcome(
            attempt_id=selected.attempt_id,
            lesson_version_id=lesson_version_id,
            outcome=RecallOutcomeKind.FALSE_POSITIVE,
            detail_code="later_counterexample",
        )
    )
    repeated_metrics = recall_service.learning_metrics()
    assert repeated_metrics["labeled_selection_count"] == 1
    assert repeated_metrics["selection_feedback_coverage"] == pytest.approx(1.0)
    assert repeated_metrics["false_positive_count"] == 1


def test_semantic_clustering_records_proposals_without_merging_lessons(
    recall_service: FailureMemoryService,
) -> None:
    _record_migration_lesson(recall_service)
    evaluated = recall_service.evaluate_failure_candidate(_candidate())
    recall_service.record_failure_incident(
        evaluated.capture_attempt_id,
        IncidentDraft(
            outcome_summary="The invoice ledger omitted one currency.",
            expected_invariant="Every invoice currency balances before posting.",
            controllable_cause="The per-currency balance check was skipped.",
            material_impact="An invoice total was incorrect.",
            recurrence_risk="Another multi-currency invoice could repeat the error.",
        ),
        LessonDraft(
            title="Balance invoices by currency",
            rule="Check each currency before posting an invoice.",
            prevention_action="Run the per-currency balance check.",
            verification_action="Confirm every currency subtotal is zero.",
            applicability="Multi-currency invoice posting.",
            counterexamples="Single-currency read-only reports.",
        ),
    )
    recall_service.retrieval_index = _ProposalIndex()  # type: ignore[assignment]

    proposal = recall_service.propose_lesson_clusters(distance_threshold=0.2)

    assert proposal["state"] == "proposed"
    assert proposal["cluster_count"] == 1
    assert proposal["automatic_merge"] is False
    assert recall_service.store.counts()["lesson"] == 2
    assert (
        recall_service.store.connection.execute(
            "SELECT COUNT(*) FROM lesson_cluster_run"
        ).fetchone()[0]
        == 1
    )
    assert (
        recall_service.store.connection.execute(
            "SELECT COUNT(*) FROM lesson_cluster_member"
        ).fetchone()[0]
        == 2
    )
    assert (
        recall_service.store.connection.execute(
            "SELECT COUNT(*) FROM lesson_generalization_proposal"
        ).fetchone()[0]
        == 1
    )
