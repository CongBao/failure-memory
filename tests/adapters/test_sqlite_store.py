import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest

from failure_memory.adapters.event_store.sqlite.connection import connect_sqlite
from failure_memory.adapters.event_store.sqlite.migrate import apply_migrations
from failure_memory.adapters.event_store.sqlite.store import SQLiteEventStore
from failure_memory.adapters.harness.context import HarnessContext
from failure_memory.domain.capture import (
    CaptureAssessment,
    CaptureDecision,
    Classification,
    ExpectationSource,
    FailureCandidate,
    ReasonCode,
)
from failure_memory.domain.learning import GeneralizationRecommendation
from failure_memory.domain.records import (
    IncidentDraft,
    IncidentLessonRelation,
    LessonDraft,
    LessonState,
    lesson_signature,
)
from failure_memory.ports.event_store import EventStorePort

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
def drafts() -> tuple[IncidentDraft, LessonDraft, HarnessContext]:
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
        HarnessContext(
            harness="pytest",
            data_root=Path("/test-data"),
            workspace_fingerprint="workspace-fingerprint",
            session_fingerprint="session-fingerprint",
        ),
    )


def _candidate() -> FailureCandidate:
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


def _assessment(decision: CaptureDecision) -> CaptureAssessment:
    if decision is CaptureDecision.ACCEPT:
        reason_codes = (ReasonCode.REAL_FAILURE_CRITERIA_MET,)
    else:
        reason_codes = (ReasonCode.UNCERTAIN_CLASSIFICATION,)
    return CaptureAssessment(decision=decision, reason_codes=reason_codes, confidence=1.0)


def _append_capture(
    store: SQLiteEventStore,
    context: HarnessContext,
    decision: CaptureDecision = CaptureDecision.ACCEPT,
) -> str:
    return store.append_capture(
        _candidate(),
        _assessment(decision),
        context,
        created_at=CREATED_AT,
        redaction_state="clean",
    )


@pytest.fixture
def rejected_capture_id(store: SQLiteEventStore, context: HarnessContext) -> str:
    return _append_capture(store, context, CaptureDecision.REJECT)


@pytest.fixture
def accepted_capture_id(store: SQLiteEventStore, context: HarnessContext) -> str:
    return _append_capture(store, context)


@pytest.fixture
def accepted_capture_ids(store: SQLiteEventStore, context: HarnessContext) -> tuple[str, str]:
    return (_append_capture(store, context), _append_capture(store, context))


def test_rejected_capture_cannot_create_incident(
    store: SQLiteEventStore,
    rejected_capture_id: str,
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
) -> None:
    incident, lesson, context = drafts

    with pytest.raises(ValueError, match="capture attempt is not accepted"):
        store.record_incident_and_lesson(
            rejected_capture_id,
            incident,
            lesson,
            context,
            created_at=CREATED_AT,
            redaction_state="clean",
        )

    assert store.counts()["incident"] == 0


def test_unknown_capture_cannot_create_any_record(
    store: SQLiteEventStore,
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
) -> None:
    """Would fail if a fabricated capture ID bypassed the accepted-capture boundary."""
    incident, lesson, context = drafts

    with pytest.raises(ValueError, match="capture attempt not found"):
        store.record_incident_and_lesson(
            "cap_01J00000000000000000000000",
            incident,
            lesson,
            context,
            created_at=CREATED_AT,
            redaction_state="clean",
        )

    assert store.counts() == {
        "capture_attempt": 0,
        "incident": 0,
        "lesson": 0,
        "lesson_version": 0,
        "incident_lesson_relation": 0,
    }


@pytest.mark.parametrize(
    ("recommendation", "candidate_ids", "message"),
    [
        (
            GeneralizationRecommendation.REUSE_EXACT,
            (),
            "exact reuse review requires exactly one candidate",
        ),
        (
            GeneralizationRecommendation.REVIEW_RELATED,
            (),
            "related review requires at least one candidate",
        ),
        (
            GeneralizationRecommendation.CREATE_DISTINCT,
            ("lv_candidate",),
            "distinct review cannot include candidates",
        ),
        (
            GeneralizationRecommendation.REVIEW_RELATED,
            ("lv_1", "lv_2", "lv_3", "lv_4"),
            "at most three candidates",
        ),
    ],
)
def test_generalization_review_rejects_inconsistent_candidate_sets(
    store: SQLiteEventStore,
    context: HarnessContext,
    accepted_capture_id: str,
    recommendation: GeneralizationRecommendation,
    candidate_ids: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        store.append_generalization_review(
            accepted_capture_id,
            "signature",
            recommendation,
            "sqlite-fts5",
            candidate_ids,
            context,
            created_at=CREATED_AT,
            redaction_state="clean",
        )


def test_append_capture_serializes_utc_timestamp_and_reason_codes(
    store: SQLiteEventStore, context: HarnessContext
) -> None:
    capture_id = _append_capture(store, context)

    row = store.connection.execute(
        "SELECT created_at, reason_codes_json FROM capture_attempt WHERE id = ?", (capture_id,)
    ).fetchone()

    assert row is not None
    assert row["created_at"] == "2026-07-29T12:00:00+00:00"
    assert row["reason_codes_json"] == '["real_failure_criteria_met"]'


def test_append_capture_normalizes_non_utc_values_and_preserves_optional_null(
    store: SQLiteEventStore,
    context: HarnessContext,
) -> None:
    """Would fail if adapter serialization preserved offsets or invented optional chronology."""
    offset = timezone(timedelta(hours=5, minutes=30))
    created_at = datetime(2026, 7, 29, 12, 0, tzinfo=offset)
    candidate = replace(
        _candidate(),
        expectation_established_at=None,
        observed_outcome_at=created_at,
    )

    capture_id = store.append_capture(
        candidate,
        _assessment(CaptureDecision.ACCEPT),
        context,
        created_at=created_at,
        redaction_state="clean",
    )

    row = store.connection.execute(
        """
        SELECT created_at, expectation_established_at, observed_outcome_at
        FROM capture_attempt
        WHERE id = ?
        """,
        (capture_id,),
    ).fetchone()
    assert row is not None
    assert dict(row) == {
        "created_at": "2026-07-29T06:30:00+00:00",
        "expectation_established_at": None,
        "observed_outcome_at": "2026-07-29T06:30:00+00:00",
    }


def test_event_store_port_reports_the_exact_database_path(
    store: SQLiteEventStore, tmp_path: Path
) -> None:
    """Would fail if the port omitted adapter diagnostics or reported a derived workspace path."""
    port: EventStorePort = store

    assert port.database_path() == str(tmp_path / "failure-memory.sqlite3")


def test_same_signature_keeps_both_incidents_but_reuses_lesson(
    store: SQLiteEventStore,
    accepted_capture_ids: tuple[str, str],
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
) -> None:
    incident, lesson, context = drafts

    first = store.record_incident_and_lesson(
        accepted_capture_ids[0],
        incident,
        lesson,
        context,
        created_at=CREATED_AT,
        redaction_state="clean",
    )
    second = store.record_incident_and_lesson(
        accepted_capture_ids[1],
        incident,
        lesson,
        context,
        created_at=CREATED_AT,
        redaction_state="clean",
    )

    assert first.created_new_lesson is True
    assert second.created_new_lesson is False
    assert first.lesson_id == second.lesson_id
    assert first.lesson_version_id == second.lesson_version_id
    assert first.relation is IncidentLessonRelation.NOVEL
    assert second.relation is IncidentLessonRelation.SAME_CAUSE_SAME_INVARIANT
    assert store.counts() == {
        "capture_attempt": 2,
        "incident": 2,
        "lesson": 1,
        "lesson_version": 1,
        "incident_lesson_relation": 2,
    }


def test_concurrent_same_signature_records_share_one_current_lesson(
    tmp_path: Path,
    context: HarnessContext,
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
) -> None:
    database_path = tmp_path / "concurrent.sqlite3"
    bootstrap_connection = connect_sqlite(database_path)
    apply_migrations(bootstrap_connection)
    bootstrap_store = SQLiteEventStore(bootstrap_connection)
    capture_ids = (
        _append_capture(bootstrap_store, context),
        _append_capture(bootstrap_store, context),
    )
    bootstrap_connection.close()

    incident, lesson, record_context = drafts
    start = Barrier(2)
    first_find_complete = Event()
    second_begin = Event()
    second_find_complete = Event()
    begin_statements: list[str] = []
    begin_lock = Lock()

    def trace_second(statement: str) -> None:
        if statement.startswith("BEGIN"):
            with begin_lock:
                begin_statements.append(statement)
            second_begin.set()

    def record(role: str, capture_id: str) -> object:
        connection = connect_sqlite(database_path)
        store = SQLiteEventStore(connection)
        try:
            if role == "first":
                first_find = store.find_lesson_by_signature

                def wait_after_first_find(
                    signature: str,
                    workspace_fingerprint: str | None = None,
                ) -> object:
                    found = first_find(signature, workspace_fingerprint)
                    first_find_complete.set()
                    assert second_begin.wait(timeout=5)
                    with begin_lock:
                        second_uses_deferred_begin = begin_statements == ["BEGIN"]
                    if second_uses_deferred_begin:
                        assert second_find_complete.wait(timeout=5)
                    return found

                store.find_lesson_by_signature = wait_after_first_find  # type: ignore[method-assign]
            else:
                connection.set_trace_callback(trace_second)
                second_find = store.find_lesson_by_signature

                def signal_second_find(
                    signature: str,
                    workspace_fingerprint: str | None = None,
                ) -> object:
                    found = second_find(signature, workspace_fingerprint)
                    second_find_complete.set()
                    return found

                store.find_lesson_by_signature = signal_second_find  # type: ignore[method-assign]
            start.wait(timeout=5)
            if role == "second":
                assert first_find_complete.wait(timeout=5)
            return store.record_incident_and_lesson(
                capture_id,
                incident,
                lesson,
                record_context,
                created_at=CREATED_AT,
                redaction_state="clean",
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(record, "first", capture_ids[0])
        second = executor.submit(record, "second", capture_ids[1])
        first_result = first.result(timeout=10)
        second_result = second.result(timeout=10)

    assert begin_statements == ["BEGIN IMMEDIATE"]
    assert first_result.lesson_id == second_result.lesson_id
    assert first_result.lesson_version_id == second_result.lesson_version_id
    verification_connection = connect_sqlite(database_path)
    verification_store = SQLiteEventStore(verification_connection)
    assert verification_store.counts() == {
        "capture_attempt": 2,
        "incident": 2,
        "lesson": 1,
        "lesson_version": 1,
        "incident_lesson_relation": 2,
    }
    verification_connection.close()


def test_same_signature_in_different_workspaces_reuses_global_lesson(
    store: SQLiteEventStore,
    context: HarnessContext,
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
) -> None:
    incident, lesson, first_context = drafts
    second_context = replace(
        first_context,
        workspace_fingerprint="another-workspace-fingerprint",
    )
    first_capture = _append_capture(store, context)
    second_capture = _append_capture(store, context)

    first = store.record_incident_and_lesson(
        first_capture,
        incident,
        lesson,
        first_context,
        created_at=CREATED_AT,
        redaction_state="clean",
    )
    second = store.record_incident_and_lesson(
        second_capture,
        incident,
        lesson,
        second_context,
        created_at=CREATED_AT,
        redaction_state="clean",
    )

    assert first.created_new_lesson is True
    assert second.created_new_lesson is False
    assert first.lesson_id == second.lesson_id
    assert first.lesson_version_id == second.lesson_version_id
    signature = lesson_signature(
        incident.expected_invariant,
        incident.controllable_cause,
        lesson.prevention_action,
    )
    assert store.find_lesson_by_signature(signature)


def test_append_capture_retries_busy_writes_then_raises_stable_storage_error(
    tmp_path: Path,
    context: HarnessContext,
) -> None:
    """Would fail if expected SQLITE_BUSY contention was not retried and classified."""
    database = tmp_path / "busy-capture.sqlite3"
    writer = connect_sqlite(database)
    apply_migrations(writer)
    writer.execute("PRAGMA busy_timeout = 1")
    blocker = connect_sqlite(database)
    blocker.execute("BEGIN IMMEDIATE")
    traces: list[str] = []
    writer.set_trace_callback(traces.append)
    store = SQLiteEventStore(writer)
    try:
        with pytest.raises(RuntimeError) as caught:
            _append_capture(store, context)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        writer.close()

    assert type(caught.value).__name__ == "StorageBusyError"
    inserts = [
        statement for statement in traces if statement.lstrip().startswith("INSERT INTO capture")
    ]
    assert len(inserts) == 3


def test_record_transaction_retries_busy_begin_without_partial_incident(
    tmp_path: Path,
    context: HarnessContext,
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
) -> None:
    """Would fail if transaction contention became internal_error or left partial records."""
    database = tmp_path / "busy-record.sqlite3"
    writer = connect_sqlite(database)
    apply_migrations(writer)
    writer.execute("PRAGMA busy_timeout = 1")
    store = SQLiteEventStore(writer)
    capture_id = _append_capture(store, context)
    blocker = connect_sqlite(database)
    blocker.execute("BEGIN IMMEDIATE")
    traces: list[str] = []
    writer.set_trace_callback(traces.append)
    incident, lesson, record_context = drafts
    try:
        with pytest.raises(RuntimeError) as caught:
            store.record_incident_and_lesson(
                capture_id,
                incident,
                lesson,
                record_context,
                created_at=CREATED_AT,
                redaction_state="clean",
            )
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert type(caught.value).__name__ == "StorageBusyError"
    assert sum(statement == "BEGIN IMMEDIATE" for statement in traces) == 3
    assert store.counts()["incident"] == 0
    writer.close()


def test_record_retries_locked_capture_lookup_before_writing(
    store: SQLiteEventStore,
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Would fail if contention before BEGIN escaped the bounded write policy."""
    attempts = 0

    def locked_lookup(_capture_attempt_id: str) -> CaptureDecision:
        nonlocal attempts
        attempts += 1
        error = sqlite3.OperationalError("database table is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_LOCKED
        raise error

    monkeypatch.setattr(store, "get_capture_decision", locked_lookup)
    incident, lesson, context = drafts

    with pytest.raises(RuntimeError) as caught:
        store.record_incident_and_lesson(
            "cap_locked",
            incident,
            lesson,
            context,
            created_at=CREATED_AT,
            redaction_state="clean",
        )

    assert type(caught.value).__name__ == "StorageBusyError"
    assert attempts == 3
    assert store.counts()["incident"] == 0


def test_raw_rows_preserve_capture_context_and_record_payloads(
    store: SQLiteEventStore,
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
) -> None:
    incident, lesson, context = drafts
    capture_id = _append_capture(store, context)
    result = store.record_incident_and_lesson(
        capture_id,
        incident,
        lesson,
        context,
        created_at=CREATED_AT,
        redaction_state="clean",
    )

    capture = store.connection.execute(
        "SELECT * FROM capture_attempt WHERE id = ?", (capture_id,)
    ).fetchone()
    stored_incident = store.connection.execute(
        "SELECT * FROM incident WHERE id = ?", (result.incident_id,)
    ).fetchone()
    stored_lesson = store.connection.execute(
        "SELECT * FROM lesson WHERE id = ?", (result.lesson_id,)
    ).fetchone()
    version = store.connection.execute(
        "SELECT * FROM lesson_version WHERE id = ?", (result.lesson_version_id,)
    ).fetchone()
    head = store.connection.execute(
        "SELECT * FROM lesson_head WHERE lesson_id = ?", (result.lesson_id,)
    ).fetchone()
    relation = store.connection.execute(
        "SELECT * FROM incident_lesson_relation WHERE incident_id = ?", (result.incident_id,)
    ).fetchone()

    assert capture is not None
    assert stored_incident is not None
    assert stored_lesson is not None
    assert version is not None
    assert head is not None
    assert relation is not None
    assert capture["id"].startswith("cap_")
    assert stored_incident["id"].startswith("inc_")
    assert stored_lesson["id"].startswith("les_")
    assert version["id"].startswith("lv_")
    assert relation["id"].startswith("rel_")
    assert dict(capture) == {
        "id": capture_id,
        "schema_version": 1,
        "created_at": "2026-07-29T12:00:00+00:00",
        "source_harness": "pytest",
        "workspace_fingerprint": "workspace-fingerprint",
        "session_fingerprint": "session-fingerprint",
        "provenance": "local",
        "redaction_state": "clean",
        "summary": "A known schema invariant was missed.",
        "classification": "real_failure",
        "decision": "accept",
        "confidence": 1.0,
        "reason_codes_json": '["real_failure_criteria_met"]',
        "expectation_source": "accepted_design",
        "expectation_established_at": "2026-07-29T11:59:00+00:00",
        "observed_outcome_at": "2026-07-29T12:00:00+00:00",
            "failure_portion_summary": None,
            "policy_version": "tier1-v1",
            "expectation_preexisted": None,
            "expectation_evidence": None,
        }
    assert dict(stored_incident) == {
        "id": result.incident_id,
        "schema_version": 1,
        "created_at": "2026-07-29T12:00:00+00:00",
        "source_harness": "pytest",
        "workspace_fingerprint": "workspace-fingerprint",
        "session_fingerprint": "session-fingerprint",
        "provenance": "local",
        "redaction_state": "clean",
        "capture_attempt_id": capture_id,
        "outcome_summary": "The migration wrote incompatible rows.",
        "expected_invariant": "Writes must preserve the schema contract.",
        "controllable_cause": "The preflight check was skipped.",
        "material_impact": "The release was delayed.",
        "recurrence_risk": "Future migrations can repeat the failure.",
    }
    assert dict(stored_lesson) == {
        "id": result.lesson_id,
        "schema_version": 1,
        "created_at": "2026-07-29T12:00:00+00:00",
        "source_harness": "pytest",
        "workspace_fingerprint": "workspace-fingerprint",
        "session_fingerprint": "session-fingerprint",
        "provenance": "local",
        "redaction_state": "clean",
    }
    assert dict(version) == {
        "id": result.lesson_version_id,
        "schema_version": 1,
        "created_at": "2026-07-29T12:00:00+00:00",
        "source_harness": "pytest",
        "workspace_fingerprint": "workspace-fingerprint",
        "session_fingerprint": "session-fingerprint",
        "provenance": "local",
        "redaction_state": "clean",
        "lesson_id": result.lesson_id,
        "version_number": 1,
        "lifecycle_state": "proposed",
        "signature": "abea346f25b1c6a88be9e5b3b37224f9afedb943c3d6b55ea34d593fa3840ac2",
        "title": "Validate migrations before writing",
        "rule": "Run the schema preflight before every migration write.",
        "prevention_action": "Run the schema preflight.",
        "verification_action": "Confirm the preflight output is clean.",
        "applicability": "Schema-changing migrations.",
        "counterexamples": "Read-only diagnostic queries.",
    }
    assert dict(head) == {
        "lesson_id": result.lesson_id,
        "lesson_version_id": result.lesson_version_id,
        "updated_at": "2026-07-29T12:00:00+00:00",
    }
    assert dict(relation) == {
        "id": relation["id"],
        "schema_version": 1,
        "created_at": "2026-07-29T12:00:00+00:00",
        "source_harness": "pytest",
        "workspace_fingerprint": "workspace-fingerprint",
        "session_fingerprint": "session-fingerprint",
        "provenance": "local",
        "redaction_state": "clean",
        "incident_id": result.incident_id,
        "lesson_id": result.lesson_id,
        "lesson_version_id": result.lesson_version_id,
        "relation_type": "novel",
        "confidence": 1.0,
    }


def test_recording_is_atomic_when_relation_insert_fails(
    store: SQLiteEventStore,
    accepted_capture_id: str,
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incident, lesson, context = drafts

    def fail_relation(*args: object, **kwargs: object) -> None:
        raise sqlite3.IntegrityError("forced relation failure")

    monkeypatch.setattr(store, "_insert_relation", fail_relation)
    with pytest.raises(sqlite3.IntegrityError, match="forced relation failure"):
        store.record_incident_and_lesson(
            accepted_capture_id,
            incident,
            lesson,
            context,
            created_at=CREATED_AT,
            redaction_state="clean",
        )

    assert store.counts() == {
        "capture_attempt": 1,
        "incident": 0,
        "lesson": 0,
        "lesson_version": 0,
        "incident_lesson_relation": 0,
    }


def test_duplicate_recording_for_one_capture_rolls_back_the_second_attempt(
    store: SQLiteEventStore,
    accepted_capture_id: str,
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
) -> None:
    incident, lesson, context = drafts
    store.record_incident_and_lesson(
        accepted_capture_id,
        incident,
        lesson,
        context,
        created_at=CREATED_AT,
        redaction_state="clean",
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.record_incident_and_lesson(
            accepted_capture_id,
            incident,
            lesson,
            context,
            created_at=CREATED_AT,
            redaction_state="clean",
        )

    assert store.counts() == {
        "capture_attempt": 1,
        "incident": 1,
        "lesson": 1,
        "lesson_version": 1,
        "incident_lesson_relation": 1,
    }


def test_capture_decision_and_lesson_row_are_mapped_to_domain_records(
    store: SQLiteEventStore,
    accepted_capture_id: str,
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
) -> None:
    incident, lesson, context = drafts
    result = store.record_incident_and_lesson(
        accepted_capture_id,
        incident,
        lesson,
        context,
        created_at=CREATED_AT,
        redaction_state="clean",
    )
    signature = lesson_signature(
        incident.expected_invariant, incident.controllable_cause, lesson.prevention_action
    )

    found = store.find_lesson_by_signature(signature)

    assert store.get_capture_decision(accepted_capture_id) is CaptureDecision.ACCEPT
    assert found is not None
    assert found.id == result.lesson_version_id
    assert found.lesson_id == result.lesson_id
    assert found.version_number == 1
    assert found.created_at == CREATED_AT
    assert found.state is LessonState.PROPOSED
    assert found.signature == signature
    assert found.draft == lesson
    assert store.integrity_check() == "ok"


def test_signature_alias_resolves_to_the_current_lesson_head(
    store: SQLiteEventStore,
    accepted_capture_id: str,
    drafts: tuple[IncidentDraft, LessonDraft, HarnessContext],
) -> None:
    incident, lesson, context = drafts
    result = store.record_incident_and_lesson(
        accepted_capture_id,
        incident,
        lesson,
        context,
        created_at=CREATED_AT,
        redaction_state="clean",
    )
    original_signature = lesson_signature(
        incident.expected_invariant, incident.controllable_cause, lesson.prevention_action
    )
    store.connection.execute(
        """
        INSERT INTO lesson_version(
            id, schema_version, created_at, source_harness, workspace_fingerprint,
            session_fingerprint, provenance, redaction_state, lesson_id, version_number,
            lifecycle_state, signature, title, rule, prevention_action, verification_action,
            applicability, counterexamples
        ) VALUES (?, 1, ?, 'pytest', 'workspace-fingerprint', 'session-fingerprint',
                  'local', 'clean', ?, 2, 'verified', 'new-signature', 'new title', 'new rule',
                  'new action', 'new verification', 'new applicability', 'new counterexamples')
        """,
        ("lv_current", CREATED_AT.isoformat(), result.lesson_id),
    )
    store.connection.execute(
        "UPDATE lesson_head SET lesson_version_id = ?, updated_at = ? WHERE lesson_id = ?",
        ("lv_current", CREATED_AT.isoformat(), result.lesson_id),
    )

    matched = store.find_lesson_by_signature(original_signature)
    assert matched is not None
    assert matched.id == "lv_current"
