from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, cast

from failure_memory.domain.capture import (
    CaptureDecision,
    Classification,
    ExpectationSource,
    ReasonCode,
)
from failure_memory.domain.learning import GeneralizationProposalDecision
from failure_memory.domain.records import (
    IncidentLessonRelation,
    LessonState,
    RecordingDisposition,
)
from failure_memory.domain.retrieval import RecallMode, RecallOutcomeKind, RecallStatus
from failure_memory.mcp.rfc3339 import RFC3339_DATE_TIME_PATTERN

Schema = Mapping[str, object]

_DRAFT_STRING: Final[Schema] = {"type": "string", "minLength": 1}
_DATE_TIME: Final[Schema] = {
    "type": "string",
    "format": "date-time",
    "pattern": RFC3339_DATE_TIME_PATTERN,
}
_CANDIDATE_PROPERTIES: Final[Schema] = {
    "summary": _DRAFT_STRING,
    "classification": {"type": "string", "enum": [member.value for member in Classification]},
    "expectation_source": {
        "type": "string",
        "enum": [member.value for member in ExpectationSource],
    },
    "expectation_established_at": _DATE_TIME,
    "observed_outcome_at": _DATE_TIME,
    "outcome_mismatch": {"type": "boolean"},
    "material_impact_or_recurrence_risk": {"type": "boolean"},
    "controllable_with_prior_information": {"type": "boolean"},
    "durable_lesson": {"type": "boolean"},
    "failure_portion_summary": _DRAFT_STRING,
}
_INCIDENT_PROPERTIES: Final[Schema] = {
    "outcome_summary": _DRAFT_STRING,
    "expected_invariant": _DRAFT_STRING,
    "controllable_cause": _DRAFT_STRING,
    "material_impact": _DRAFT_STRING,
    "recurrence_risk": _DRAFT_STRING,
}
_LESSON_DRAFT_PROPERTIES: Final[Schema] = {
    "title": _DRAFT_STRING,
    "rule": _DRAFT_STRING,
    "prevention_action": _DRAFT_STRING,
    "verification_action": _DRAFT_STRING,
    "applicability": _DRAFT_STRING,
    "counterexamples": _DRAFT_STRING,
}
_INCIDENT_REQUIRED: Final[list[str]] = list(_INCIDENT_PROPERTIES)
_LESSON_DRAFT_REQUIRED: Final[list[str]] = list(_LESSON_DRAFT_PROPERTIES)


def _object_schema(properties: Schema, required: list[str]) -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _error_payload_schema() -> dict[str, object]:
    return _object_schema(
        {
            "error": _object_schema(
                {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                },
                ["code", "message"],
            )
        },
        ["error"],
    )


def _tool_output_schema(success: Schema) -> dict[str, object]:
    """Accept one closed successful payload or one closed public error payload."""
    success_properties = cast(Schema, success["properties"])
    error_payload = _error_payload_schema()
    error_properties = cast(Schema, error_payload["properties"])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {**success_properties, **error_properties},
        "additionalProperties": False,
        "oneOf": [success, error_payload],
    }


def _record_output_schema() -> Schema:
    return _object_schema(
        {
            "incident_id": _DRAFT_STRING,
            "lesson_id": _DRAFT_STRING,
            "lesson_version_id": _DRAFT_STRING,
            "relation": {
                "type": "string",
                "enum": [member.value for member in IncidentLessonRelation],
            },
            "created_new_lesson": {"type": "boolean"},
            "generalization_decision_id": _DRAFT_STRING,
        },
        [
            "incident_id",
            "lesson_id",
            "lesson_version_id",
            "relation",
            "created_new_lesson",
            "generalization_decision_id",
        ],
    )


def _lesson_output_schema() -> Schema:
    return _object_schema(
        {
            "id": _DRAFT_STRING,
            "lesson_id": _DRAFT_STRING,
            "version_number": {"type": "integer", "minimum": 1},
            "created_at": _DATE_TIME,
            "state": {"type": "string", "enum": [member.value for member in LessonState]},
            "signature": _DRAFT_STRING,
            "draft": _object_schema(_LESSON_DRAFT_PROPERTIES, _LESSON_DRAFT_REQUIRED),
        },
        ["id", "lesson_id", "version_number", "created_at", "state", "signature", "draft"],
    )


def _counts_schema() -> Schema:
    return _object_schema(
        {
            "capture_attempt": {"type": "integer", "minimum": 0},
            "incident": {"type": "integer", "minimum": 0},
            "lesson": {"type": "integer", "minimum": 0},
            "lesson_version": {"type": "integer", "minimum": 0},
            "incident_lesson_relation": {"type": "integer", "minimum": 0},
        },
        [
            "capture_attempt",
            "incident",
            "lesson",
            "lesson_version",
            "incident_lesson_relation",
        ],
    )


def _status_properties() -> Schema:
    return {
        "scope": {"type": "string", "const": "global_personal"},
        "state": _DRAFT_STRING,
        "profile": _DRAFT_STRING,
        "available_capabilities": {"type": "array", "items": _DRAFT_STRING},
        "unavailable_capabilities": {"type": "array", "items": _DRAFT_STRING},
    }


def _recall_counts_schema() -> Schema:
    properties = {
        "retrieval_profile_snapshot": {"type": "integer", "minimum": 0},
        "recall_attempt": {"type": "integer", "minimum": 0},
        "recall_candidate": {"type": "integer", "minimum": 0},
        "recall_selection": {"type": "integer", "minimum": 0},
        "recall_outcome_event": {"type": "integer", "minimum": 0},
        "recall_miss_event": {"type": "integer", "minimum": 0},
        **{
            f"attempt_status_{status}": {"type": "integer", "minimum": 0}
            for status in (
                "ok",
                "no_match",
                "degraded",
                "setup_required",
                "insufficient_evidence",
            )
        },
        **{
            f"outcome_{outcome}": {"type": "integer", "minimum": 0}
            for outcome in (
                "useful",
                "not_useful",
                "false_positive",
                "prevented_recurrence",
                "contradicted_current_task",
                "stale",
                "ignored",
                "unknown",
                "missed_relevant",
            )
        },
    }
    return _object_schema(properties, list(properties))


def _learning_metrics_schema() -> Schema:
    ratio = {"oneOf": [{"type": "null"}, {"type": "number", "minimum": 0, "maximum": 1}]}
    return _object_schema(
        {
            "scope": {"type": "string", "const": "global_personal"},
            "attempt_count": {"type": "integer", "minimum": 0},
            "selection_count": {"type": "integer", "minimum": 0},
            "labeled_attempt_count": {"type": "integer", "minimum": 0},
            "labeled_selection_count": {"type": "integer", "minimum": 0},
            "positive_selection_count": {"type": "integer", "minimum": 0},
            "false_positive_count": {"type": "integer", "minimum": 0},
            "missed_relevant_count": {"type": "integer", "minimum": 0},
            "feedback_coverage": ratio,
            "selection_feedback_coverage": ratio,
            "useful_rate": ratio,
            "false_positive_rate": ratio,
            "precision_at": _object_schema({"1": ratio, "3": ratio}, ["1", "3"]),
            "exact_reuse_count": {"type": "integer", "minimum": 0},
            "exact_reuse_rate": ratio,
            "attempts_by_harness": {
                "type": "object",
                "additionalProperties": {"type": "integer", "minimum": 0},
            },
        },
        [
            "scope",
            "attempt_count",
            "selection_count",
            "labeled_attempt_count",
            "labeled_selection_count",
            "positive_selection_count",
            "false_positive_count",
            "missed_relevant_count",
            "feedback_coverage",
            "selection_feedback_coverage",
            "useful_rate",
            "false_positive_rate",
            "precision_at",
            "exact_reuse_count",
            "exact_reuse_rate",
            "attempts_by_harness",
        ],
    )


def _store_status_schema() -> Schema:
    import_item = _object_schema(
        {
            "import_id": _DRAFT_STRING,
            "created_at": _DATE_TIME,
            "source_store_id": _DRAFT_STRING,
            "content_fingerprint": _DRAFT_STRING,
            "source_schema_version": {"type": "integer", "minimum": 1},
            "imported_counts": {"type": "object", "additionalProperties": {"type": "integer"}},
            "skipped_counts": {"type": "object", "additionalProperties": {"type": "integer"}},
        },
        [
            "import_id",
            "created_at",
            "source_store_id",
            "content_fingerprint",
            "source_schema_version",
            "imported_counts",
            "skipped_counts",
        ],
    )
    return _object_schema(
        {
            "state": _DRAFT_STRING,
            "scope": {"type": "string", "const": "global_personal"},
            "target_store_id": _DRAFT_STRING,
            "import_count": {"type": "integer", "minimum": 0},
            "imports": {"type": "array", "items": import_item},
        },
        ["state", "scope", "target_store_id", "import_count", "imports"],
    )


def _retrieval_status_schema() -> Schema:
    return _object_schema(
        {
            "state": _DRAFT_STRING,
            "profile": _DRAFT_STRING,
            "lexical_available": {"type": "boolean"},
            "semantic_available": {"type": "boolean"},
            "indexed_documents": {"type": "integer", "minimum": 0},
            "detail": {"oneOf": [{"type": "null"}, {"type": "string"}]},
        },
        [
            "state",
            "profile",
            "lexical_available",
            "semantic_available",
            "indexed_documents",
            "detail",
        ],
    )


def _recall_candidate_schema() -> Schema:
    return _object_schema(
        {
            "lesson": _lesson_output_schema(),
            "evidence": _object_schema(
                {
                    "expected_invariant": _DRAFT_STRING,
                    "controllable_cause": _DRAFT_STRING,
                    "outcome_summary": {"type": "string"},
                },
                ["expected_invariant", "controllable_cause", "outcome_summary"],
            ),
            "channels": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["exact", "lexical", "semantic", "cluster"],
                },
                "minItems": 1,
                "uniqueItems": True,
            },
            "score": {"type": "number"},
            "exact": {"type": "boolean"},
            "lexical_rank": {"oneOf": [{"type": "null"}, {"type": "integer", "minimum": 1}]},
            "semantic_rank": {"oneOf": [{"type": "null"}, {"type": "integer", "minimum": 1}]},
            "vector_distance": {"oneOf": [{"type": "null"}, {"type": "number"}]},
            "cluster_review_id": {
                "oneOf": [{"type": "null"}, _DRAFT_STRING],
            },
            "cluster_key": {
                "oneOf": [{"type": "null"}, _DRAFT_STRING],
            },
            "cluster_supporting_lesson_version_ids": {
                "type": "array",
                "items": _DRAFT_STRING,
                "uniqueItems": True,
            },
        },
        [
            "lesson",
            "evidence",
            "channels",
            "score",
            "exact",
            "lexical_rank",
            "semantic_rank",
            "vector_distance",
            "cluster_review_id",
            "cluster_key",
            "cluster_supporting_lesson_version_ids",
        ],
    )


def _recall_input_schema() -> Schema:
    schema = _object_schema(
        {
            "mode": {
                "type": "string",
                "enum": [member.value for member in RecallMode],
            },
            "text": _DRAFT_STRING,
            "expected_invariant": _DRAFT_STRING,
            "controllable_cause": _DRAFT_STRING,
            "prevention_action": _DRAFT_STRING,
            "component": _DRAFT_STRING,
            "top_k": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        [],
    )
    schema["anyOf"] = [
        {
            "required": [
                "expected_invariant",
                "controllable_cause",
                "prevention_action",
            ]
        },
        {"required": ["text", "expected_invariant"]},
        {"required": ["text", "controllable_cause"]},
        {"required": ["text", "prevention_action"]},
        {"required": ["text", "component"]},
    ]
    return schema


def _generalization_proposal_schema() -> Schema:
    return _object_schema(
        {
            "proposal_id": _DRAFT_STRING,
            "cluster_run_id": _DRAFT_STRING,
            "cluster_key": _DRAFT_STRING,
            "supporting_lesson_version_ids": {
                "type": "array",
                "items": _DRAFT_STRING,
                "minItems": 2,
                "uniqueItems": True,
            },
            "counterexample_lesson_version_ids": {
                "type": "array",
                "items": _DRAFT_STRING,
                "uniqueItems": True,
            },
            "status": {
                "type": "string",
                "enum": ["proposed", "accepted", "rejected", "deferred"],
            },
            "latest_review_id": {
                "oneOf": [{"type": "null"}, _DRAFT_STRING],
            },
        },
        [
            "proposal_id",
            "cluster_run_id",
            "cluster_key",
            "supporting_lesson_version_ids",
            "counterexample_lesson_version_ids",
            "status",
            "latest_review_id",
        ],
    )


def _generalized_lesson_input_schema() -> Schema:
    return _object_schema(
        {
            "expected_invariant": _DRAFT_STRING,
            "controllable_cause": _DRAFT_STRING,
            "lesson": _object_schema(
                _LESSON_DRAFT_PROPERTIES,
                _LESSON_DRAFT_REQUIRED,
            ),
        },
        ["expected_invariant", "controllable_cause", "lesson"],
    )


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """An immutable public MCP operation and its JSON Schema contracts."""

    name: str
    description: str
    input_schema: Schema
    output_schema: Schema
    annotations: Schema

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", cast(Schema, _freeze(self.input_schema)))
        object.__setattr__(self, "output_schema", cast(Schema, _freeze(self.output_schema)))
        object.__setattr__(self, "annotations", cast(Schema, _freeze(self.annotations)))

    def as_mcp_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": _thaw(self.input_schema),
            "outputSchema": _thaw(self.output_schema),
            "annotations": _thaw(self.annotations),
        }


def _annotations(*, read_only: bool, idempotent: bool | None = None) -> Schema:
    return {
        "readOnlyHint": read_only,
        "destructiveHint": False,
        "idempotentHint": read_only if idempotent is None else idempotent,
        "openWorldHint": False,
    }


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(nested) for key, nested in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


TOOLS: Final[tuple[ToolDefinition, ...]] = (
    ToolDefinition(
        name="evaluate_failure_candidate",
        description="Qualify a candidate as an accepted, rejected, or deferred failure capture.",
        input_schema=_object_schema(
            _CANDIDATE_PROPERTIES,
            [
                "summary",
                "classification",
                "expectation_source",
                "observed_outcome_at",
                "outcome_mismatch",
                "material_impact_or_recurrence_risk",
                "controllable_with_prior_information",
                "durable_lesson",
            ],
        ),
        output_schema=_tool_output_schema(
            _object_schema(
                {
                    "capture_attempt_id": _DRAFT_STRING,
                    "decision": {
                        "type": "string",
                        "enum": [member.value for member in CaptureDecision],
                    },
                    "reason_codes": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [member.value for member in ReasonCode],
                        },
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "policy_version": _DRAFT_STRING,
                },
                [
                    "capture_attempt_id",
                    "decision",
                    "reason_codes",
                    "confidence",
                    "policy_version",
                ],
            )
        ),
        annotations=_annotations(read_only=False),
    ),
    ToolDefinition(
        name="review_failure_recording",
        description=(
            "Run the required second-tier exact and similarity review before recording "
            "an accepted failure; this never merges lessons automatically."
        ),
        input_schema=_object_schema(
            {
                "capture_attempt_id": _DRAFT_STRING,
                "incident": _object_schema(_INCIDENT_PROPERTIES, _INCIDENT_REQUIRED),
                "lesson": _object_schema(_LESSON_DRAFT_PROPERTIES, _LESSON_DRAFT_REQUIRED),
            },
            ["capture_attempt_id", "incident", "lesson"],
        ),
        output_schema=_tool_output_schema(
            _object_schema(
                {
                    "review_id": _DRAFT_STRING,
                    "recommendation": {
                        "type": "string",
                        "enum": [
                            "reuse_exact",
                            "review_related",
                            "create_distinct",
                        ],
                    },
                    "retrieval_profile": _DRAFT_STRING,
                    "automatic_merge": {"type": "boolean", "const": False},
                    "candidates": {
                        "type": "array",
                        "maxItems": 3,
                        "items": _object_schema(
                            {
                                "lesson_version_id": _DRAFT_STRING,
                                "lesson_id": _DRAFT_STRING,
                                "title": _DRAFT_STRING,
                                "rule": _DRAFT_STRING,
                                "prevention_action": _DRAFT_STRING,
                                "verification_action": _DRAFT_STRING,
                                "applicability": _DRAFT_STRING,
                                "counterexamples": _DRAFT_STRING,
                                "expected_invariant": _DRAFT_STRING,
                                "controllable_cause": _DRAFT_STRING,
                                "channels": {
                                    "type": "array",
                                    "items": _DRAFT_STRING,
                                },
                                "score": {"type": "number"},
                                "exact": {"type": "boolean"},
                                "lifecycle_state": {
                                    "type": "string",
                                    "enum": [member.value for member in LessonState],
                                },
                            },
                            [
                                "lesson_version_id",
                                "lesson_id",
                                "title",
                                "rule",
                                "prevention_action",
                                "verification_action",
                                "applicability",
                                "counterexamples",
                                "expected_invariant",
                                "controllable_cause",
                                "channels",
                                "score",
                                "exact",
                                "lifecycle_state",
                            ],
                        ),
                    },
                },
                [
                    "review_id",
                    "recommendation",
                    "retrieval_profile",
                    "automatic_merge",
                    "candidates",
                ],
            )
        ),
        annotations=_annotations(read_only=False, idempotent=False),
    ),
    ToolDefinition(
        name="record_failure_incident",
        description=(
            "Record an accepted failure only after an explicit generalization review "
            "and disposition."
        ),
        input_schema=_object_schema(
            {
                "capture_attempt_id": _DRAFT_STRING,
                "generalization_review_id": _DRAFT_STRING,
                "disposition": {
                    "type": "string",
                    "enum": [member.value for member in RecordingDisposition],
                },
                "target_lesson_version_id": _DRAFT_STRING,
                "rationale_code": _DRAFT_STRING,
                "incident": _object_schema(_INCIDENT_PROPERTIES, _INCIDENT_REQUIRED),
                "lesson": _object_schema(_LESSON_DRAFT_PROPERTIES, _LESSON_DRAFT_REQUIRED),
            },
            [
                "capture_attempt_id",
                "generalization_review_id",
                "disposition",
                "rationale_code",
                "incident",
                "lesson",
            ],
        ),
        output_schema=_tool_output_schema(_record_output_schema()),
        annotations=_annotations(read_only=False),
    ),
    ToolDefinition(
        name="find_related_failures",
        description="Find a lesson matching the exact invariant, cause, and prevention signature.",
        input_schema=_object_schema(
            {
                "expected_invariant": _DRAFT_STRING,
                "controllable_cause": _DRAFT_STRING,
                "prevention_action": _DRAFT_STRING,
            },
            ["expected_invariant", "controllable_cause", "prevention_action"],
        ),
        output_schema=_tool_output_schema(
            _object_schema(
                {
                    "found": {"type": "boolean"},
                    "lesson": {"oneOf": [{"type": "null"}, _lesson_output_schema()]},
                },
                ["found", "lesson"],
            )
        ),
        annotations=_annotations(read_only=True),
    ),
    ToolDefinition(
        name="recall_failure_lessons",
        description=(
            "Recall up to five lessons from global personal memory using exact, lexical, "
            "semantic, or hybrid retrieval and append a privacy-preserving trace."
        ),
        input_schema=_recall_input_schema(),
        output_schema=_tool_output_schema(
            _object_schema(
                {
                    "attempt_id": _DRAFT_STRING,
                    "requested_mode": {
                        "type": "string",
                        "enum": [member.value for member in RecallMode],
                    },
                    "executed_mode": {
                        "type": "string",
                        "enum": [member.value for member in RecallMode],
                    },
                    "status": {
                        "type": "string",
                        "enum": [member.value for member in RecallStatus],
                    },
                    "retrieval_profile": _DRAFT_STRING,
                    "detail": {"oneOf": [{"type": "null"}, {"type": "string"}]},
                    "candidates": {
                        "type": "array",
                        "items": _recall_candidate_schema(),
                        "maxItems": 5,
                    },
                },
                [
                    "attempt_id",
                    "requested_mode",
                    "executed_mode",
                    "status",
                    "retrieval_profile",
                    "detail",
                    "candidates",
                ],
            )
        ),
        annotations=_annotations(read_only=False, idempotent=False),
    ),
    ToolDefinition(
        name="record_recall_outcome",
        description=(
            "Append structured usefulness, false-positive, recurrence-prevention, "
            "staleness, or contradiction feedback for a prior recall attempt."
        ),
        input_schema=_object_schema(
            {
                "attempt_id": _DRAFT_STRING,
                "outcome": {
                    "type": "string",
                    "enum": [member.value for member in RecallOutcomeKind],
                },
                "lesson_version_id": _DRAFT_STRING,
                "detail_code": _DRAFT_STRING,
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            ["attempt_id", "outcome"],
        ),
        output_schema=_tool_output_schema(
            _object_schema({"outcome_event_id": _DRAFT_STRING}, ["outcome_event_id"])
        ),
        annotations=_annotations(read_only=False, idempotent=False),
    ),
    ToolDefinition(
        name="get_failure_memory_metrics",
        description="Return local append-only failure-memory record counts.",
        input_schema=_object_schema({}, []),
        output_schema=_tool_output_schema(_counts_schema()),
        annotations=_annotations(read_only=True),
    ),
    ToolDefinition(
        name="get_failure_recall_metrics",
        description="Return append-only recall and outcome telemetry record counts.",
        input_schema=_object_schema({}, []),
        output_schema=_tool_output_schema(_recall_counts_schema()),
        annotations=_annotations(read_only=True),
    ),
    ToolDefinition(
        name="failure_memory_retrieval_status",
        description="Return lexical/vector index availability and indexed-document count.",
        input_schema=_object_schema({}, []),
        output_schema=_tool_output_schema(_retrieval_status_schema()),
        annotations=_annotations(read_only=True),
    ),
    ToolDefinition(
        name="build_failure_memory_index",
        description=(
            "Idempotently synchronize the rebuildable retrieval index from accepted global lessons."
        ),
        input_schema=_object_schema({}, []),
        output_schema=_tool_output_schema(
            _object_schema(
                {
                    **cast(Schema, _retrieval_status_schema()["properties"]),
                    "changed_documents": {"type": "integer", "minimum": 0},
                },
                [
                    "state",
                    "profile",
                    "lexical_available",
                    "semantic_available",
                    "indexed_documents",
                    "detail",
                    "changed_documents",
                ],
            )
        ),
        annotations=_annotations(read_only=False, idempotent=True),
    ),
    ToolDefinition(
        name="get_failure_learning_metrics",
        description="Return measured feedback coverage, precision, reuse, and recall quality.",
        input_schema=_object_schema({}, []),
        output_schema=_tool_output_schema(_learning_metrics_schema()),
        annotations=_annotations(read_only=True),
    ),
    ToolDefinition(
        name="failure_memory_store_status",
        description="Return global-store identity and completed copy-only import history.",
        input_schema=_object_schema({}, []),
        output_schema=_tool_output_schema(_store_status_schema()),
        annotations=_annotations(read_only=True),
    ),
    ToolDefinition(
        name="transition_failure_lesson",
        description="Append a reviewed lesson lifecycle version without rewriting history.",
        input_schema=_object_schema(
            {
                "lesson_id": _DRAFT_STRING,
                "to_state": {
                    "type": "string",
                    "enum": ["verified", "deprecated", "superseded"],
                },
                "rationale_code": _DRAFT_STRING,
            },
            ["lesson_id", "to_state", "rationale_code"],
        ),
        output_schema=_tool_output_schema(
            _object_schema(
                {
                    "event_id": _DRAFT_STRING,
                    "lesson_id": _DRAFT_STRING,
                    "prior_version_id": _DRAFT_STRING,
                    "new_version_id": _DRAFT_STRING,
                    "version_number": {"type": "integer", "minimum": 2},
                    "from_state": {
                        "type": "string",
                        "enum": [member.value for member in LessonState],
                    },
                    "to_state": {
                        "type": "string",
                        "enum": ["verified", "deprecated", "superseded"],
                    },
                },
                [
                    "event_id",
                    "lesson_id",
                    "prior_version_id",
                    "new_version_id",
                    "version_number",
                    "from_state",
                    "to_state",
                ],
            )
        ),
        annotations=_annotations(read_only=False, idempotent=False),
    ),
    ToolDefinition(
        name="run_failure_ranking_experiment",
        description="Append a shadow-only feedback-ranking experiment; never activate it.",
        input_schema=_object_schema({}, []),
        output_schema=_tool_output_schema(
            _object_schema(
                {
                    "experiment_id": _DRAFT_STRING,
                    "state": {"type": "string", "const": "shadow"},
                    "attempt_count": {"type": "integer", "minimum": 0},
                    "labeled_selection_count": {"type": "integer", "minimum": 0},
                    "changed_top_rank_count": {"type": "integer", "minimum": 0},
                    "baseline_policy": _DRAFT_STRING,
                    "candidate_policy": _DRAFT_STRING,
                    "metrics": _learning_metrics_schema(),
                },
                [
                    "experiment_id",
                    "state",
                    "attempt_count",
                    "labeled_selection_count",
                    "changed_top_rank_count",
                    "baseline_policy",
                    "candidate_policy",
                    "metrics",
                ],
            )
        ),
        annotations=_annotations(read_only=False, idempotent=False),
    ),
    ToolDefinition(
        name="propose_failure_lesson_clusters",
        description=("Append proposal-only semantic lesson clusters with source IDs and no merge."),
        input_schema=_object_schema(
            {
                "distance_threshold": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 2,
                }
            },
            [],
        ),
        output_schema=_tool_output_schema(
            _object_schema(
                {
                    "run_id": _DRAFT_STRING,
                    "state": {"type": "string", "const": "proposed"},
                    "retrieval_profile": _DRAFT_STRING,
                    "distance_threshold": {"type": "number", "minimum": 0, "maximum": 2},
                    "lesson_count": {"type": "integer", "minimum": 0},
                    "cluster_count": {"type": "integer", "minimum": 0},
                    "clusters": {
                        "type": "array",
                        "items": _object_schema(
                            {
                                "cluster_key": _DRAFT_STRING,
                                "lesson_version_ids": {
                                    "type": "array",
                                    "items": _DRAFT_STRING,
                                    "minItems": 2,
                                },
                            },
                            ["cluster_key", "lesson_version_ids"],
                        ),
                    },
                    "automatic_merge": {"type": "boolean", "const": False},
                },
                [
                    "run_id",
                    "state",
                    "retrieval_profile",
                    "distance_threshold",
                    "lesson_count",
                    "cluster_count",
                    "clusters",
                    "automatic_merge",
                ],
            )
        ),
        annotations=_annotations(read_only=False, idempotent=False),
    ),
    ToolDefinition(
        name="list_failure_generalization_proposals",
        description=("List proposal-only lesson clusters and their latest explicit review state."),
        input_schema=_object_schema({}, []),
        output_schema=_tool_output_schema(
            _object_schema(
                {
                    "scope": {"type": "string", "const": "global_personal"},
                    "proposals": {
                        "type": "array",
                        "items": _generalization_proposal_schema(),
                    },
                },
                ["scope", "proposals"],
            )
        ),
        annotations=_annotations(read_only=True),
    ),
    ToolDefinition(
        name="review_failure_generalization_proposal",
        description=(
            "Append an accept, reject, or defer review for one lesson-cluster "
            "proposal; acceptance never merges or verifies source lessons."
        ),
        input_schema={
            **_object_schema(
                {
                    "proposal_id": _DRAFT_STRING,
                    "decision": {
                        "type": "string",
                        "enum": [member.value for member in GeneralizationProposalDecision],
                    },
                    "rationale_code": _DRAFT_STRING,
                    "generalized_lesson": _generalized_lesson_input_schema(),
                },
                ["proposal_id", "decision", "rationale_code"],
            ),
            "allOf": [
                {
                    "if": {"required": ["generalized_lesson"]},
                    "then": {
                        "properties": {
                            "decision": {"const": "accept"},
                        }
                    },
                }
            ],
        },
        output_schema=_tool_output_schema(
            _object_schema(
                {
                    "review_id": _DRAFT_STRING,
                    "proposal_id": _DRAFT_STRING,
                    "prior_review_id": {
                        "oneOf": [{"type": "null"}, _DRAFT_STRING],
                    },
                    "decision": {
                        "type": "string",
                        "enum": [member.value for member in GeneralizationProposalDecision],
                    },
                    "rationale_code": _DRAFT_STRING,
                    "supporting_lesson_version_ids": {
                        "type": "array",
                        "items": _DRAFT_STRING,
                        "minItems": 2,
                        "uniqueItems": True,
                    },
                    "counterexample_lesson_version_ids": {
                        "type": "array",
                        "items": _DRAFT_STRING,
                        "uniqueItems": True,
                    },
                    "resulting_lesson_version_id": {
                        "oneOf": [{"type": "null"}, _DRAFT_STRING],
                    },
                    "automatic_merge": {"type": "boolean", "const": False},
                    "production_activated": {"type": "boolean", "const": False},
                },
                [
                    "review_id",
                    "proposal_id",
                    "prior_review_id",
                    "decision",
                    "rationale_code",
                    "supporting_lesson_version_ids",
                    "counterexample_lesson_version_ids",
                    "resulting_lesson_version_id",
                    "automatic_merge",
                    "production_activated",
                ],
            )
        ),
        annotations=_annotations(read_only=False, idempotent=False),
    ),
    ToolDefinition(
        name="failure_memory_setup_status",
        description="Return the capabilities available in the installed local profile.",
        input_schema=_object_schema({}, []),
        output_schema=_tool_output_schema(
            _object_schema(
                _status_properties(),
                [
                    "scope",
                    "state",
                    "profile",
                    "available_capabilities",
                    "unavailable_capabilities",
                ],
            )
        ),
        annotations=_annotations(read_only=True),
    ),
    ToolDefinition(
        name="failure_memory_doctor",
        description="Return local database health, setup status, and record counts.",
        input_schema=_object_schema({}, []),
        output_schema=_tool_output_schema(
            _object_schema(
                {
                    **_status_properties(),
                    "integrity_check": _DRAFT_STRING,
                    "counts": _counts_schema(),
                    "recall_counts": _recall_counts_schema(),
                    "learning_metrics": _learning_metrics_schema(),
                    "store": _store_status_schema(),
                    "retrieval": {
                        "oneOf": [
                            _retrieval_status_schema(),
                            _object_schema({"state": _DRAFT_STRING}, ["state"]),
                        ]
                    },
                },
                [
                    "state",
                    "scope",
                    "profile",
                    "available_capabilities",
                    "unavailable_capabilities",
                    "integrity_check",
                    "counts",
                    "recall_counts",
                    "learning_metrics",
                    "store",
                    "retrieval",
                ],
            )
        ),
        annotations=_annotations(read_only=True),
    ),
)
