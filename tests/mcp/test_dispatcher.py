from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from failure_memory.adapters.event_store.sqlite.connection import connect_sqlite
from failure_memory.adapters.event_store.sqlite.migrate import apply_migrations
from failure_memory.adapters.event_store.sqlite.store import SQLiteEventStore
from failure_memory.adapters.harness.context import HarnessContext
from failure_memory.application.service import FailureMemoryService
from failure_memory.mcp.dispatcher import dispatch_tool
from failure_memory.mcp.tools import TOOLS

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def service(tmp_path: Path) -> FailureMemoryService:
    """A real service prevents dispatcher tests from only exercising a test double."""
    connection = connect_sqlite(tmp_path / "failure-memory.sqlite3")
    apply_migrations(connection)
    store = SQLiteEventStore(connection)
    context = HarnessContext.create(
        data_root=tmp_path / "data",
        cwd=tmp_path / "workspace",
        harness="pytest",
        session_id="mcp-tests",
    )
    yield FailureMemoryService(store, context, clock=lambda: NOW)
    connection.close()


def _candidate(**changes: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "summary": "The migration skipped a known preflight check.",
        "classification": "real_failure",
        "expectation_source": "accepted_design",
        "expectation_established_at": (NOW - timedelta(minutes=1)).isoformat(),
        "observed_outcome_at": NOW.isoformat(),
        "outcome_mismatch": True,
        "material_impact_or_recurrence_risk": True,
        "controllable_with_prior_information": True,
        "durable_lesson": True,
    }
    candidate.update(changes)
    return candidate


def _drafts(capture_attempt_id: str, **changes: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "capture_attempt_id": capture_attempt_id,
        "causal_assessment_id": "cas_not_diagnosed",
        "generalization_review_id": "fgr_not_reviewed",
        "disposition": "create_distinct",
        "rationale_code": "test_rationale",
        "incident": {
            "outcome_summary": "The deployment was delayed.",
            "expected_invariant": "Migrations must run the preflight check.",
            "controllable_cause": "The required check was skipped.",
            "material_impact": "The release was delayed.",
            "recurrence_risk": "Future migrations can repeat the delay.",
        },
        "lesson": {
            "title": "Run migration preflight checks",
            "rule": "Run the preflight before every migration write.",
            "prevention_action": "Run the migration preflight check.",
            "verification_action": "Confirm the preflight output is clean.",
            "applicability": "Schema-changing migrations.",
            "counterexamples": "Read-only diagnostics.",
        },
    }
    arguments.update(changes)
    return arguments


def _reviewed_drafts(
    service: FailureMemoryService, capture_attempt_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    arguments = _drafts(capture_attempt_id)
    diagnosis = dispatch_tool(
        "diagnose_failure_cause",
        {
            "capture_attempt_id": capture_attempt_id,
            "state": "supported",
            "factors": [
                {
                    "role": "primary",
                    "layer": "skill_instruction",
                    "failure_mode": "ignored",
                    "component_reference": "skill:migration-preflight",
                    "evidence_summary": "The accepted preflight instruction was available.",
                    "confidence": "high",
                }
            ],
            "recommendations": [
                {
                    "target_layer": "skill_instruction",
                    "target_reference": "skill:migration-preflight",
                    "recommended_change": "Make the preflight a required first step.",
                    "verification_action": "Run a fixture that omits the preflight.",
                    "rationale": "The failure originated at the instruction boundary.",
                    "confidence": "high",
                }
            ],
        },
        service,
    )
    _assert_envelope(diagnosis)
    causal_assessment_id = diagnosis["structuredContent"]["causal_assessment_id"]
    assert isinstance(causal_assessment_id, str)
    arguments["causal_assessment_id"] = causal_assessment_id
    review_input = {
        key: arguments[key]
        for key in ("capture_attempt_id", "causal_assessment_id", "incident", "lesson")
    }
    review = dispatch_tool("review_failure_recording", review_input, service)
    _assert_envelope(review)
    review_id = review["structuredContent"]["review_id"]
    assert isinstance(review_id, str)
    arguments["generalization_review_id"] = review_id
    return arguments, review


def _assert_envelope(result: dict[str, object]) -> None:
    assert result["isError"] is False
    assert isinstance(result["structuredContent"], dict)
    assert result["content"] and result["content"][0]["type"] == "text"
    assert isinstance(result["content"][0]["text"], str)


def _assert_valid_tool_result(name: str, result: dict[str, object]) -> None:
    schema = next(tool.as_mcp_dict()["outputSchema"] for tool in TOOLS if tool.name == name)
    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            result["structuredContent"]
        )
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_missing_candidate_field_is_an_invalid_arguments_result(
    service: FailureMemoryService,
) -> None:
    """Would fail if incomplete candidate evidence reached the qualification policy."""
    arguments = _candidate()
    arguments.pop("summary")

    result = dispatch_tool("evaluate_failure_candidate", arguments, service)

    assert result["isError"] is True
    assert result["structuredContent"] == {
        "error": {"code": "invalid_arguments", "message": "summary is required"}
    }
    _assert_valid_tool_result("evaluate_failure_candidate", result)


def test_accepted_failure_records_cause_repair_and_outcome_before_lesson_review(
    service: FailureMemoryService,
) -> None:
    evaluated = dispatch_tool("evaluate_failure_candidate", _candidate(), service)
    capture_attempt_id = evaluated["structuredContent"]["capture_attempt_id"]
    assert isinstance(capture_attempt_id, str)
    diagnosed = dispatch_tool(
        "diagnose_failure_cause",
        {
            "capture_attempt_id": capture_attempt_id,
            "state": "supported",
            "factors": [
                {
                    "role": "primary",
                    "layer": "hook_policy",
                    "failure_mode": "not_triggered",
                    "component_reference": "hook:user-prompt-submit",
                    "evidence_summary": "Only a session-start hook was configured.",
                    "confidence": "high",
                }
            ],
            "recommendations": [
                {
                    "target_layer": "hook_policy",
                    "target_reference": "hook:user-prompt-submit",
                    "recommended_change": "Add a bounded prompt-submission context hook.",
                    "verification_action": "Submit a correction and inspect injected context.",
                    "rationale": "Prompt-time feedback needs a prompt-time trigger.",
                    "confidence": "high",
                }
            ],
        },
        service,
    )

    _assert_envelope(diagnosed)
    _assert_valid_tool_result("diagnose_failure_cause", diagnosed)
    diagnosis_payload = diagnosed["structuredContent"]
    causal_assessment_id = diagnosis_payload["causal_assessment_id"]
    recommendation_id = diagnosis_payload["recommendations"][0]["recommendation_id"]
    assert isinstance(causal_assessment_id, str)
    assert isinstance(recommendation_id, str)

    feedback = dispatch_tool(
        "record_failure_repair_outcome",
        {
            "recommendation_id": recommendation_id,
            "outcome": "applied",
            "detail_code": "hook_added",
            "evidence_summary": "The prompt hook is present in the host projection.",
            "confidence": "high",
        },
        service,
    )
    _assert_envelope(feedback)
    _assert_valid_tool_result("record_failure_repair_outcome", feedback)

    metrics = dispatch_tool("get_failure_learning_metrics", {}, service)
    assert metrics["structuredContent"]["causal_assessment_count"] == 1
    assert metrics["structuredContent"]["causal_assessment_coverage"] == 1.0
    assert metrics["structuredContent"]["repair_recommendation_count"] == 1
    assert metrics["structuredContent"]["applied_recommendation_count"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("classification", "retroactive_failure"),
        ("expectation_source", "newly_invented_source"),
    ],
)
def test_invalid_candidate_enums_are_invalid_arguments(
    service: FailureMemoryService, field: str, value: str
) -> None:
    """Would fail if a wire enum bypassed the domain's closed vocabulary."""
    result = dispatch_tool("evaluate_failure_candidate", _candidate(**{field: value}), service)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_arguments"
    assert field in result["structuredContent"]["error"]["message"]
    _assert_valid_tool_result("evaluate_failure_candidate", result)


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_outcome_at": "2026-07-29T12:00:00"},
        {"observed_outcome_at": "not-a-date-time"},
        {"summary": "   "},
        {"failure_portion_summary": ""},
        {"outcome_mismatch": 1},
        {"unexpected": "field"},
    ],
)
def test_candidate_boundary_values_are_rejected(
    service: FailureMemoryService, changes: dict[str, object]
) -> None:
    """Would fail if malformed boundary values entered durable capture."""
    result = dispatch_tool("evaluate_failure_candidate", _candidate(**changes), service)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_arguments"
    _assert_valid_tool_result("evaluate_failure_candidate", result)


@pytest.mark.parametrize(
    ("wire_value", "valid"),
    [
        ("2026-07-29T12:00:00Z", True),
        ("2026-07-29t12:00:00z", True),
        ("2026-07-29T12:00:00.123456+08:30", True),
        ("2026-07-29t12:00:00-00:00", True),
        ("20260729T120000+00:00", False),
        ("2026-07-29 12:00:00+00:00", False),
        ("2026-07-29T12:00:00+0000", False),
        ("2026-07-29T12:00:00", False),
        ("2026-02-30T12:00:00Z", False),
        ("2026-07-29T12:00:00Z\n", False),
    ],
)
def test_rfc3339_schema_and_runtime_accept_the_same_wire_values(
    service: FailureMemoryService,
    wire_value: str,
    valid: bool,
) -> None:
    """Would fail if advertised date-time inputs and the runtime parser diverged."""
    schema = next(
        tool.as_mcp_dict()["inputSchema"]
        for tool in TOOLS
        if tool.name == "evaluate_failure_candidate"
    )
    arguments = _candidate(observed_outcome_at=wire_value)
    schema_errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(arguments)
    )

    result = dispatch_tool("evaluate_failure_candidate", arguments, service)

    assert (not schema_errors) is valid
    assert (result["isError"] is False) is valid
    if not valid:
        assert result["structuredContent"]["error"]["code"] == "invalid_arguments"


def test_requirement_update_returns_reject_decision_in_structured_content(
    service: FailureMemoryService,
) -> None:
    """Would fail if a requirement change were reported as a captureable failure."""
    result = dispatch_tool(
        "evaluate_failure_candidate", _candidate(classification="requirement_update"), service
    )

    _assert_envelope(result)
    assert result["structuredContent"]["decision"] == "reject"
    assert result["structuredContent"]["reason_codes"] == ["not_preexisting_requirement"]


def test_generalization_proposal_tools_are_schema_valid_at_the_mcp_boundary(
    service: FailureMemoryService,
) -> None:
    listed = dispatch_tool("list_failure_generalization_proposals", {}, service)

    _assert_envelope(listed)
    _assert_valid_tool_result("list_failure_generalization_proposals", listed)
    assert listed["structuredContent"] == {
        "scope": "global_personal",
        "proposals": [],
    }

    invalid_review = dispatch_tool(
        "review_failure_generalization_proposal",
        {
            "proposal_id": "lgp_missing",
            "decision": "accept",
            "rationale_code": "reviewed_related_failures",
            "unexpected": True,
        },
        service,
    )
    assert invalid_review["isError"] is True
    assert invalid_review["structuredContent"]["error"]["code"] == "invalid_arguments"
    _assert_valid_tool_result("review_failure_generalization_proposal", invalid_review)


def test_real_sqlite_contention_returns_stable_mcp_busy_result(
    service: FailureMemoryService,
) -> None:
    """Would fail if real SQLITE_BUSY contention was serialized as internal_error."""
    database = Path(service.store.database_path())
    service.store.connection.execute("PRAGMA busy_timeout = 1")
    blocker = connect_sqlite(database)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        result = dispatch_tool("evaluate_failure_candidate", _candidate(), service)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert result == {
        "content": [
            {
                "type": "text",
                "text": "Failure-memory storage is busy; retry the operation.",
            }
        ],
        "structuredContent": {
            "error": {
                "code": "busy",
                "message": "Failure-memory storage is busy; retry the operation.",
            }
        },
        "isError": True,
    }
    _assert_valid_tool_result("evaluate_failure_candidate", result)


def test_recording_an_accepted_failure_returns_all_durable_identifiers(
    service: FailureMemoryService,
) -> None:
    """Would fail if the MCP write result hid an ID needed for later exact lookup and auditing."""
    evaluated = dispatch_tool("evaluate_failure_candidate", _candidate(), service)
    capture_attempt_id = evaluated["structuredContent"]["capture_attempt_id"]
    assert isinstance(capture_attempt_id, str)

    arguments, _review = _reviewed_drafts(service, capture_attempt_id)
    result = dispatch_tool("record_failure_incident", arguments, service)

    _assert_envelope(result)
    payload = result["structuredContent"]
    assert set(payload) == {
        "incident_id",
        "lesson_id",
        "lesson_version_id",
        "relation",
        "created_new_lesson",
        "generalization_decision_id",
    }
    assert all(
        isinstance(payload[key], str) and payload[key] for key in payload if key.endswith("_id")
    )
    assert payload["relation"] == "novel"
    assert payload["created_new_lesson"] is True


def test_nested_draft_extra_field_is_an_invalid_arguments_result(
    service: FailureMemoryService,
) -> None:
    """Would fail if undocumented incident fields were silently ignored or persisted."""
    evaluated = dispatch_tool("evaluate_failure_candidate", _candidate(), service)
    capture_attempt_id = evaluated["structuredContent"]["capture_attempt_id"]
    assert isinstance(capture_attempt_id, str)
    arguments = _drafts(capture_attempt_id)
    incident = arguments["incident"]
    assert isinstance(incident, dict)
    incident["unreviewed_note"] = "do not persist"

    result = dispatch_tool("record_failure_incident", arguments, service)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_arguments"
    assert "incident.unreviewed_note" in result["structuredContent"]["error"]["message"]
    _assert_valid_tool_result("record_failure_incident", result)


def test_recording_rejected_capture_maps_service_value_error_to_operation_rejected(
    service: FailureMemoryService,
) -> None:
    """Would fail if a service rejection were exposed as a client-input error or leaked text."""
    evaluated = dispatch_tool(
        "evaluate_failure_candidate", _candidate(classification="requirement_update"), service
    )
    capture_attempt_id = evaluated["structuredContent"]["capture_attempt_id"]
    assert isinstance(capture_attempt_id, str)

    result = dispatch_tool("record_failure_incident", _drafts(capture_attempt_id), service)

    assert result["isError"] is True
    assert result["structuredContent"] == {
        "error": {
            "code": "operation_rejected",
            "message": "The failure-memory service rejected the operation.",
        }
    }
    _assert_valid_tool_result("record_failure_incident", result)


class _SecretRejectingService:
    def evaluate_failure_candidate(self, _: object) -> object:
        raise ValueError("password=correct-horse-battery-staple")


class _ExplodingService:
    def evaluate_failure_candidate(self, _: object) -> object:
        raise RuntimeError("token=secret-should-never-reach-client")


def test_service_value_error_is_sanitized_as_operation_rejected() -> None:
    """Would fail if a service error could disclose credential-bearing diagnostic text."""
    result = dispatch_tool("evaluate_failure_candidate", _candidate(), _SecretRejectingService())

    assert result == {
        "content": [{"type": "text", "text": "The failure-memory service rejected the operation."}],
        "structuredContent": {
            "error": {
                "code": "operation_rejected",
                "message": "The failure-memory service rejected the operation.",
            }
        },
        "isError": True,
    }
    assert "correct-horse" not in str(result)
    _assert_valid_tool_result("evaluate_failure_candidate", result)


def test_unexpected_service_exception_is_sanitized_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Would fail if unexpected service failures leaked text or became untraceable to operators."""
    caplog.set_level(logging.ERROR, logger="failure_memory.mcp.dispatcher")

    result = dispatch_tool("evaluate_failure_candidate", _candidate(), _ExplodingService())

    assert result == {
        "content": [{"type": "text", "text": "Internal failure-memory error."}],
        "structuredContent": {
            "error": {"code": "internal_error", "message": "Internal failure-memory error."}
        },
        "isError": True,
    }
    assert "secret-should" not in str(result)
    dispatcher_records = [
        record for record in caplog.records if record.name == "failure_memory.mcp.dispatcher"
    ]
    assert len(dispatcher_records) == 1
    assert dispatcher_records[0].exc_info is not None
    assert dispatcher_records[0].exc_info[1] is not None
    assert "secret-should-never-reach-client" in str(dispatcher_records[0].exc_info[1])
    assert dispatcher_records[0].getMessage() == (
        "Unexpected failure-memory MCP tool failure for evaluate_failure_candidate"
    )
    _assert_valid_tool_result("evaluate_failure_candidate", result)


def test_exact_lookup_returns_none_when_no_lesson_matches(service: FailureMemoryService) -> None:
    """Would fail if an absent exact match were mistaken for a related lesson."""
    result = dispatch_tool(
        "find_related_failures",
        {
            "expected_invariant": "Every invoice must balance.",
            "controllable_cause": "The ledger query omitted a currency.",
            "prevention_action": "Check every currency before posting.",
        },
        service,
    )

    _assert_envelope(result)
    assert result["structuredContent"] == {"found": False, "lesson": None}


@pytest.mark.parametrize(
    "name",
    [
        "get_failure_memory_metrics",
        "get_failure_recall_metrics",
        "get_failure_learning_metrics",
        "failure_memory_retrieval_status",
        "build_failure_memory_index",
        "failure_memory_store_status",
        "run_failure_ranking_experiment",
        "failure_memory_setup_status",
        "failure_memory_doctor",
    ],
)
def test_empty_argument_tools_reject_extra_fields(service: FailureMemoryService, name: str) -> None:
    """Would fail if a no-argument read tool acquired an undocumented input channel."""
    result = dispatch_tool(name, {"unexpected": True}, service)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_arguments"
    _assert_valid_tool_result(name, result)


def test_unknown_tool_returns_a_protocol_error_without_raising(
    service: FailureMemoryService,
) -> None:
    """Would fail if an unknown public tool could terminate the stdio request loop."""
    result = dispatch_tool("erase_failure_memory", {}, service)

    assert result == {
        "content": [{"type": "text", "text": "Unknown tool: erase_failure_memory"}],
        "structuredContent": {
            "error": {"code": "unknown_tool", "message": "Unknown tool: erase_failure_memory"}
        },
        "isError": True,
    }


def test_cluster_proposal_without_semantic_adapter_returns_setup_required(
    service: FailureMemoryService,
) -> None:
    result = dispatch_tool("propose_failure_lesson_clusters", {}, service)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "setup_required"
    _assert_valid_tool_result("propose_failure_lesson_clusters", result)


def test_every_success_payload_has_the_published_output_shape(
    service: FailureMemoryService,
) -> None:
    """Would fail if a dispatcher returned fields outside its advertised output schema."""
    evaluated = dispatch_tool("evaluate_failure_candidate", _candidate(), service)
    capture_attempt_id = evaluated["structuredContent"]["capture_attempt_id"]
    assert isinstance(capture_attempt_id, str)
    record_arguments, reviewed = _reviewed_drafts(service, capture_attempt_id)
    recorded = dispatch_tool("record_failure_incident", record_arguments, service)
    lesson_id = recorded["structuredContent"]["lesson_id"]
    assert isinstance(lesson_id, str)
    calls = {
        "evaluate_failure_candidate": evaluated,
        "review_failure_recording": reviewed,
        "record_failure_incident": recorded,
        "find_related_failures": dispatch_tool(
            "find_related_failures",
            {
                "expected_invariant": "Migrations must run the preflight check.",
                "controllable_cause": "The required check was skipped.",
                "prevention_action": "Run the migration preflight check.",
            },
            service,
        ),
        "get_failure_memory_metrics": dispatch_tool("get_failure_memory_metrics", {}, service),
        "get_failure_recall_metrics": dispatch_tool("get_failure_recall_metrics", {}, service),
        "get_failure_learning_metrics": dispatch_tool("get_failure_learning_metrics", {}, service),
        "failure_memory_retrieval_status": dispatch_tool(
            "failure_memory_retrieval_status", {}, service
        ),
        "build_failure_memory_index": dispatch_tool("build_failure_memory_index", {}, service),
        "failure_memory_store_status": dispatch_tool("failure_memory_store_status", {}, service),
        "run_failure_ranking_experiment": dispatch_tool(
            "run_failure_ranking_experiment", {}, service
        ),
        "transition_failure_lesson": dispatch_tool(
            "transition_failure_lesson",
            {
                "lesson_id": lesson_id,
                "to_state": "verified",
                "rationale_code": "human_review_confirmed",
            },
            service,
        ),
        "failure_memory_setup_status": dispatch_tool("failure_memory_setup_status", {}, service),
        "failure_memory_doctor": dispatch_tool("failure_memory_doctor", {}, service),
    }

    for name, result in calls.items():
        _assert_envelope(result)
        _assert_valid_tool_result(name, result)


def test_recall_and_false_positive_feedback_follow_published_contracts(
    service: FailureMemoryService,
) -> None:
    evaluated = dispatch_tool("evaluate_failure_candidate", _candidate(), service)
    capture_attempt_id = evaluated["structuredContent"]["capture_attempt_id"]
    assert isinstance(capture_attempt_id, str)
    recorded = dispatch_tool(
        "record_failure_incident",
        _reviewed_drafts(service, capture_attempt_id)[0],
        service,
    )
    lesson_version_id = recorded["structuredContent"]["lesson_version_id"]
    assert isinstance(lesson_version_id, str)

    recalled = dispatch_tool(
        "recall_failure_lessons",
        {
            "mode": "auto",
            "expected_invariant": "Migrations must run the preflight check.",
            "controllable_cause": "The required check was skipped.",
            "prevention_action": "Run the migration preflight check.",
        },
        service,
    )
    _assert_envelope(recalled)
    _assert_valid_tool_result("recall_failure_lessons", recalled)
    attempt_id = recalled["structuredContent"]["attempt_id"]
    assert isinstance(attempt_id, str)
    assert recalled["structuredContent"]["executed_mode"] == "exact"

    feedback = dispatch_tool(
        "record_recall_outcome",
        {
            "attempt_id": attempt_id,
            "lesson_version_id": lesson_version_id,
            "outcome": "false_positive",
            "detail_code": "different_failure_mechanism",
            "confidence": 0.8,
        },
        service,
    )

    _assert_envelope(feedback)
    _assert_valid_tool_result("record_recall_outcome", feedback)
    assert feedback["structuredContent"]["outcome_event_id"].startswith("ro_")
