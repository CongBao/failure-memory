from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Classification(StrEnum):
    REQUIREMENT_UPDATE = "requirement_update"
    REQUIREMENT_CLARIFICATION = "requirement_clarification"
    PREFERENCE_UPDATE = "preference_update"
    REAL_FAILURE = "real_failure"
    MIXED = "mixed"
    UNCERTAIN = "uncertain"


class CaptureDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"


class ExpectationSource(StrEnum):
    EXPLICIT_REQUIREMENT = "explicit_requirement"
    ACCEPTED_DESIGN = "accepted_design"
    REPOSITORY_CONTRACT = "repository_contract"
    PRIOR_TOOL_EVIDENCE = "prior_tool_evidence"
    DOMAIN_INVARIANT = "domain_invariant"
    NONE = "none"


class ReasonCode(StrEnum):
    NOT_PREEXISTING_REQUIREMENT = "not_preexisting_requirement"
    UNCERTAIN_CLASSIFICATION = "uncertain_classification"
    MIXED_FAILURE_NOT_SEPARATED = "mixed_failure_not_separated"
    EXPECTATION_SOURCE_MISSING = "expectation_source_missing"
    EXPECTATION_NOT_ESTABLISHED_BEFORE_OUTCOME = "expectation_not_established_before_outcome"
    NO_INSPECTABLE_MISMATCH = "no_inspectable_mismatch"
    NO_MATERIAL_IMPACT_OR_RECURRENCE_RISK = "no_material_impact_or_recurrence_risk"
    NOT_CONTROLLABLE_WITH_PRIOR_INFORMATION = "not_controllable_with_prior_information"
    NO_DURABLE_LESSON = "no_durable_lesson"
    REAL_FAILURE_CRITERIA_MET = "real_failure_criteria_met"


@dataclass(frozen=True)
class FailureCandidate:
    summary: str
    classification: Classification
    expectation_source: ExpectationSource
    expectation_established_at: datetime | None
    observed_outcome_at: datetime
    outcome_mismatch: bool
    material_impact_or_recurrence_risk: bool
    controllable_with_prior_information: bool
    durable_lesson: bool
    failure_portion_summary: str | None = None
    expectation_preexisted: bool | None = None
    expectation_evidence: str | None = None


@dataclass(frozen=True)
class CaptureAssessment:
    decision: CaptureDecision
    reason_codes: tuple[ReasonCode, ...]
    confidence: float
    policy_version: str = "tier1-v1"
