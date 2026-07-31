from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from failure_memory.adapters.event_store.sqlite.connection import connect_sqlite
from failure_memory.adapters.event_store.sqlite.migrate import apply_migrations
from failure_memory.adapters.event_store.sqlite.store import SQLiteEventStore
from failure_memory.adapters.harness.context import HarnessContext
from failure_memory.application import service as service_module
from failure_memory.application.service import FailureMemoryService
from failure_memory.domain.capture import (
    CaptureDecision,
    Classification,
    ExpectationSource,
    FailureCandidate,
)
from failure_memory.domain.records import IncidentDraft, LessonDraft, LessonState

CREATED_AT = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteEventStore:
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    apply_migrations(connection)
    yield SQLiteEventStore(connection)
    connection.close()


@pytest.fixture
def context(tmp_path: Path) -> HarnessContext:
    return HarnessContext.create(
        data_root=tmp_path / "data",
        cwd=tmp_path / "workspace",
        harness="pytest",
        session_id="session-1",
    )


@pytest.fixture
def service(store: SQLiteEventStore, context: HarnessContext) -> FailureMemoryService:
    return FailureMemoryService(store, context, clock=lambda: CREATED_AT)


@pytest.fixture
def real_candidate() -> FailureCandidate:
    return FailureCandidate(
        summary="A known schema invariant was missed.",
        classification=Classification.REAL_FAILURE,
        expectation_source=ExpectationSource.ACCEPTED_DESIGN,
        expectation_established_at=CREATED_AT - timedelta(minutes=1),
        observed_outcome_at=CREATED_AT,
        outcome_mismatch=True,
        material_impact_or_recurrence_risk=True,
        controllable_with_prior_information=True,
        durable_lesson=True,
    )


@pytest.fixture
def update_candidate(real_candidate: FailureCandidate) -> FailureCandidate:
    return replace(real_candidate, classification=Classification.REQUIREMENT_UPDATE)


@pytest.fixture
def drafts() -> tuple[IncidentDraft, LessonDraft]:
    return (
        IncidentDraft(
            outcome_summary="The migration wrote incompatible rows.",
            expected_invariant="Writes must preserve the schema contract.",
            controllable_cause="The preflight check was skipped.",
            material_impact="The release was delayed.",
            recurrence_risk="Future migrations can repeat the failure.",
        ),
        LessonDraft(
            title="Validate migrations before writing",
            rule="Run the schema preflight before every migration write.",
            prevention_action="Run the schema preflight.",
            verification_action="Confirm the preflight output is clean.",
            applicability="Schema-changing migrations.",
            counterexamples="Read-only diagnostic queries.",
        ),
    )


def test_rejected_requirement_update_is_stored_only_as_capture(
    service: FailureMemoryService, update_candidate: FailureCandidate
) -> None:
    """Would fail if rejected updates became incidents or were omitted from capture telemetry."""
    result = service.evaluate_failure_candidate(update_candidate)

    assert result.assessment.decision is CaptureDecision.REJECT
    assert service.metrics() == {
        "capture_attempt": 1,
        "incident": 0,
        "lesson": 0,
        "lesson_version": 0,
        "incident_lesson_relation": 0,
    }


def test_deferred_candidate_cannot_create_an_incident(
    service: FailureMemoryService,
    real_candidate: FailureCandidate,
    drafts: tuple[IncidentDraft, LessonDraft],
) -> None:
    """Would fail if a deferred capture bypassed the accepted-capture recording gate."""
    evaluated = service.evaluate_failure_candidate(
        replace(real_candidate, classification=Classification.UNCERTAIN)
    )

    with pytest.raises(ValueError, match="capture attempt is not accepted"):
        service.record_failure_incident(evaluated.capture_attempt_id, *drafts)

    assert evaluated.assessment.decision is CaptureDecision.DEFER
    assert service.metrics() == {
        "capture_attempt": 1,
        "incident": 0,
        "lesson": 0,
        "lesson_version": 0,
        "incident_lesson_relation": 0,
    }


def test_service_redacts_before_durable_recording(
    service: FailureMemoryService,
    real_candidate: FailureCandidate,
    drafts: tuple[IncidentDraft, LessonDraft],
    store: SQLiteEventStore,
) -> None:
    """Would fail if the service forwarded an incident draft before redacting its text."""
    evaluated = service.evaluate_failure_candidate(real_candidate)
    github_token = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"

    result = service.record_failure_incident(
        evaluated.capture_attempt_id,
        replace(drafts[0], outcome_summary=f"leaked {github_token}"),
        drafts[1],
    )

    row = store.connection.execute(
        "SELECT outcome_summary, redaction_state FROM incident WHERE id = ?", (result.incident_id,)
    ).fetchone()

    assert row is not None
    assert row["outcome_summary"] == "leaked [REDACTED:github_token]"
    assert row["redaction_state"] == "redacted"


def test_service_redacts_every_authoritative_text_column(
    service: FailureMemoryService,
    real_candidate: FailureCandidate,
    drafts: tuple[IncidentDraft, LessonDraft],
    store: SQLiteEventStore,
) -> None:
    """Would fail if any capture, incident, or lesson-draft text bypassed redaction."""
    private_key = "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----"
    github_token = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
    openai_project_key = "sk-" + "proj-abcdefghijklmnop"
    candidate = replace(
        real_candidate,
        summary=f"capture {github_token}",
        failure_portion_summary="Bearer abcdefghijklmnopqrstuvwxyz123456",
    )
    incident = IncidentDraft(
        outcome_summary=f"outcome {openai_project_key}",
        expected_invariant=private_key,
        controllable_cause=f"cause {github_token}",
        material_impact="impact sk-abcdefghijklmnop",
        recurrence_risk="risk Bearer abcdefghijklmnopqrstuvwxyz123456",
    )
    lesson = LessonDraft(
        title=f"title {github_token}",
        rule=f"rule {openai_project_key}",
        prevention_action="prevent Bearer abcdefghijklmnopqrstuvwxyz123456",
        verification_action=private_key,
        applicability=f"apply {github_token}",
        counterexamples="exception sk-abcdefghijklmnop",
    )

    evaluated = service.evaluate_failure_candidate(candidate)
    result = service.record_failure_incident(evaluated.capture_attempt_id, incident, lesson)

    capture = store.connection.execute(
        """
        SELECT summary, failure_portion_summary, redaction_state
        FROM capture_attempt WHERE id = ?
        """,
        (evaluated.capture_attempt_id,),
    ).fetchone()
    stored_incident = store.connection.execute(
        """
        SELECT outcome_summary, expected_invariant, controllable_cause, material_impact,
               recurrence_risk, redaction_state
        FROM incident WHERE id = ?
        """,
        (result.incident_id,),
    ).fetchone()
    version = store.connection.execute(
        """
        SELECT title, rule, prevention_action, verification_action, applicability,
               counterexamples, redaction_state
        FROM lesson_version WHERE id = ?
        """,
        (result.lesson_version_id,),
    ).fetchone()

    assert capture is not None
    assert stored_incident is not None
    assert version is not None
    durable_text = " ".join(
        str(value)
        for row in (capture, stored_incident, version)
        for column, value in dict(row).items()
        if column != "redaction_state"
    )
    assert "ghp_" not in durable_text
    assert "sk-" not in durable_text
    assert "Bearer " not in durable_text
    assert "BEGIN PRIVATE KEY" not in durable_text
    assert capture["redaction_state"] == "redacted"
    assert stored_incident["redaction_state"] == "redacted"
    assert version["redaction_state"] == "redacted"


def test_secret_only_in_failure_portion_marks_capture_redacted(
    service: FailureMemoryService,
    real_candidate: FailureCandidate,
    store: SQLiteEventStore,
) -> None:
    """Would fail if capture state ignored a secret outside the primary summary."""
    evaluated = service.evaluate_failure_candidate(
        replace(
            real_candidate,
            failure_portion_summary="portion sk-abcdefghijklmno-",
        )
    )

    row = store.connection.execute(
        """
        SELECT summary, failure_portion_summary, redaction_state
        FROM capture_attempt WHERE id = ?
        """,
        (evaluated.capture_attempt_id,),
    ).fetchone()

    assert row is not None
    assert dict(row) == {
        "summary": "A known schema invariant was missed.",
        "failure_portion_summary": "portion [REDACTED:openai_key]",
        "redaction_state": "redacted",
    }


def test_clean_capture_and_record_stay_clean(
    service: FailureMemoryService,
    real_candidate: FailureCandidate,
    drafts: tuple[IncidentDraft, LessonDraft],
    store: SQLiteEventStore,
) -> None:
    """Would fail if combining clean redaction results marked durable rows as redacted."""
    evaluated = service.evaluate_failure_candidate(real_candidate)
    result = service.record_failure_incident(evaluated.capture_attempt_id, *drafts)

    states = store.connection.execute(
        """
        SELECT
            (SELECT redaction_state FROM capture_attempt WHERE id = ?) AS capture_state,
            (SELECT redaction_state FROM incident WHERE id = ?) AS incident_state,
            (SELECT redaction_state FROM lesson WHERE id = ?) AS lesson_state,
            (SELECT redaction_state FROM lesson_version WHERE id = ?) AS version_state,
            (
                SELECT redaction_state FROM incident_lesson_relation WHERE incident_id = ?
            ) AS relation_state
        """,
        (
            evaluated.capture_attempt_id,
            result.incident_id,
            result.lesson_id,
            result.lesson_version_id,
            result.incident_id,
        ),
    ).fetchone()

    assert states is not None
    assert dict(states) == {
        "capture_state": "clean",
        "incident_state": "clean",
        "lesson_state": "clean",
        "version_state": "clean",
        "relation_state": "clean",
    }


@pytest.mark.parametrize(
    ("target", "expected_recurrence_risk", "expected_counterexamples"),
    [
        (
            "incident",
            "risk [REDACTED:github_token]",
            "Read-only diagnostic queries.",
        ),
        (
            "lesson",
            "Future migrations can repeat the failure.",
            "exception [REDACTED:openai_key]",
        ),
    ],
)
def test_secret_only_in_last_draft_field_marks_record_redacted(
    service: FailureMemoryService,
    real_candidate: FailureCandidate,
    drafts: tuple[IncidentDraft, LessonDraft],
    store: SQLiteEventStore,
    target: str,
    expected_recurrence_risk: str,
    expected_counterexamples: str,
) -> None:
    """Would fail if the final incident or lesson field were omitted from combined state."""
    incident, lesson = drafts
    if target == "incident":
        incident = replace(
            incident,
            recurrence_risk="risk " + "ghp_" + "abcdefghijklmnopqrst",
        )
    else:
        lesson = replace(lesson, counterexamples="exception sk-abcdefghijklmno-")
    evaluated = service.evaluate_failure_candidate(real_candidate)
    result = service.record_failure_incident(
        evaluated.capture_attempt_id,
        incident,
        lesson,
    )

    row = store.connection.execute(
        """
        SELECT incident.recurrence_risk,
               incident.redaction_state AS incident_state,
               lesson.redaction_state AS lesson_state,
               lesson_version.counterexamples,
               lesson_version.redaction_state AS version_state,
               relation.redaction_state AS relation_state
        FROM incident
        JOIN lesson ON lesson.id = ?
        JOIN lesson_version ON lesson_version.id = ?
        JOIN incident_lesson_relation AS relation ON relation.incident_id = incident.id
        WHERE incident.id = ?
        """,
        (result.lesson_id, result.lesson_version_id, result.incident_id),
    ).fetchone()

    assert row is not None
    assert dict(row) == {
        "recurrence_risk": expected_recurrence_risk,
        "incident_state": "redacted",
        "lesson_state": "redacted",
        "counterexamples": expected_counterexamples,
        "version_state": "redacted",
        "relation_state": "redacted",
    }


def test_related_lookup_uses_the_incident_and_lesson_signature(
    service: FailureMemoryService,
    real_candidate: FailureCandidate,
    drafts: tuple[IncidentDraft, LessonDraft],
) -> None:
    """Would fail if lookup used a different signature than durable lesson recording."""
    evaluated = service.evaluate_failure_candidate(real_candidate)
    recorded = service.record_failure_incident(evaluated.capture_attempt_id, *drafts)

    related = service.find_related_failures(
        drafts[0].expected_invariant,
        drafts[0].controllable_cause,
        drafts[1].prevention_action,
    )

    assert related is not None
    assert related.id == recorded.lesson_version_id


def test_related_lookup_redacts_all_raw_signature_components(
    service: FailureMemoryService,
    real_candidate: FailureCandidate,
) -> None:
    """Would fail if lookup left any signature component raw while recording redacts it."""
    expected_invariant = "invariant " + "ghp_" + "abcdefghijklmnopqrst"
    controllable_cause = "cause sk-abcdefghijklmno-"
    prevention_action = "prevent Bearer abcdefghijklmnopqrstuvwxyz123456"
    evaluated = service.evaluate_failure_candidate(real_candidate)
    result = service.record_failure_incident(
        evaluated.capture_attempt_id,
        IncidentDraft(
            outcome_summary="recorded outcome",
            expected_invariant=expected_invariant,
            controllable_cause=controllable_cause,
            material_impact="recorded impact",
            recurrence_risk="recorded risk",
        ),
        LessonDraft(
            title="recorded lesson",
            rule="recorded rule",
            prevention_action=prevention_action,
            verification_action="recorded verification",
            applicability="recorded applicability",
            counterexamples="recorded counterexamples",
        ),
    )

    related = service.find_related_failures(
        expected_invariant,
        controllable_cause,
        prevention_action,
    )

    assert related is not None
    assert related.id == result.lesson_version_id


def test_reviewed_lifecycle_transition_appends_a_version_and_preserves_history(
    service: FailureMemoryService,
    real_candidate: FailureCandidate,
    drafts: tuple[IncidentDraft, LessonDraft],
    store: SQLiteEventStore,
) -> None:
    evaluated = service.evaluate_failure_candidate(real_candidate)
    recorded = service.record_failure_incident(evaluated.capture_attempt_id, *drafts)

    transitioned = service.transition_lesson(
        recorded.lesson_id,
        LessonState.VERIFIED,
        "human_review_confirmed",
    )

    assert transitioned["prior_version_id"] == recorded.lesson_version_id
    assert transitioned["version_number"] == 2
    assert transitioned["from_state"] == "proposed"
    assert transitioned["to_state"] == "verified"
    versions = store.connection.execute(
        """
        SELECT id, version_number, lifecycle_state
        FROM lesson_version
        WHERE lesson_id = ?
        ORDER BY version_number
        """,
        (recorded.lesson_id,),
    ).fetchall()
    assert [tuple(row) for row in versions] == [
        (recorded.lesson_version_id, 1, "proposed"),
        (transitioned["new_version_id"], 2, "verified"),
    ]
    assert (
        store.connection.execute("SELECT COUNT(*) FROM lesson_lifecycle_event").fetchone()[0] == 1
    )
    related = service.find_related_failures(
        drafts[0].expected_invariant,
        drafts[0].controllable_cause,
        drafts[1].prevention_action,
    )
    assert related is not None
    assert related.id == transitioned["new_version_id"]


def test_terminal_lesson_state_cannot_be_transitioned_again(
    service: FailureMemoryService,
    real_candidate: FailureCandidate,
    drafts: tuple[IncidentDraft, LessonDraft],
) -> None:
    evaluated = service.evaluate_failure_candidate(real_candidate)
    recorded = service.record_failure_incident(evaluated.capture_attempt_id, *drafts)
    service.transition_lesson(
        recorded.lesson_id,
        LessonState.DEPRECATED,
        "superseded_by_current_practice",
    )

    with pytest.raises(ValueError, match="transition is not allowed"):
        service.transition_lesson(
            recorded.lesson_id,
            LessonState.VERIFIED,
            "late_review",
        )


def test_incident_redaction_preserves_and_scans_future_named_optional_fields() -> None:
    """Would fail if draft reconstruction dropped a future field or bypassed its redaction."""

    @dataclass(frozen=True)
    class ExtendedIncidentDraft(IncidentDraft):
        optional_note: str | None = None

    draft = ExtendedIncidentDraft(
        outcome_summary="outcome",
        expected_invariant="invariant",
        controllable_cause="cause",
        material_impact="impact",
        recurrence_risk="risk",
        optional_note="note " + "ghp_" + "abcdefghijklmnopqrst",
    )

    redacted, results = service_module._redact_incident(draft)

    assert isinstance(redacted, ExtendedIncidentDraft)
    assert redacted.optional_note == "note [REDACTED:github_token]"
    assert len(results) == 6
    assert results[-1].state == "redacted"


def test_incident_redaction_preserves_future_null_optional_fields() -> None:
    """Would fail if a future optional field were coerced or passed to text redaction."""

    @dataclass(frozen=True)
    class ExtendedIncidentDraft(IncidentDraft):
        optional_note: str | None = None

    draft = ExtendedIncidentDraft(
        outcome_summary="outcome",
        expected_invariant="invariant",
        controllable_cause="cause",
        material_impact="impact",
        recurrence_risk="risk",
    )

    redacted, results = service_module._redact_incident(draft)

    assert isinstance(redacted, ExtendedIncidentDraft)
    assert redacted.optional_note is None
    assert len(results) == 5


def test_setup_status_matches_the_bootstrap_capability_contract(
    service: FailureMemoryService,
) -> None:
    """Would fail if setup advertised a capability unavailable in the bootstrap profile."""
    assert service.setup_status() == {
        "state": "bootstrap_ready",
        "profile": "bootstrap-sqlite",
        "scope": "global_personal",
        "available_capabilities": [
            "failure_qualification",
            "incident_recording",
            "exact_signature_lookup",
            "global_cross_harness_recall",
            "copy_only_store_import",
            "recall_telemetry",
            "learning_metrics",
                "tier_two_generalization_review",
                "evidence_bounded_causal_diagnosis",
                "single_call_failure_recording",
                "recording_operation_telemetry",
                "repair_recommendation_telemetry",
            "causal_recall_filters",
            "generalization_proposal_review",
            "reviewed_cluster_recall",
            "offline_shadow_evaluation",
            "bounded_session_hook",
            "codex_claude_prompt_failure_check_hook",
        ],
        "unavailable_capabilities": [
            "production_feedback_ranking",
                "copilot_cli_cursor_prompt_context_hook",
            "fts5_recall",
            "semantic_recall",
            "hybrid_recall",
        ],
    }


def test_doctor_reports_adapter_health_without_context_secrets(
    service: FailureMemoryService,
    context: HarnessContext,
    tmp_path: Path,
) -> None:
    """Would fail if diagnostics revealed the workspace or identity-related fingerprints."""
    doctor = service.doctor()

    assert doctor["state"] == "bootstrap_ready"
    assert doctor["profile"] == "bootstrap-sqlite"
    assert doctor["integrity_check"] == "ok"
    assert doctor["counts"] == {
        "capture_attempt": 0,
        "incident": 0,
        "lesson": 0,
        "lesson_version": 0,
        "incident_lesson_relation": 0,
    }
    assert doctor["recall_counts"] == {
        "retrieval_profile_snapshot": 0,
        "recall_attempt": 0,
        "recall_candidate": 0,
        "recall_selection": 0,
        "recall_outcome_event": 0,
        "recall_miss_event": 0,
        "attempt_status_ok": 0,
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
    assert doctor["retrieval"] == {"state": "unavailable"}
    assert str(tmp_path / "workspace") not in str(doctor)
    assert str(context.data_root / "bootstrap" / "identity.key") not in str(doctor)
    assert context.workspace_fingerprint not in str(doctor)
    assert context.session_fingerprint not in str(doctor)
    assert str(tmp_path) not in str(doctor)
