from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class CausalAssessmentState(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class CauseLayer(StrEnum):
    SKILL_INSTRUCTION = "skill_instruction"
    AGENT_INSTRUCTION = "agent_instruction"
    PROJECT_INSTRUCTION = "project_instruction"
    SYSTEM_INSTRUCTION = "system_instruction"
    HOOK_POLICY = "hook_policy"
    PLUGIN_MANIFEST = "plugin_manifest"
    TOOL_CONTRACT = "tool_contract"
    APPLICATION_LOGIC = "application_logic"
    ADAPTER_RUNTIME = "adapter_runtime"
    SCHEMA_MIGRATION = "schema_migration"
    TEST_EVALUATION_GAP = "test_evaluation_gap"
    HARNESS_LIMITATION = "harness_limitation"
    MODEL_BEHAVIOR = "model_behavior"
    EXTERNAL_DEPENDENCY = "external_dependency"
    UNKNOWN = "unknown"


class FailureMode(StrEnum):
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    NOT_LOADED = "not_loaded"
    NOT_TRIGGERED = "not_triggered"
    IGNORED = "ignored"
    INCORRECTLY_IMPLEMENTED = "incorrectly_implemented"
    INSUFFICIENT_VALIDATION = "insufficient_validation"
    UNINSPECTABLE = "uninspectable"
    UNKNOWN = "unknown"


class CausalFactorRole(StrEnum):
    PRIMARY = "primary"
    CONTRIBUTING = "contributing"


class CausalConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class RepairOutcomeKind(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"
    PARTIALLY_APPLIED = "partially_applied"
    VERIFIED_EFFECTIVE = "verified_effective"
    VERIFIED_INEFFECTIVE = "verified_ineffective"
    RECURRENCE_OBSERVED = "recurrence_observed"
    SUPERSEDED = "superseded"


_COMPONENT_REFERENCE = re.compile(r"[a-z][a-z0-9_-]{0,31}:[a-z0-9][a-z0-9_.:/-]{0,159}")
_DETAIL_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must be non-empty")


def _validate_component_reference(value: str, label: str) -> None:
    if _COMPONENT_REFERENCE.fullmatch(value.strip()) is None:
        raise ValueError(f"{label} must be a portable namespaced reference")


@dataclass(frozen=True, slots=True)
class CausalFactorDraft:
    role: CausalFactorRole
    layer: CauseLayer
    failure_mode: FailureMode
    component_reference: str
    evidence_summary: str
    confidence: CausalConfidence

    def __post_init__(self) -> None:
        _validate_component_reference(self.component_reference, "component_reference")
        _require_text(self.evidence_summary, "evidence_summary")


@dataclass(frozen=True, slots=True)
class RepairRecommendationDraft:
    target_layer: CauseLayer
    target_reference: str
    recommended_change: str
    verification_action: str
    rationale: str
    confidence: CausalConfidence

    def __post_init__(self) -> None:
        _validate_component_reference(self.target_reference, "target_reference")
        _require_text(self.recommended_change, "recommended_change")
        _require_text(self.verification_action, "verification_action")
        _require_text(self.rationale, "rationale")


@dataclass(frozen=True, slots=True)
class CausalAssessmentDraft:
    state: CausalAssessmentState
    factors: tuple[CausalFactorDraft, ...]
    recommendations: tuple[RepairRecommendationDraft, ...]
    unknown_reason: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.factors) <= 4:
            raise ValueError("causal assessment requires between one and four factors")
        if sum(factor.role is CausalFactorRole.PRIMARY for factor in self.factors) != 1:
            raise ValueError("causal assessment requires exactly one primary factor")
        if not 1 <= len(self.recommendations) <= 3:
            raise ValueError("causal assessment requires between one and three recommendations")
        reason = None if self.unknown_reason is None else self.unknown_reason.strip()
        if self.state is CausalAssessmentState.UNKNOWN:
            if not reason:
                raise ValueError("unknown causal assessment requires an unknown reason")
            primary = next(
                factor for factor in self.factors if factor.role is CausalFactorRole.PRIMARY
            )
            if primary.layer is not CauseLayer.UNKNOWN:
                raise ValueError("unknown causal assessment requires an unknown primary layer")
            if primary.failure_mode not in {FailureMode.UNKNOWN, FailureMode.UNINSPECTABLE}:
                raise ValueError("unknown causal assessment requires unknown or uninspectable mode")
        elif reason:
            raise ValueError("supported or partial assessment cannot have an unknown reason")


@dataclass(frozen=True, slots=True)
class CausalAssessmentRecord:
    id: str
    capture_attempt_id: str
    created_at: datetime
    draft: CausalAssessmentDraft
    factor_ids: tuple[str, ...]
    recommendation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    recommendation_id: str
    outcome: RepairOutcomeKind
    detail_code: str
    evidence_summary: str
    confidence: CausalConfidence

    def __post_init__(self) -> None:
        if _DETAIL_CODE.fullmatch(self.detail_code.strip()) is None:
            raise ValueError("detail_code must be a bounded machine code")
        _require_text(self.evidence_summary, "evidence_summary")
