from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from failure_memory.domain.records import (
    IncidentDraft,
    IncidentLessonRelation,
    IncidentRecord,
    LessonDraft,
    LessonRecord,
    LessonState,
    LessonVersionRecord,
    RecordResult,
    lesson_signature,
)


def test_lesson_signature_normalizes_case_and_whitespace() -> None:
    one = lesson_signature(
        "Validate schema before write",
        "  Skipped   preflight ",
        "RUN migration validation",
    )
    two = lesson_signature(
        "validate SCHEMA before write",
        "skipped preflight",
        "run migration validation",
    )
    assert one == two
    assert len(one) == 64


def test_lesson_signature_uses_unicode_casefolding_and_whitespace() -> None:
    one = lesson_signature("Straße\ncheck", "\tMISS\ted", "Prevent  it")
    two = lesson_signature("STRASSE check", "miss ed", "prevent it")
    assert one == two


def test_lesson_signature_keeps_causal_differences_distinct() -> None:
    first = lesson_signature("same invariant", "cause one", "same prevention")
    second = lesson_signature("same invariant", "cause two", "same prevention")
    assert first != second


def test_incident_and_lesson_records_are_immutable() -> None:
    created_at = datetime(2026, 7, 29, tzinfo=UTC)
    incident = IncidentRecord(
        id="inc_01",
        capture_attempt_id="cap_01",
        created_at=created_at,
        draft=IncidentDraft(
            outcome_summary="Migration failed",
            expected_invariant="Schema validates",
            controllable_cause="Preflight skipped",
            material_impact="Deployment delayed",
            recurrence_risk="High",
        ),
    )
    lesson = LessonRecord(id="les_01", created_at=created_at)
    lesson_version = LessonVersionRecord(
        id="lvr_01",
        lesson_id=lesson.id,
        version_number=1,
        created_at=created_at,
        state=LessonState.PROPOSED,
        signature=lesson_signature("Schema validates", "Preflight skipped", "Run validation"),
        draft=LessonDraft(
            title="Validate migrations",
            rule="Validate before writes",
            prevention_action="Run validation",
            verification_action="Check dry run",
            applicability="Database migrations",
            counterexamples="Read-only changes",
        ),
    )
    result = RecordResult(
        incident_id=incident.id,
        lesson_id=lesson.id,
        lesson_version_id=lesson_version.id,
        relation=IncidentLessonRelation.SAME_CAUSE_SAME_INVARIANT,
        created_new_lesson=True,
    )

    with pytest.raises(FrozenInstanceError):
        incident.id = "inc_02"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        lesson.id = "les_02"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        lesson_version.state = LessonState.VERIFIED  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.created_new_lesson = False  # type: ignore[misc]
