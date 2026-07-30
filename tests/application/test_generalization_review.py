from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from failure_memory.application.service import create_local_service
from failure_memory.domain.capture import Classification, ExpectationSource, FailureCandidate
from failure_memory.domain.records import (
    IncidentDraft,
    LessonDraft,
    RecordingDisposition,
)
from failure_memory.domain.retrieval import RecallMode, RecallQuery, RecallStatus

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _candidate(summary: str) -> FailureCandidate:
    return FailureCandidate(
        summary=summary,
        classification=Classification.REAL_FAILURE,
        expectation_source=ExpectationSource.ACCEPTED_DESIGN,
        expectation_established_at=NOW - timedelta(minutes=1),
        observed_outcome_at=NOW,
        outcome_mismatch=True,
        material_impact_or_recurrence_risk=True,
        controllable_with_prior_information=True,
        durable_lesson=True,
    )


def _drafts(suffix: str = "") -> tuple[IncidentDraft, LessonDraft]:
    return (
        IncidentDraft(
            outcome_summary=f"The migration skipped its preflight {suffix}".strip(),
            expected_invariant=f"Migration writes require a validated preflight {suffix}".strip(),
            controllable_cause=f"The agent skipped the available preflight {suffix}".strip(),
            material_impact="The release was delayed.",
            recurrence_risk="Future migrations can repeat this failure.",
        ),
        LessonDraft(
            title="Validate migration preflight",
            rule="Validate the migration preflight before any schema write.",
            prevention_action=f"Run and inspect the migration preflight {suffix}".strip(),
            verification_action="Require a successful preflight result.",
            applicability="Schema-changing migrations.",
            counterexamples="Read-only schema inspection.",
        ),
    )


def test_required_review_creates_then_exactly_reuses_one_global_lesson(
    tmp_path: Path,
) -> None:
    root = tmp_path / "global"
    codex = create_local_service(
        data_root=root,
        cwd=tmp_path / "workspace",
        harness="codex",
        session_id="codex-session",
    )
    incident, lesson = _drafts()
    capture = codex.evaluate_failure_candidate(_candidate("Codex skipped preflight"))
    review = codex.review_failure_recording(capture.capture_attempt_id, incident, lesson)
    assert review["recommendation"] == "create_distinct"
    first = codex.record_failure_incident(
        capture.capture_attempt_id,
        incident,
        lesson,
        generalization_review_id=str(review["review_id"]),
        disposition=RecordingDisposition.CREATE_DISTINCT,
        rationale_code="no_related_lesson",
    )
    codex.close()

    copilot = create_local_service(
        data_root=root,
        cwd=tmp_path / "other-workspace",
        harness="copilot",
        session_id="copilot-session",
    )
    second_capture = copilot.evaluate_failure_candidate(_candidate("Copilot skipped preflight"))
    second_review = copilot.review_failure_recording(
        second_capture.capture_attempt_id, incident, lesson
    )
    assert second_review["recommendation"] == "reuse_exact"
    assert second_review["candidates"][0]["lesson_version_id"] == first.lesson_version_id
    reused = copilot.record_failure_incident(
        second_capture.capture_attempt_id,
        incident,
        lesson,
        generalization_review_id=str(second_review["review_id"]),
        disposition=RecordingDisposition.REUSE_EXISTING,
        target_lesson_version_id=first.lesson_version_id,
        rationale_code="exact_signature_match",
    )
    assert reused.lesson_id == first.lesson_id
    assert reused.created_new_lesson is False
    assert reused.generalization_decision_id is not None
    assert copilot.metrics()["lesson"] == 1
    recalled = copilot.recall_failure_lessons(
        RecallQuery(
            mode=RecallMode.EXACT,
            text="Prepare the next migration.",
            expected_invariant=incident.expected_invariant,
            controllable_cause=incident.controllable_cause,
            prevention_action=lesson.prevention_action,
        )
    )
    assert recalled.status is RecallStatus.OK
    assert recalled.candidates[0].lesson.lesson_id == first.lesson_id
    copilot.close()


def test_exact_review_cannot_create_or_generalize_a_duplicate(tmp_path: Path) -> None:
    service = create_local_service(data_root=tmp_path / "global", cwd=tmp_path)
    incident, lesson = _drafts()
    first_capture = service.evaluate_failure_candidate(_candidate("first"))
    first_review = service.review_failure_recording(
        first_capture.capture_attempt_id, incident, lesson
    )
    first = service.record_failure_incident(
        first_capture.capture_attempt_id,
        incident,
        lesson,
        generalization_review_id=str(first_review["review_id"]),
        disposition=RecordingDisposition.CREATE_DISTINCT,
        rationale_code="no_related_lesson",
    )
    second_capture = service.evaluate_failure_candidate(_candidate("second"))
    second_review = service.review_failure_recording(
        second_capture.capture_attempt_id, incident, lesson
    )

    with pytest.raises(ValueError, match="exact existing lesson"):
        service.record_failure_incident(
            second_capture.capture_attempt_id,
            incident,
            lesson,
            generalization_review_id=str(second_review["review_id"]),
            disposition=RecordingDisposition.GENERALIZE_EXISTING,
            target_lesson_version_id=first.lesson_version_id,
            rationale_code="unnecessary_broadening",
        )
    service.close()
