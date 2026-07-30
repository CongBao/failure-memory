from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from failure_memory.application.service import FailureMemoryService, create_local_service
from failure_memory.domain.capture import Classification, ExpectationSource, FailureCandidate
from failure_memory.domain.records import IncidentDraft, LessonDraft, lesson_signature

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _database(root: Path) -> Path:
    return root / "adapters" / "event-store" / "sqlite" / "primary" / "failure-memory.sqlite3"


def _record_lesson(service: FailureMemoryService) -> tuple[str, str]:
    evaluated = service.evaluate_failure_candidate(
        FailureCandidate(
            summary="A known migration preflight was skipped.",
            classification=Classification.REAL_FAILURE,
            expectation_source=ExpectationSource.ACCEPTED_DESIGN,
            expectation_established_at=NOW - timedelta(minutes=1),
            observed_outcome_at=NOW,
            outcome_mismatch=True,
            material_impact_or_recurrence_risk=True,
            controllable_with_prior_information=True,
            durable_lesson=True,
        )
    )
    recorded = service.record_failure_incident(
        evaluated.capture_attempt_id,
        IncidentDraft(
            outcome_summary="The deployment wrote incompatible rows.",
            expected_invariant="Migration writes preserve the schema contract.",
            controllable_cause="The required schema preflight was skipped.",
            material_impact="The release was delayed.",
            recurrence_risk="A later migration could repeat the failure.",
        ),
        LessonDraft(
            title="Run migration preflight checks",
            rule="Validate compatibility before every schema migration write.",
            prevention_action="Run the schema preflight before migration writes.",
            verification_action="Confirm the preflight result is clean.",
            applicability="Schema-changing migrations.",
            counterexamples="Read-only diagnostics.",
        ),
    )
    return recorded.lesson_id, recorded.lesson_version_id


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_copy_only_import_is_global_idempotent_and_preserves_source_bytes(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "codex-store"
    target_root = tmp_path / "global-store"
    source = create_local_service(
        data_root=source_root,
        cwd=tmp_path / "workspace-a",
        harness="codex",
        session_id="source-session",
    )
    lesson_id, lesson_version_id = _record_lesson(source)
    source.close()
    source_database = _database(source_root)
    source_digest = _sha256(source_database)

    target = create_local_service(
        data_root=target_root,
        cwd=tmp_path / "workspace-b",
        harness="claude",
        session_id="target-session",
    )
    assert target.store_importer is not None
    plan = target.store_importer.plan(source_database)

    assert plan.can_apply is True
    assert plan.already_imported is False
    assert plan.importable_counts["capture_attempt"] == 1
    assert plan.importable_counts["lesson"] == 1
    assert plan.conflicts == ()

    imported = target.store_importer.apply(source_database)

    assert imported.already_imported is False
    assert imported.imported_counts["capture_attempt"] == 1
    assert target.metrics() == {
        "capture_attempt": 1,
        "incident": 1,
        "lesson": 1,
        "lesson_version": 1,
        "incident_lesson_relation": 1,
    }
    found = target.store.find_lesson_by_signature(
        lesson_signature(
            "Migration writes preserve the schema contract.",
            "The required schema preflight was skipped.",
            "Run the schema preflight before migration writes.",
        )
    )
    assert found is not None
    assert found.lesson_id == lesson_id
    assert found.id == lesson_version_id
    assert _sha256(source_database) == source_digest

    repeated = target.store_importer.apply(source_database)

    assert repeated.already_imported is True
    assert all(count == 0 for count in repeated.imported_counts.values())
    assert target.store_importer.status()["import_count"] == 1
    assert _sha256(source_database) == source_digest
    target.close()


def test_import_aborts_before_writes_on_conflicting_record_id(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source = create_local_service(
        data_root=source_root,
        cwd=tmp_path / "workspace",
        harness="codex",
    )
    _record_lesson(source)
    source.close()
    source_database = _database(source_root)

    target = create_local_service(
        data_root=target_root,
        cwd=tmp_path / "workspace",
        harness="cursor",
    )
    assert target.store_importer is not None
    source_connection = sqlite3.connect(source_database)
    source_connection.row_factory = sqlite3.Row
    source_row = source_connection.execute("SELECT * FROM capture_attempt").fetchone()
    source_connection.close()
    assert source_row is not None
    conflicting = dict(source_row)
    conflicting["summary"] = "Different content under the same immutable identifier."
    columns = tuple(conflicting)
    target.store.connection.execute(
        f"INSERT INTO capture_attempt({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(conflicting[column] for column in columns),
    )

    plan = target.store_importer.plan(source_database)

    assert plan.can_apply is False
    assert plan.conflicts == (f"capture_attempt:{conflicting['id']}",)
    with pytest.raises(ValueError, match="record ID collisions"):
        target.store_importer.apply(source_database)
    assert (
        target.store.connection.execute("SELECT COUNT(*) FROM source_store_import").fetchone()[0]
        == 0
    )
    assert (
        target.store.connection.execute("SELECT COUNT(*) FROM capture_attempt").fetchone()[0] == 1
    )
    target.close()
