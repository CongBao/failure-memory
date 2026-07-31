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
from failure_memory.domain.causal import (
    CausalAssessmentDraft,
    CausalAssessmentState,
    CausalConfidence,
    CausalFactorDraft,
    CausalFactorRole,
    CauseLayer,
    FailureMode,
    RepairRecommendationDraft,
)
from failure_memory.domain.learning import (
    GeneralizationProposalDecision,
    GeneralizedLessonDraft,
    SimilarityPair,
)
from failure_memory.domain.records import IncidentDraft, LessonDraft
from failure_memory.domain.retrieval import (
    RecallMode,
    RecallOutcome,
    RecallOutcomeKind,
    RecallQuery,
    RecallStatus,
    RetrievalIndexStatus,
    RetrievalMatch,
    RetrievalProfile,
)

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class _ProposalIndex:
    profile_name = "proposal-test"

    def __init__(self) -> None:
        self.documents: tuple[object, ...] = ()

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
        self.documents = tuple(documents)  # type: ignore[arg-type]
        return 0

    def search_lexical(
        self,
        query: object,
        *,
        limit: int,
    ) -> tuple[RetrievalMatch, ...]:
        del query, limit
        matching = [
            document
            for document in self.documents
            if "migration" in document.lesson_version.draft.title.casefold()  # type: ignore[union-attr]
        ]
        return tuple(
            RetrievalMatch(
                lesson_version_id=document.lesson_version.id,  # type: ignore[union-attr]
                channel="lexical",
                rank=rank,
                score=1.0,
            )
            for rank, document in enumerate(matching, start=1)
        )

    def search_semantic(
        self,
        query: object,
        *,
        limit: int,
    ) -> tuple[RetrievalMatch, ...]:
        del query, limit
        return ()

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


def test_causal_layer_is_indexed_returned_and_available_as_a_structured_filter(
    recall_service: FailureMemoryService,
) -> None:
    evaluated = recall_service.evaluate_failure_candidate(_candidate())
    assessment = recall_service.diagnose_failure_cause(
        evaluated.capture_attempt_id,
        CausalAssessmentDraft(
            state=CausalAssessmentState.SUPPORTED,
            factors=(
                CausalFactorDraft(
                    role=CausalFactorRole.PRIMARY,
                    layer=CauseLayer.SKILL_INSTRUCTION,
                    failure_mode=FailureMode.AMBIGUOUS,
                    component_reference="skill:migration-preflight",
                    evidence_summary="The preflight instruction did not define ordering.",
                    confidence=CausalConfidence.HIGH,
                ),
            ),
            recommendations=(
                RepairRecommendationDraft(
                    target_layer=CauseLayer.SKILL_INSTRUCTION,
                    target_reference="skill:migration-preflight",
                    recommended_change="State the preflight order explicitly.",
                    verification_action="Test a migration write without a preflight.",
                    rationale="The instruction ambiguity allowed the skipped control.",
                    confidence=CausalConfidence.HIGH,
                ),
            ),
        ),
    )
    record = recall_service.record_failure_incident(
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
        causal_assessment_id=assessment.id,
    )

    matching = recall_service.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.LEXICAL,
            text="schema migration preflight",
            cause_layer=CauseLayer.SKILL_INSTRUCTION,
        )
    )
    excluded = recall_service.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.LEXICAL,
            text="schema migration preflight",
            cause_layer=CauseLayer.EXTERNAL_DEPENDENCY,
        )
    )

    assert [candidate.lesson.id for candidate in matching.candidates] == [record.lesson_version_id]
    assert matching.candidates[0].cause_layer is CauseLayer.SKILL_INSTRUCTION
    assert matching.candidates[0].failure_mode is FailureMode.AMBIGUOUS
    assert matching.candidates[0].repair_target_layer is CauseLayer.SKILL_INSTRUCTION
    assert excluded.status is RecallStatus.NO_MATCH
    assert excluded.candidates == ()


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


def test_reviewed_cluster_acceptance_is_append_only_and_does_not_merge_sources(
    recall_service: FailureMemoryService,
) -> None:
    first_lesson_version_id = _record_migration_lesson(recall_service)
    evaluated = recall_service.evaluate_failure_candidate(_candidate())
    second = recall_service.record_failure_incident(
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
    recall_service.propose_lesson_clusters(distance_threshold=0.2)
    proposal = recall_service.list_lesson_generalization_proposals()[0]

    review = recall_service.review_lesson_generalization_proposal(
        proposal["proposal_id"],
        GeneralizationProposalDecision.ACCEPT,
        "reviewed_related_failures",
    )

    assert review["decision"] == "accept"
    assert review["resulting_lesson_version_id"] is None
    assert review["supporting_lesson_version_ids"] == sorted(
        [first_lesson_version_id, second.lesson_version_id]
    )
    assert recall_service.store.counts()["lesson"] == 2
    assert recall_service.list_lesson_generalization_proposals()[0]["status"] == "accepted"
    with pytest.raises(ValueError, match="terminal"):
        recall_service.review_lesson_generalization_proposal(
            proposal["proposal_id"],
            GeneralizationProposalDecision.REJECT,
            "later_reversal",
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only table"):
        recall_service.store.connection.execute(
            """
            UPDATE lesson_generalization_proposal_review
            SET rationale_code = 'changed'
            WHERE id = ?
            """,
            (review["review_id"],),
        )


def test_deferred_cluster_can_be_accepted_with_a_new_proposed_generalization(
    recall_service: FailureMemoryService,
) -> None:
    first_lesson_version_id = _record_migration_lesson(recall_service)
    evaluated = recall_service.evaluate_failure_candidate(_candidate())
    second = recall_service.record_failure_incident(
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
    recall_service.propose_lesson_clusters(distance_threshold=0.2)
    proposal_id = recall_service.list_lesson_generalization_proposals()[0]["proposal_id"]
    assert isinstance(proposal_id, str)
    deferred = recall_service.review_lesson_generalization_proposal(
        proposal_id,
        GeneralizationProposalDecision.DEFER,
        "needs_broader_wording",
    )

    accepted = recall_service.review_lesson_generalization_proposal(
        proposal_id,
        GeneralizationProposalDecision.ACCEPT,
        "reviewed_broader_control",
        GeneralizedLessonDraft(
            expected_invariant="State-changing operations preserve declared invariants.",
            controllable_cause="A required preflight validation was skipped.",
            lesson=LessonDraft(
                title="Run invariant preflights before writes",
                rule="Validate declared invariants before state-changing writes.",
                prevention_action="Run the applicable invariant preflight before writing.",
                verification_action="Confirm the preflight passes for every affected dimension.",
                applicability="State-changing migrations and ledger writes.",
                counterexamples="Read-only diagnostics without state changes.",
            ),
        ),
    )

    resulting_id = accepted["resulting_lesson_version_id"]
    assert isinstance(resulting_id, str)
    assert deferred["decision"] == "defer"
    assert accepted["prior_review_id"] == deferred["review_id"]
    assert accepted["decision"] == "accept"
    assert recall_service.store.counts()["lesson"] == 3
    assert {
        document.lesson_version.id for document in recall_service.store.list_retrieval_documents()
    } == {first_lesson_version_id, second.lesson_version_id, resulting_id}
    source_heads = {
        first_lesson_version_id,
        second.lesson_version_id,
    }
    assert source_heads <= {
        str(row["lesson_version_id"])
        for row in recall_service.store.connection.execute(
            "SELECT lesson_version_id FROM lesson_head"
        )
    }
    assert (
        recall_service.store.connection.execute(
            """
            SELECT COUNT(*)
            FROM lesson_generalization_source
            WHERE review_id = ? AND relation = 'supporting'
            """,
            (accepted["review_id"],),
        ).fetchone()[0]
        == 2
    )


def test_only_an_accepted_review_can_add_one_traceable_cluster_neighbor_to_recall(
    recall_service: FailureMemoryService,
) -> None:
    first_lesson_version_id = _record_migration_lesson(recall_service)
    evaluated = recall_service.evaluate_failure_candidate(_candidate())
    second = recall_service.record_failure_incident(
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
    proposal_index = _ProposalIndex()
    recall_service.retrieval_index = proposal_index
    recall_service.propose_lesson_clusters(distance_threshold=0.2)
    proposal = recall_service.list_lesson_generalization_proposals()[0]

    before_review = recall_service.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.LEXICAL,
            text="Prepare a database schema migration.",
            component="migration",
            top_k=3,
        )
    )
    assert [candidate.lesson.id for candidate in before_review.candidates] == [
        first_lesson_version_id
    ]

    review = recall_service.review_lesson_generalization_proposal(
        str(proposal["proposal_id"]),
        GeneralizationProposalDecision.ACCEPT,
        "reviewed_related_failures",
    )
    after_review = recall_service.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.LEXICAL,
            text="Prepare a database schema migration.",
            component="migration",
            top_k=3,
        )
    )

    assert [candidate.lesson.id for candidate in after_review.candidates] == [
        first_lesson_version_id,
        second.lesson_version_id,
    ]
    cluster_candidate = after_review.candidates[1]
    assert cluster_candidate.channels == ("cluster",)
    assert cluster_candidate.cluster_review_id == review["review_id"]
    assert cluster_candidate.cluster_key == proposal["cluster_key"]
    assert cluster_candidate.cluster_supporting_lesson_version_ids == tuple(
        sorted([first_lesson_version_id, second.lesson_version_id])
    )
    stored = recall_service.store.connection.execute(
        """
        SELECT cluster_review_id, cluster_key, cluster_supporting_lesson_version_ids_json
        FROM recall_candidate
        WHERE recall_attempt_id = ? AND lesson_version_id = ?
        """,
        (after_review.attempt_id, second.lesson_version_id),
    ).fetchone()
    assert stored is not None
    assert stored["cluster_review_id"] == review["review_id"]
    assert stored["cluster_key"] == proposal["cluster_key"]
