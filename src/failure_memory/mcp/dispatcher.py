from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime

from failure_memory.application.errors import (
    SEMANTIC_SETUP_MESSAGE,
    STORAGE_BUSY_MESSAGE,
    SemanticSetupRequiredError,
    StorageBusyError,
)
from failure_memory.application.service import FailureMemoryService
from failure_memory.domain.capture import Classification, ExpectationSource, FailureCandidate
from failure_memory.domain.records import (
    IncidentDraft,
    LessonDraft,
    LessonState,
    LessonVersionRecord,
)
from failure_memory.domain.retrieval import (
    RecallCandidate,
    RecallMode,
    RecallOutcome,
    RecallOutcomeKind,
    RecallQuery,
)
from failure_memory.mcp.rfc3339 import parse_rfc3339_date_time

_CANDIDATE_KEYS = {
    "summary",
    "classification",
    "expectation_source",
    "expectation_established_at",
    "observed_outcome_at",
    "outcome_mismatch",
    "material_impact_or_recurrence_risk",
    "controllable_with_prior_information",
    "durable_lesson",
    "failure_portion_summary",
}
_CANDIDATE_REQUIRED = _CANDIDATE_KEYS - {"expectation_established_at", "failure_portion_summary"}
_INCIDENT_KEYS = {
    "outcome_summary",
    "expected_invariant",
    "controllable_cause",
    "material_impact",
    "recurrence_risk",
}
_LESSON_KEYS = {
    "title",
    "rule",
    "prevention_action",
    "verification_action",
    "applicability",
    "counterexamples",
}
_LOOKUP_KEYS = {"expected_invariant", "controllable_cause", "prevention_action"}
_RECALL_KEYS = {
    "mode",
    "text",
    "expected_invariant",
    "controllable_cause",
    "prevention_action",
    "component",
    "top_k",
}
_OUTCOME_KEYS = {
    "attempt_id",
    "outcome",
    "lesson_version_id",
    "detail_code",
    "confidence",
}
_TRANSITION_KEYS = {"lesson_id", "to_state", "rationale_code"}
_CLUSTER_KEYS = {"distance_threshold"}
_LOGGER = logging.getLogger(__name__)
_REJECTED_MESSAGE = "The failure-memory service rejected the operation."
_INTERNAL_ERROR_MESSAGE = "Internal failure-memory error."


class BoundaryValidationError(ValueError):
    """A safe-to-expose violation of the public tool argument contract."""


def dispatch_tool(
    name: str,
    arguments: Mapping[str, object],
    service: FailureMemoryService,
    *,
    log_exceptions: bool = True,
) -> dict[str, object]:
    """Validate one public operation, invoke the service, and serialize a MCP tool result."""
    if name not in {
        "evaluate_failure_candidate",
        "record_failure_incident",
        "find_related_failures",
        "recall_failure_lessons",
        "record_recall_outcome",
        "get_failure_memory_metrics",
        "get_failure_recall_metrics",
        "get_failure_learning_metrics",
        "failure_memory_retrieval_status",
        "build_failure_memory_index",
        "failure_memory_store_status",
        "transition_failure_lesson",
        "run_failure_ranking_experiment",
        "propose_failure_lesson_clusters",
        "failure_memory_setup_status",
        "failure_memory_doctor",
    }:
        return _error(f"Unknown tool: {name}", "unknown_tool")
    try:
        if not isinstance(arguments, Mapping):
            raise BoundaryValidationError("arguments must be an object")
        if name == "evaluate_failure_candidate":
            return _evaluate(arguments, service)
        if name == "record_failure_incident":
            return _record(arguments, service)
        if name == "find_related_failures":
            return _find_related(arguments, service)
        if name == "recall_failure_lessons":
            return _recall(arguments, service)
        if name == "record_recall_outcome":
            return _record_outcome(arguments, service)
        if name == "transition_failure_lesson":
            return _transition_lesson(arguments, service)
        if name == "propose_failure_lesson_clusters":
            return _propose_clusters(arguments, service)
        _validate_object(arguments, set(), set(), "arguments")
        if name == "get_failure_memory_metrics":
            return _success(
                dict[str, object](service.metrics()), "Failure-memory metrics returned."
            )
        if name == "get_failure_recall_metrics":
            return _success(
                dict[str, object](service.recall_metrics()),
                "Failure-memory recall metrics returned.",
            )
        if name == "get_failure_learning_metrics":
            return _success(
                dict(service.learning_metrics()),
                "Failure-memory learning metrics returned.",
            )
        if name == "failure_memory_retrieval_status":
            return _success(
                dict(service.retrieval_status()),
                "Failure-memory retrieval status returned.",
            )
        if name == "build_failure_memory_index":
            return _success(
                dict(service.build_index()),
                "Failure-memory retrieval index synchronized.",
            )
        if name == "failure_memory_store_status":
            return _success(
                dict(service.store_status()),
                "Global failure-memory store status returned.",
            )
        if name == "run_failure_ranking_experiment":
            return _success(
                dict(service.run_shadow_ranking_experiment()),
                "Shadow ranking experiment recorded.",
            )
        if name == "failure_memory_setup_status":
            return _success(dict(service.setup_status()), "Failure-memory setup status returned.")
        return _success(dict(service.doctor()), "Failure-memory doctor report returned.")
    except BoundaryValidationError as exc:
        return _error(str(exc), "invalid_arguments")
    except StorageBusyError:
        return _error(STORAGE_BUSY_MESSAGE, "busy")
    except SemanticSetupRequiredError:
        return _error(SEMANTIC_SETUP_MESSAGE, "setup_required")
    except ValueError:
        return _error(_REJECTED_MESSAGE, "operation_rejected")
    except Exception:
        if log_exceptions:
            _LOGGER.exception("Unexpected failure-memory MCP tool failure for %s", name)
        return _error(_INTERNAL_ERROR_MESSAGE, "internal_error")


def _evaluate(arguments: Mapping[str, object], service: FailureMemoryService) -> dict[str, object]:
    _validate_object(arguments, _CANDIDATE_REQUIRED, _CANDIDATE_KEYS, "arguments")
    expectation_established_at = _optional_datetime(arguments, "expectation_established_at")
    candidate = FailureCandidate(
        summary=_require_string(arguments, "summary"),
        classification=_parse_classification(_require_string(arguments, "classification")),
        expectation_source=_parse_expectation_source(
            _require_string(arguments, "expectation_source")
        ),
        expectation_established_at=expectation_established_at,
        observed_outcome_at=_parse_datetime(
            _require_string(arguments, "observed_outcome_at"), "observed_outcome_at"
        ),
        outcome_mismatch=_require_bool(arguments, "outcome_mismatch"),
        material_impact_or_recurrence_risk=_require_bool(
            arguments, "material_impact_or_recurrence_risk"
        ),
        controllable_with_prior_information=_require_bool(
            arguments, "controllable_with_prior_information"
        ),
        durable_lesson=_require_bool(arguments, "durable_lesson"),
        failure_portion_summary=_optional_string(arguments, "failure_portion_summary"),
    )
    result = service.evaluate_failure_candidate(candidate)
    assessment = result.assessment
    payload: dict[str, object] = {
        "capture_attempt_id": result.capture_attempt_id,
        "decision": assessment.decision.value,
        "reason_codes": [code.value for code in assessment.reason_codes],
        "confidence": assessment.confidence,
        "policy_version": assessment.policy_version,
    }
    return _success(payload, f"Failure candidate {assessment.decision.value}.")


def _record(arguments: Mapping[str, object], service: FailureMemoryService) -> dict[str, object]:
    record_keys = {"capture_attempt_id", "incident", "lesson"}
    _validate_object(arguments, record_keys, record_keys, "arguments")
    incident_arguments = _require_object(arguments, "incident")
    lesson_arguments = _require_object(arguments, "lesson")
    _validate_object(incident_arguments, _INCIDENT_KEYS, _INCIDENT_KEYS, "incident")
    _validate_object(lesson_arguments, _LESSON_KEYS, _LESSON_KEYS, "lesson")
    incident = IncidentDraft(
        outcome_summary=_require_string(incident_arguments, "outcome_summary"),
        expected_invariant=_require_string(incident_arguments, "expected_invariant"),
        controllable_cause=_require_string(incident_arguments, "controllable_cause"),
        material_impact=_require_string(incident_arguments, "material_impact"),
        recurrence_risk=_require_string(incident_arguments, "recurrence_risk"),
    )
    lesson = LessonDraft(
        title=_require_string(lesson_arguments, "title"),
        rule=_require_string(lesson_arguments, "rule"),
        prevention_action=_require_string(lesson_arguments, "prevention_action"),
        verification_action=_require_string(lesson_arguments, "verification_action"),
        applicability=_require_string(lesson_arguments, "applicability"),
        counterexamples=_require_string(lesson_arguments, "counterexamples"),
    )
    result = service.record_failure_incident(
        _require_string(arguments, "capture_attempt_id"), incident, lesson
    )
    return _success(
        {
            "incident_id": result.incident_id,
            "lesson_id": result.lesson_id,
            "lesson_version_id": result.lesson_version_id,
            "relation": result.relation.value,
            "created_new_lesson": result.created_new_lesson,
        },
        "Failure incident and lesson recorded.",
    )


def _find_related(
    arguments: Mapping[str, object], service: FailureMemoryService
) -> dict[str, object]:
    _validate_object(arguments, _LOOKUP_KEYS, _LOOKUP_KEYS, "arguments")
    lesson = service.find_related_failures(
        _require_string(arguments, "expected_invariant"),
        _require_string(arguments, "controllable_cause"),
        _require_string(arguments, "prevention_action"),
    )
    if lesson is None:
        return _success({"found": False, "lesson": None}, "No exactly related failure was found.")
    return _success({"found": True, "lesson": _lesson_payload(lesson)}, "Related failure found.")


def _recall(
    arguments: Mapping[str, object],
    service: FailureMemoryService,
) -> dict[str, object]:
    _validate_object(arguments, set(), _RECALL_KEYS, "arguments")
    mode_value = _optional_string(arguments, "mode")
    try:
        mode = RecallMode.AUTO if mode_value is None else RecallMode(mode_value)
    except ValueError as exc:
        raise BoundaryValidationError(f"mode has invalid value: {mode_value}") from exc
    result = service.recall_failure_lessons(
        RecallQuery(
            mode=mode,
            text=_optional_string(arguments, "text"),
            expected_invariant=_optional_string(arguments, "expected_invariant"),
            controllable_cause=_optional_string(arguments, "controllable_cause"),
            prevention_action=_optional_string(arguments, "prevention_action"),
            component=_optional_string(arguments, "component"),
            top_k=_optional_int(arguments, "top_k", default=3, minimum=1, maximum=5),
        )
    )
    payload: dict[str, object] = {
        "attempt_id": result.attempt_id,
        "requested_mode": result.requested_mode.value,
        "executed_mode": result.executed_mode.value,
        "status": result.status.value,
        "retrieval_profile": result.retrieval_profile,
        "detail": result.detail,
        "candidates": [_recall_candidate_payload(candidate) for candidate in result.candidates],
    }
    return _success(payload, f"Failure lessons recall completed with status {result.status.value}.")


def _record_outcome(
    arguments: Mapping[str, object],
    service: FailureMemoryService,
) -> dict[str, object]:
    _validate_object(arguments, {"attempt_id", "outcome"}, _OUTCOME_KEYS, "arguments")
    outcome_value = _require_string(arguments, "outcome")
    try:
        outcome_kind = RecallOutcomeKind(outcome_value)
    except ValueError as exc:
        raise BoundaryValidationError(f"outcome has invalid value: {outcome_value}") from exc
    confidence = _optional_number(arguments, "confidence", minimum=0, maximum=1)
    outcome_id = service.record_recall_outcome(
        RecallOutcome(
            attempt_id=_require_string(arguments, "attempt_id"),
            outcome=outcome_kind,
            lesson_version_id=_optional_string(arguments, "lesson_version_id"),
            detail_code=_optional_string(arguments, "detail_code"),
            confidence=confidence,
        )
    )
    return _success(
        {"outcome_event_id": outcome_id},
        "Recall outcome recorded.",
    )


def _transition_lesson(
    arguments: Mapping[str, object],
    service: FailureMemoryService,
) -> dict[str, object]:
    _validate_object(arguments, _TRANSITION_KEYS, _TRANSITION_KEYS, "arguments")
    state_value = _require_string(arguments, "to_state")
    try:
        state = LessonState(state_value)
    except ValueError as exc:
        raise BoundaryValidationError(f"to_state has invalid value: {state_value}") from exc
    if state not in {
        LessonState.VERIFIED,
        LessonState.DEPRECATED,
        LessonState.SUPERSEDED,
    }:
        raise BoundaryValidationError(f"to_state has invalid value: {state_value}")
    return _success(
        dict(
            service.transition_lesson(
                _require_string(arguments, "lesson_id"),
                state,
                _require_string(arguments, "rationale_code"),
            )
        ),
        "Lesson lifecycle transition recorded.",
    )


def _propose_clusters(
    arguments: Mapping[str, object],
    service: FailureMemoryService,
) -> dict[str, object]:
    _validate_object(arguments, set(), _CLUSTER_KEYS, "arguments")
    threshold = _optional_number(
        arguments,
        "distance_threshold",
        minimum=0,
        maximum=2,
    )
    return _success(
        dict(
            service.propose_lesson_clusters(
                distance_threshold=0.2 if threshold is None else threshold
            )
        ),
        "Proposal-only lesson clustering completed.",
    )


def _lesson_payload(lesson: LessonVersionRecord) -> dict[str, object]:
    return {
        "id": lesson.id,
        "lesson_id": lesson.lesson_id,
        "version_number": lesson.version_number,
        "created_at": lesson.created_at.isoformat(),
        "state": lesson.state.value,
        "signature": lesson.signature,
        "draft": {
            "title": lesson.draft.title,
            "rule": lesson.draft.rule,
            "prevention_action": lesson.draft.prevention_action,
            "verification_action": lesson.draft.verification_action,
            "applicability": lesson.draft.applicability,
            "counterexamples": lesson.draft.counterexamples,
        },
    }


def _recall_candidate_payload(candidate: RecallCandidate) -> dict[str, object]:
    return {
        "lesson": _lesson_payload(candidate.lesson),
        "evidence": {
            "expected_invariant": candidate.expected_invariant,
            "controllable_cause": candidate.controllable_cause,
            "outcome_summary": candidate.outcome_summary,
        },
        "channels": list(candidate.channels),
        "score": candidate.score,
        "exact": candidate.exact,
        "lexical_rank": candidate.lexical_rank,
        "semantic_rank": candidate.semantic_rank,
        "vector_distance": candidate.vector_distance,
    }


def _validate_object(
    arguments: Mapping[str, object], required: set[str], allowed: set[str], label: str
) -> None:
    missing = required - set(arguments)
    if missing:
        raise BoundaryValidationError(f"{next(iter(missing))} is required")
    extra = set(arguments) - allowed
    if extra:
        raise BoundaryValidationError(f"{label}.{next(iter(extra))} is not allowed")


def _require_object(arguments: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = arguments.get(key)
    if not isinstance(value, Mapping):
        raise BoundaryValidationError(f"{key} must be an object")
    return value


def _require_string(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BoundaryValidationError(f"{key} must be a non-empty string")
    return value


def _optional_string(arguments: Mapping[str, object], key: str) -> str | None:
    if key not in arguments:
        return None
    return _require_string(arguments, key)


def _require_bool(arguments: Mapping[str, object], key: str) -> bool:
    value = arguments.get(key)
    if type(value) is not bool:
        raise BoundaryValidationError(f"{key} must be a boolean")
    return value


def _optional_int(
    arguments: Mapping[str, object],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if key not in arguments:
        return default
    value = arguments.get(key)
    if type(value) is not int or not minimum <= value <= maximum:
        raise BoundaryValidationError(f"{key} must be an integer between {minimum} and {maximum}")
    return value


def _optional_number(
    arguments: Mapping[str, object],
    key: str,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    if key not in arguments:
        return None
    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BoundaryValidationError(f"{key} must be a number")
    rendered = float(value)
    if not minimum <= rendered <= maximum:
        raise BoundaryValidationError(f"{key} must be between {minimum} and {maximum}")
    return rendered


def _optional_datetime(arguments: Mapping[str, object], key: str) -> datetime | None:
    if key not in arguments:
        return None
    return _parse_datetime(_require_string(arguments, key), key)


def _parse_datetime(value: str, key: str) -> datetime:
    try:
        parsed = parse_rfc3339_date_time(value)
    except ValueError as exc:
        raise BoundaryValidationError(f"{key} must be an RFC3339 date-time") from exc
    return parsed


def _parse_classification(value: str) -> Classification:
    try:
        return Classification(value)
    except ValueError as exc:
        raise BoundaryValidationError(f"classification has invalid value: {value}") from exc


def _parse_expectation_source(value: str) -> ExpectationSource:
    try:
        return ExpectationSource(value)
    except ValueError as exc:
        raise BoundaryValidationError(f"expectation_source has invalid value: {value}") from exc


def _success(payload: dict[str, object], summary: str) -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": summary}],
        "structuredContent": payload,
        "isError": False,
    }


def _error(message: str, code: str) -> dict[str, object]:
    payload: dict[str, object] = {"error": {"code": code, "message": message}}
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": payload,
        "isError": True,
    }
