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
from failure_memory.domain.causal import (
    CausalAssessmentDraft,
    CausalAssessmentRecord,
    CausalAssessmentState,
    CausalConfidence,
    CausalFactorDraft,
    CausalFactorRole,
    CauseLayer,
    FailureMode,
    RepairOutcome,
    RepairOutcomeKind,
    RepairRecommendationDraft,
)
from failure_memory.domain.fast_capture import (
    CauseEvidence,
    ExpectationEvidence,
    LessonEvidence,
    ObservedEvidence,
    RememberFailureDraft,
)
from failure_memory.domain.ids import new_id
from failure_memory.domain.learning import (
    GeneralizationProposalDecision,
    GeneralizedLessonDraft,
)
from failure_memory.domain.records import (
    IncidentDraft,
    LessonDraft,
    LessonState,
    LessonVersionRecord,
    RecordingDisposition,
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
    "cause_layer",
    "failure_mode",
    "repair_target_layer",
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
_PROPOSAL_REVIEW_KEYS = {
    "proposal_id",
    "decision",
    "rationale_code",
    "generalized_lesson",
}
_LOGGER = logging.getLogger(__name__)
_REJECTED_MESSAGE = "The failure-memory service rejected the operation."
_INTERNAL_ERROR_MESSAGE = "Internal failure-memory error."


class BoundaryValidationError(ValueError):
    """A safe-to-expose violation of the public tool argument contract."""

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        code: str = "invalid_arguments",
        expected: str | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.code = code
        self.expected = expected


def dispatch_tool(
    name: str,
    arguments: Mapping[str, object],
    service: FailureMemoryService,
    *,
    log_exceptions: bool = True,
    transport: str = "mcp",
) -> dict[str, object]:
    """Validate one public operation, invoke the service, and serialize a MCP tool result."""
    if name not in {
        "remember_failure",
        "evaluate_failure_candidate",
        "diagnose_failure_cause",
        "record_failure_incident",
        "record_failure_repair_outcome",
        "review_failure_recording",
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
        "list_failure_generalization_proposals",
        "review_failure_generalization_proposal",
        "failure_memory_setup_status",
        "failure_memory_doctor",
    }:
        return _error(f"Unknown tool: {name}", "unknown_tool")
    try:
        if not isinstance(arguments, Mapping):
            raise BoundaryValidationError("arguments must be an object")
        if name == "remember_failure":
            return _remember(arguments, service, transport=transport)
        if name == "evaluate_failure_candidate":
            return _evaluate(arguments, service)
        if name == "diagnose_failure_cause":
            return _diagnose_cause(arguments, service)
        if name == "record_failure_incident":
            return _record(arguments, service)
        if name == "record_failure_repair_outcome":
            return _record_repair_outcome(arguments, service)
        if name == "review_failure_recording":
            return _review_recording(arguments, service)
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
        if name == "review_failure_generalization_proposal":
            return _review_generalization_proposal(arguments, service)
        _validate_object(arguments, set(), set(), "arguments")
        if name == "list_failure_generalization_proposals":
            return _success(
                {
                    "scope": "global_personal",
                    "proposals": list(service.list_lesson_generalization_proposals()),
                },
                "Failure generalization proposals returned.",
            )
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
        return _error(
            str(exc),
            exc.code,
            field=exc.field,
            expected=exc.expected,
        )
    except StorageBusyError:
        return _error(STORAGE_BUSY_MESSAGE, "busy")
    except SemanticSetupRequiredError:
        return _error(SEMANTIC_SETUP_MESSAGE, "setup_required")
    except ValueError:
        return _error(_REJECTED_MESSAGE, "operation_rejected")
    except Exception:
        trace_id = new_id("err")
        if log_exceptions:
            _LOGGER.exception(
                "Unexpected failure-memory MCP tool failure for %s (trace %s)",
                name,
                trace_id,
            )
        return _error(_INTERNAL_ERROR_MESSAGE, "internal_error", trace_id=trace_id)


def _remember(
    arguments: Mapping[str, object],
    service: FailureMemoryService,
    *,
    transport: str,
) -> dict[str, object]:
    allowed = {
        "summary",
        "classification",
        "failure_portion",
        "expectation",
        "observed",
        "cause",
        "lesson",
    }
    _validate_object(arguments, {"summary", "classification"}, allowed, "arguments")
    classification = _parse_classification(_require_string(arguments, "classification"))
    expectation: ExpectationEvidence | None = None
    observed: ObservedEvidence | None = None
    cause: CauseEvidence | None = None
    lesson: LessonEvidence | None = None
    if classification in {Classification.REAL_FAILURE, Classification.MIXED}:
        missing = next(
            (
                field
                for field in ("expectation", "observed", "cause", "lesson")
                if field not in arguments
            ),
            None,
        )
        if missing is not None:
            raise BoundaryValidationError(
                f"{missing} is required for {classification.value}",
                field=missing,
                code="missing_failure_evidence",
                expected=f"{missing} object",
            )
        if classification is Classification.MIXED and "failure_portion" not in arguments:
            raise BoundaryValidationError(
                "failure_portion is required for mixed feedback",
                field="failure_portion",
                code="mixed_failure_not_separated",
                expected="only the prior-invariant mismatch",
            )
        expectation_arguments = _require_object(arguments, "expectation")
        expectation_keys = {"invariant", "source", "evidence"}
        _validate_object(
            expectation_arguments,
            expectation_keys,
            expectation_keys,
            "expectation",
        )
        expectation = ExpectationEvidence(
            invariant=_require_string(expectation_arguments, "invariant"),
            source=_parse_expectation_source(
                _require_string(expectation_arguments, "source")
            ),
            evidence=_require_string(expectation_arguments, "evidence"),
        )
        observed_arguments = _require_object(arguments, "observed")
        _validate_object(
            observed_arguments,
            {"outcome", "impact"},
            {"outcome", "impact", "recurrence_risk"},
            "observed",
        )
        observed = ObservedEvidence(
            outcome=_require_string(observed_arguments, "outcome"),
            impact=_require_string(observed_arguments, "impact"),
            recurrence_risk=_optional_string(observed_arguments, "recurrence_risk"),
        )
        cause_arguments = _require_object(arguments, "cause")
        _validate_object(
            cause_arguments,
            {
                "layer",
                "failure_mode",
                "component",
                "evidence",
                "recommended_change",
                "verification",
            },
            {
                "layer",
                "failure_mode",
                "component",
                "evidence",
                "recommended_change",
                "verification",
                "confidence",
            },
            "cause",
        )
        confidence_value = _optional_string(cause_arguments, "confidence")
        cause = CauseEvidence(
            layer=_parse_enum(
                CauseLayer,
                _require_string(cause_arguments, "layer"),
                "cause.layer",
            ),
            failure_mode=_parse_enum(
                FailureMode,
                _require_string(cause_arguments, "failure_mode"),
                "cause.failure_mode",
            ),
            component=_require_string(cause_arguments, "component"),
            evidence=_require_string(cause_arguments, "evidence"),
            recommended_change=_require_string(cause_arguments, "recommended_change"),
            verification=_require_string(cause_arguments, "verification"),
            confidence=(
                CausalConfidence.MEDIUM
                if confidence_value is None
                else _parse_enum(
                    CausalConfidence,
                    confidence_value,
                    "cause.confidence",
                )
            ),
        )
        lesson_arguments = _require_object(arguments, "lesson")
        _validate_object(
            lesson_arguments,
            {"rule", "prevention", "verification"},
            {
                "rule",
                "prevention",
                "verification",
                "title",
                "applicability",
                "counterexamples",
            },
            "lesson",
        )
        lesson = LessonEvidence(
            rule=_require_string(lesson_arguments, "rule"),
            prevention=_require_string(lesson_arguments, "prevention"),
            verification=_require_string(lesson_arguments, "verification"),
            title=_optional_string(lesson_arguments, "title"),
            applicability=_optional_string(lesson_arguments, "applicability"),
            counterexamples=_optional_string(lesson_arguments, "counterexamples"),
        )
    result = service.remember_failure(
        RememberFailureDraft(
            summary=_require_string(arguments, "summary"),
            classification=classification,
            expectation=expectation,
            observed=observed,
            cause=cause,
            lesson=lesson,
            failure_portion=_optional_string(arguments, "failure_portion"),
        ),
        transport=transport,
    )
    payload: dict[str, object] = {
        "operation_id": result.operation_id,
        "status": result.status.value,
        "capture_attempt_id": result.capture_attempt_id,
        "decision": result.decision.value,
        "reason_codes": [reason.value for reason in result.reason_codes],
        "deduplication_status": result.deduplication_status.value,
        "semantic_status": result.semantic_status,
        "total_latency_ms": result.total_latency_ms,
        "incident_id": result.incident_id,
        "lesson_id": result.lesson_id,
        "lesson_version_id": result.lesson_version_id,
        "relation": None if result.relation is None else result.relation.value,
        "causal_assessment_id": result.causal_assessment_id,
        "repair_recommendation_id": result.repair_recommendation_id,
        "generalization_review_id": result.generalization_review_id,
    }
    return _success(payload, f"Failure-memory result: {result.status.value}.")


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


def _diagnose_cause(
    arguments: Mapping[str, object],
    service: FailureMemoryService,
) -> dict[str, object]:
    allowed = {
        "capture_attempt_id",
        "state",
        "unknown_reason",
        "factors",
        "recommendations",
    }
    required = allowed - {"unknown_reason"}
    _validate_object(arguments, required, allowed, "arguments")
    factor_values = _require_object_array(arguments, "factors", minimum=1, maximum=4)
    recommendation_values = _require_object_array(
        arguments,
        "recommendations",
        minimum=1,
        maximum=3,
    )
    factors: list[CausalFactorDraft] = []
    factor_keys = {
        "role",
        "layer",
        "failure_mode",
        "component_reference",
        "evidence_summary",
        "confidence",
    }
    for index, factor in enumerate(factor_values):
        _validate_object(factor, factor_keys, factor_keys, f"factors[{index}]")
        factors.append(
            CausalFactorDraft(
                role=_parse_enum(
                    CausalFactorRole,
                    _require_string(factor, "role"),
                    f"factors[{index}].role",
                ),
                layer=_parse_enum(
                    CauseLayer,
                    _require_string(factor, "layer"),
                    f"factors[{index}].layer",
                ),
                failure_mode=_parse_enum(
                    FailureMode,
                    _require_string(factor, "failure_mode"),
                    f"factors[{index}].failure_mode",
                ),
                component_reference=_require_string(factor, "component_reference"),
                evidence_summary=_require_string(factor, "evidence_summary"),
                confidence=_parse_enum(
                    CausalConfidence,
                    _require_string(factor, "confidence"),
                    f"factors[{index}].confidence",
                ),
            )
        )
    recommendations: list[RepairRecommendationDraft] = []
    recommendation_keys = {
        "target_layer",
        "target_reference",
        "recommended_change",
        "verification_action",
        "rationale",
        "confidence",
    }
    for index, recommendation in enumerate(recommendation_values):
        _validate_object(
            recommendation,
            recommendation_keys,
            recommendation_keys,
            f"recommendations[{index}]",
        )
        recommendations.append(
            RepairRecommendationDraft(
                target_layer=_parse_enum(
                    CauseLayer,
                    _require_string(recommendation, "target_layer"),
                    f"recommendations[{index}].target_layer",
                ),
                target_reference=_require_string(recommendation, "target_reference"),
                recommended_change=_require_string(recommendation, "recommended_change"),
                verification_action=_require_string(recommendation, "verification_action"),
                rationale=_require_string(recommendation, "rationale"),
                confidence=_parse_enum(
                    CausalConfidence,
                    _require_string(recommendation, "confidence"),
                    f"recommendations[{index}].confidence",
                ),
            )
        )
    assessment = service.diagnose_failure_cause(
        _require_string(arguments, "capture_attempt_id"),
        CausalAssessmentDraft(
            state=_parse_enum(
                CausalAssessmentState,
                _require_string(arguments, "state"),
                "state",
            ),
            factors=tuple(factors),
            recommendations=tuple(recommendations),
            unknown_reason=_optional_string(arguments, "unknown_reason"),
        ),
    )
    return _success(
        _causal_assessment_payload(assessment),
        "Failure root cause and repair recommendations recorded.",
    )


def _record_repair_outcome(
    arguments: Mapping[str, object],
    service: FailureMemoryService,
) -> dict[str, object]:
    keys = {
        "recommendation_id",
        "outcome",
        "detail_code",
        "evidence_summary",
        "confidence",
    }
    _validate_object(arguments, keys, keys, "arguments")
    outcome_id = service.record_failure_repair_outcome(
        RepairOutcome(
            recommendation_id=_require_string(arguments, "recommendation_id"),
            outcome=_parse_enum(
                RepairOutcomeKind,
                _require_string(arguments, "outcome"),
                "outcome",
            ),
            detail_code=_require_string(arguments, "detail_code"),
            evidence_summary=_require_string(arguments, "evidence_summary"),
            confidence=_parse_enum(
                CausalConfidence,
                _require_string(arguments, "confidence"),
                "confidence",
            ),
        )
    )
    return _success(
        {"repair_outcome_event_id": outcome_id},
        "Failure repair outcome recorded.",
    )


def _record(arguments: Mapping[str, object], service: FailureMemoryService) -> dict[str, object]:
    record_keys = {
        "capture_attempt_id",
        "causal_assessment_id",
        "generalization_review_id",
        "disposition",
        "target_lesson_version_id",
        "rationale_code",
        "incident",
        "lesson",
    }
    required = record_keys - {"target_lesson_version_id"}
    _validate_object(arguments, required, record_keys, "arguments")
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
    disposition_value = _require_string(arguments, "disposition")
    try:
        disposition = RecordingDisposition(disposition_value)
    except ValueError as exc:
        raise BoundaryValidationError(
            f"disposition has invalid value: {disposition_value}"
        ) from exc
    result = service.record_failure_incident(
        _require_string(arguments, "capture_attempt_id"),
        incident,
        lesson,
        generalization_review_id=_require_string(arguments, "generalization_review_id"),
        disposition=disposition,
        target_lesson_version_id=_optional_string(arguments, "target_lesson_version_id"),
        rationale_code=_require_string(arguments, "rationale_code"),
        causal_assessment_id=_require_string(arguments, "causal_assessment_id"),
    )
    return _success(
        {
            "incident_id": result.incident_id,
            "lesson_id": result.lesson_id,
            "lesson_version_id": result.lesson_version_id,
            "relation": result.relation.value,
            "created_new_lesson": result.created_new_lesson,
            "generalization_decision_id": result.generalization_decision_id,
        },
        "Failure incident and lesson recorded.",
    )


def _review_recording(
    arguments: Mapping[str, object], service: FailureMemoryService
) -> dict[str, object]:
    review_keys = {"capture_attempt_id", "causal_assessment_id", "incident", "lesson"}
    _validate_object(arguments, review_keys, review_keys, "arguments")
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
    return _success(
        dict(
            service.review_failure_recording(
                _require_string(arguments, "capture_attempt_id"),
                incident,
                lesson,
                causal_assessment_id=_require_string(arguments, "causal_assessment_id"),
            )
        ),
        "Failure recording generalization review completed.",
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
            cause_layer=_optional_cause_layer(arguments, "cause_layer"),
            failure_mode=_optional_failure_mode(arguments, "failure_mode"),
            repair_target_layer=_optional_cause_layer(arguments, "repair_target_layer"),
            top_k=_optional_int(arguments, "top_k", default=3, minimum=1, maximum=3),
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


def _review_generalization_proposal(
    arguments: Mapping[str, object],
    service: FailureMemoryService,
) -> dict[str, object]:
    _validate_object(
        arguments,
        {"proposal_id", "decision", "rationale_code"},
        _PROPOSAL_REVIEW_KEYS,
        "arguments",
    )
    decision_value = _require_string(arguments, "decision")
    try:
        decision = GeneralizationProposalDecision(decision_value)
    except ValueError as exc:
        raise BoundaryValidationError(f"decision has invalid value: {decision_value}") from exc
    generalized_lesson: GeneralizedLessonDraft | None = None
    if "generalized_lesson" in arguments:
        if decision is not GeneralizationProposalDecision.ACCEPT:
            raise BoundaryValidationError(
                "generalized_lesson is allowed only for an accepted proposal"
            )
        generalized_arguments = _require_object(arguments, "generalized_lesson")
        generalized_keys = {
            "expected_invariant",
            "controllable_cause",
            "lesson",
        }
        _validate_object(
            generalized_arguments,
            generalized_keys,
            generalized_keys,
            "generalized_lesson",
        )
        lesson_arguments = _require_object(generalized_arguments, "lesson")
        _validate_object(
            lesson_arguments,
            _LESSON_KEYS,
            _LESSON_KEYS,
            "generalized_lesson.lesson",
        )
        generalized_lesson = GeneralizedLessonDraft(
            expected_invariant=_require_string(
                generalized_arguments,
                "expected_invariant",
            ),
            controllable_cause=_require_string(
                generalized_arguments,
                "controllable_cause",
            ),
            lesson=LessonDraft(
                title=_require_string(lesson_arguments, "title"),
                rule=_require_string(lesson_arguments, "rule"),
                prevention_action=_require_string(
                    lesson_arguments,
                    "prevention_action",
                ),
                verification_action=_require_string(
                    lesson_arguments,
                    "verification_action",
                ),
                applicability=_require_string(
                    lesson_arguments,
                    "applicability",
                ),
                counterexamples=_require_string(
                    lesson_arguments,
                    "counterexamples",
                ),
            ),
        )
    return _success(
        dict(
            service.review_lesson_generalization_proposal(
                _require_string(arguments, "proposal_id"),
                decision,
                _require_string(arguments, "rationale_code"),
                generalized_lesson,
            )
        ),
        "Failure generalization proposal review recorded.",
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


def _causal_assessment_payload(
    assessment: CausalAssessmentRecord,
) -> dict[str, object]:
    return {
        "causal_assessment_id": assessment.id,
        "capture_attempt_id": assessment.capture_attempt_id,
        "state": assessment.draft.state.value,
        "unknown_reason": assessment.draft.unknown_reason,
        "factors": [
            {
                "factor_id": factor_id,
                "role": factor.role.value,
                "layer": factor.layer.value,
                "failure_mode": factor.failure_mode.value,
                "component_reference": factor.component_reference,
                "evidence_summary": factor.evidence_summary,
                "confidence": factor.confidence.value,
            }
            for factor_id, factor in zip(
                assessment.factor_ids,
                assessment.draft.factors,
                strict=True,
            )
        ],
        "recommendations": [
            {
                "recommendation_id": recommendation_id,
                "target_layer": recommendation.target_layer.value,
                "target_reference": recommendation.target_reference,
                "recommended_change": recommendation.recommended_change,
                "verification_action": recommendation.verification_action,
                "rationale": recommendation.rationale,
                "confidence": recommendation.confidence.value,
            }
            for recommendation_id, recommendation in zip(
                assessment.recommendation_ids,
                assessment.draft.recommendations,
                strict=True,
            )
        ],
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
        "cause_layer": (None if candidate.cause_layer is None else candidate.cause_layer.value),
        "failure_mode": (None if candidate.failure_mode is None else candidate.failure_mode.value),
        "repair_target_layer": (
            None if candidate.repair_target_layer is None else candidate.repair_target_layer.value
        ),
        "cluster_review_id": candidate.cluster_review_id,
        "cluster_key": candidate.cluster_key,
        "cluster_supporting_lesson_version_ids": list(
            candidate.cluster_supporting_lesson_version_ids
        ),
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


def _require_object_array(
    arguments: Mapping[str, object],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> tuple[Mapping[str, object], ...]:
    value = arguments.get(key)
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise BoundaryValidationError(
            f"{key} must be an array with between {minimum} and {maximum} items"
        )
    if not all(isinstance(item, Mapping) for item in value):
        raise BoundaryValidationError(f"{key} items must be objects")
    return tuple(value)


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


def _parse_enum[EnumT](
    enum_type: type[EnumT],
    value: str,
    label: str,
) -> EnumT:
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except ValueError as exc:
        raise BoundaryValidationError(f"{label} has invalid value: {value}") from exc


def _optional_cause_layer(
    arguments: Mapping[str, object],
    key: str,
) -> CauseLayer | None:
    value = _optional_string(arguments, key)
    return None if value is None else _parse_enum(CauseLayer, value, key)


def _optional_failure_mode(
    arguments: Mapping[str, object],
    key: str,
) -> FailureMode | None:
    value = _optional_string(arguments, key)
    return None if value is None else _parse_enum(FailureMode, value, key)


def _success(payload: dict[str, object], summary: str) -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": summary}],
        "structuredContent": payload,
        "isError": False,
    }


def _error(
    message: str,
    code: str,
    *,
    field: str | None = None,
    expected: str | None = None,
    trace_id: str | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if field is not None:
        error["field"] = field
    if expected is not None:
        error["expected"] = expected
    if trace_id is not None:
        error["trace_id"] = trace_id
    payload: dict[str, object] = {"error": error}
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": payload,
        "isError": True,
    }
