from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from failure_memory.application.service import create_local_service
from failure_memory.domain.capture import (
    CaptureDecision,
    Classification,
    ExpectationSource,
    FailureCandidate,
)
from failure_memory.domain.records import (
    IncidentDraft,
    IncidentLessonRelation,
    LessonDraft,
)

OBSERVED_AT = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
EMPTY_COUNTS = {
    "capture_attempt": 0,
    "incident": 0,
    "lesson": 0,
    "lesson_version": 0,
    "incident_lesson_relation": 0,
}
RECORDED_COUNTS = {
    "capture_attempt": 3,
    "incident": 2,
    "lesson": 1,
    "lesson_version": 1,
    "incident_lesson_relation": 2,
}


def _real_failure() -> FailureCandidate:
    return FailureCandidate(
        summary="A schema migration skipped its established preflight.",
        classification=Classification.REAL_FAILURE,
        expectation_source=ExpectationSource.ACCEPTED_DESIGN,
        expectation_established_at=OBSERVED_AT - timedelta(minutes=5),
        observed_outcome_at=OBSERVED_AT,
        outcome_mismatch=True,
        material_impact_or_recurrence_risk=True,
        controllable_with_prior_information=True,
        durable_lesson=True,
    )


def _incident(outcome: str) -> IncidentDraft:
    return IncidentDraft(
        outcome_summary=outcome,
        expected_invariant="Every schema migration must pass the compatibility preflight.",
        controllable_cause="The agent skipped the required preflight command.",
        material_impact="The produced migration could write incompatible rows.",
        recurrence_risk="Later schema changes could repeat the same omission.",
    )


def _lesson() -> LessonDraft:
    return LessonDraft(
        title="Run the schema compatibility preflight",
        rule="Do not write a schema migration until the compatibility preflight passes.",
        prevention_action="Run the required compatibility preflight before editing migrations.",
        verification_action="Keep the successful preflight output with the change evidence.",
        applicability="Changes that create or alter persisted schema.",
        counterexamples="Read-only inspection that cannot change persisted schema.",
    )


def test_recording_flow_rejects_updates_deduplicates_recurrences_and_survives_restart(
    tmp_path: Path,
) -> None:
    """Would fail if qualification, exact reuse, integrity, or restart durability regressed."""
    data_root = tmp_path / "plugin-data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = create_local_service(
        data_root=data_root,
        cwd=workspace,
        harness="pytest-e2e",
        session_id="recording-flow-1",
    )

    update = replace(
        _real_failure(),
        summary="The user requested another output field after the result.",
        classification=Classification.REQUIREMENT_UPDATE,
        expectation_source=ExpectationSource.NONE,
        expectation_established_at=None,
    )
    rejected = service.evaluate_failure_candidate(update)

    assert rejected.assessment.decision is CaptureDecision.REJECT
    assert service.metrics() == {**EMPTY_COUNTS, "capture_attempt": 1}

    first_capture = service.evaluate_failure_candidate(_real_failure())
    first = service.record_failure_incident(
        first_capture.capture_attempt_id,
        _incident("The first migration omitted its compatibility preflight."),
        _lesson(),
    )
    second_capture = service.evaluate_failure_candidate(
        replace(
            _real_failure(),
            summary="A later schema migration repeated the same preflight omission.",
            observed_outcome_at=OBSERVED_AT + timedelta(minutes=10),
        )
    )
    second = service.record_failure_incident(
        second_capture.capture_attempt_id,
        _incident("The later migration independently omitted the same preflight."),
        _lesson(),
    )

    assert first_capture.assessment.decision is CaptureDecision.ACCEPT
    assert second_capture.assessment.decision is CaptureDecision.ACCEPT
    assert first.incident_id != second.incident_id
    assert first.relation is IncidentLessonRelation.NOVEL
    assert first.created_new_lesson is True
    assert second.lesson_id == first.lesson_id
    assert second.lesson_version_id == first.lesson_version_id
    assert second.relation is IncidentLessonRelation.SAME_CAUSE_SAME_INVARIANT
    assert second.created_new_lesson is False
    assert service.metrics() == RECORDED_COUNTS
    doctor = service.doctor()
    assert doctor["integrity_check"] == "ok"
    assert doctor["counts"] == RECORDED_COUNTS
    service.close()

    restarted = create_local_service(
        data_root=data_root,
        cwd=workspace,
        harness="pytest-e2e",
        session_id="recording-flow-2",
    )
    try:
        assert restarted.metrics() == RECORDED_COUNTS
        assert restarted.doctor()["integrity_check"] == "ok"
    finally:
        restarted.close()
