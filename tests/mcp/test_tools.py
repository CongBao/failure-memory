from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from failure_memory.domain.capture import Classification, ExpectationSource
from failure_memory.mcp.tools import TOOLS


def _tool(name: str) -> dict[str, object]:
    return next(tool.as_mcp_dict() for tool in TOOLS if tool.name == name)


def test_tools_publish_the_public_operations() -> None:
    """Would fail if a public operation were renamed, omitted, or added."""
    expected = (
        "evaluate_failure_candidate",
        "record_failure_incident",
        "find_related_failures",
        "recall_failure_lessons",
        "record_recall_outcome",
        "get_failure_memory_metrics",
        "get_failure_recall_metrics",
        "failure_memory_retrieval_status",
        "build_failure_memory_index",
        "get_failure_learning_metrics",
        "failure_memory_store_status",
        "transition_failure_lesson",
        "run_failure_ranking_experiment",
        "propose_failure_lesson_clusters",
        "failure_memory_setup_status",
        "failure_memory_doctor",
    )

    assert len(TOOLS) == 16
    assert tuple(tool.name for tool in TOOLS) == expected
    assert {tool.name for tool in TOOLS} == set(expected)


def test_tool_definitions_cannot_be_mutated_through_nested_schema_data() -> None:
    """Would fail if one caller could silently alter another MCP client's advertised contract."""
    with pytest.raises(TypeError):
        TOOLS[0].input_schema["type"] = "array"
    nested_properties = TOOLS[0].input_schema["properties"]
    assert isinstance(nested_properties, dict) is False
    with pytest.raises(TypeError):
        nested_properties["summary"]["minLength"] = 0


def test_mcp_dictionary_is_a_fresh_json_serializable_copy() -> None:
    """Would fail if a caller could mutate future discovery responses or serialize proxy objects."""
    first = TOOLS[0].as_mcp_dict()
    second = TOOLS[0].as_mcp_dict()
    first["inputSchema"]["properties"]["summary"]["minLength"] = 99

    assert second["inputSchema"]["properties"]["summary"]["minLength"] == 1
    assert json.loads(json.dumps(second))["name"] == "evaluate_failure_candidate"


def test_all_tools_publish_object_schemas_and_safe_annotations() -> None:
    """Would fail if clients could not discover a complete, non-destructive tool contract."""
    writes = {
        "evaluate_failure_candidate",
        "record_failure_incident",
        "recall_failure_lessons",
        "record_recall_outcome",
        "build_failure_memory_index",
        "transition_failure_lesson",
        "run_failure_ranking_experiment",
        "propose_failure_lesson_clusters",
    }

    for tool in TOOLS:
        published = tool.as_mcp_dict()
        assert published["inputSchema"]["type"] == "object"
        assert published["outputSchema"]["type"] == "object"
        assert published["annotations"]["destructiveHint"] is False
        assert published["annotations"]["readOnlyHint"] is (tool.name not in writes)


def test_output_schemas_accept_exactly_closed_success_or_error_payloads() -> None:
    """Would fail if an MCP error result did not conform to the same advertised output schema."""
    for tool in TOOLS:
        schema = tool.as_mcp_dict()["outputSchema"]

        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert len(schema["oneOf"]) == 2
        error_payload = {"error": {"code": "internal_error", "message": "safe message"}}
        _assert_schema_valid(error_payload, schema)
        _assert_schema_invalid(
            {"error": {"code": "internal_error", "message": "safe message"}, "extra": True},
            schema,
        )


def test_schema_validator_enforces_format_length_bounds_and_one_of() -> None:
    """Would fail if the published JSON Schemas stopped enforcing their declared constraints."""
    evaluate = _tool("evaluate_failure_candidate")
    valid = {
        "summary": "Known invariant missed.",
        "classification": "real_failure",
        "expectation_source": "accepted_design",
        "observed_outcome_at": "2026-07-29T12:00:00Z",
        "outcome_mismatch": True,
        "material_impact_or_recurrence_risk": True,
        "controllable_with_prior_information": True,
        "durable_lesson": True,
    }
    _assert_schema_valid(valid, evaluate["inputSchema"])
    _assert_schema_invalid({**valid, "summary": ""}, evaluate["inputSchema"])
    _assert_schema_invalid(
        {**valid, "observed_outcome_at": "not-a-date-time"},
        evaluate["inputSchema"],
    )

    success = {
        "capture_attempt_id": "cap_01",
        "decision": "accept",
        "reason_codes": ["real_failure_criteria_met"],
        "confidence": 1.0,
        "policy_version": "tier1-v1",
    }
    _assert_schema_valid(success, evaluate["outputSchema"])
    _assert_schema_invalid({**success, "confidence": 1.01}, evaluate["outputSchema"])
    _assert_schema_invalid(
        {**success, "error": {"code": "internal_error", "message": "not exclusive"}},
        evaluate["outputSchema"],
    )


def test_recall_schema_requires_exact_fields_or_context_plus_discriminator() -> None:
    schema = _tool("recall_failure_lessons")["inputSchema"]

    _assert_schema_valid(
        {
            "mode": "auto",
            "expected_invariant": "Writes preserve the schema.",
            "controllable_cause": "The preflight was skipped.",
            "prevention_action": "Run the preflight.",
        },
        schema,
    )
    _assert_schema_valid(
        {
            "mode": "hybrid",
            "text": "Prepare a migration.",
            "component": "migration",
            "top_k": 3,
        },
        schema,
    )
    _assert_schema_invalid({"text": "Find something similar."}, schema)
    _assert_schema_invalid({"text": "Task", "component": "migration", "top_k": 6}, schema)


def test_evaluate_schema_requires_complete_candidate_and_domain_enums() -> None:
    """Would fail if callers could omit qualification evidence or submit undeclared enums."""
    schema = _tool("evaluate_failure_candidate")["inputSchema"]

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "summary",
        "classification",
        "expectation_source",
        "observed_outcome_at",
        "outcome_mismatch",
        "material_impact_or_recurrence_risk",
        "controllable_with_prior_information",
        "durable_lesson",
    }
    assert schema["properties"]["classification"]["enum"] == [
        member.value for member in Classification
    ]
    assert schema["properties"]["expectation_source"]["enum"] == [
        member.value for member in ExpectationSource
    ]
    assert schema["properties"]["observed_outcome_at"]["format"] == "date-time"
    assert schema["properties"]["expectation_established_at"]["format"] == "date-time"


def test_record_schema_rejects_undeclared_nested_fields() -> None:
    """Would fail if a nested incident or lesson could silently accept undocumented data."""
    schema = _tool("record_failure_incident")["inputSchema"]

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"capture_attempt_id", "incident", "lesson"}
    assert schema["properties"]["incident"]["additionalProperties"] is False
    assert set(schema["properties"]["incident"]["required"]) == {
        "outcome_summary",
        "expected_invariant",
        "controllable_cause",
        "material_impact",
        "recurrence_risk",
    }
    assert schema["properties"]["lesson"]["additionalProperties"] is False
    assert set(schema["properties"]["lesson"]["required"]) == {
        "title",
        "rule",
        "prevention_action",
        "verification_action",
        "applicability",
        "counterexamples",
    }


def test_lookup_and_status_schemas_have_stable_argument_boundaries() -> None:
    """Would fail if exact lookup became partial or no-argument tools accepted hidden inputs."""
    lookup = _tool("find_related_failures")["inputSchema"]

    assert lookup["additionalProperties"] is False
    assert set(lookup["required"]) == {
        "expected_invariant",
        "controllable_cause",
        "prevention_action",
    }
    for name in (
        "get_failure_memory_metrics",
        "get_failure_recall_metrics",
        "get_failure_learning_metrics",
        "failure_memory_retrieval_status",
        "failure_memory_store_status",
        "run_failure_ranking_experiment",
        "failure_memory_setup_status",
        "failure_memory_doctor",
    ):
        schema = _tool(name)["inputSchema"]
        assert schema["additionalProperties"] is False
        assert schema["properties"] == {}
        assert schema["required"] == []


def _assert_schema_valid(instance: object, schema: object) -> None:
    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(instance)
    )
    assert not errors, "\n".join(error.message for error in errors)


def _assert_schema_invalid(instance: object, schema: object) -> None:
    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(instance)
    )
    assert errors
