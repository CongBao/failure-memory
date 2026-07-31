from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from failure_memory.domain.capture import (
    CaptureDecision,
    Classification,
    ExpectationSource,
    ReasonCode,
)
from failure_memory.domain.causal import CausalConfidence, CauseLayer, FailureMode
from failure_memory.domain.records import IncidentLessonRelation


class RememberStatus(StrEnum):
    RECORDED = "recorded"
    NOT_FAILURE = "not_failure"
    DEFERRED = "deferred"


class DeduplicationStatus(StrEnum):
    NOT_RUN = "not_run"
    EXACT_REUSE = "exact_reuse"
    DISTINCT = "distinct"
    RELATED_PENDING_GENERALIZATION = "related_pending_generalization"


@dataclass(frozen=True, slots=True)
class ExpectationEvidence:
    invariant: str
    source: ExpectationSource
    evidence: str


@dataclass(frozen=True, slots=True)
class ObservedEvidence:
    outcome: str
    impact: str
    recurrence_risk: str | None = None


@dataclass(frozen=True, slots=True)
class CauseEvidence:
    layer: CauseLayer
    failure_mode: FailureMode
    component: str
    evidence: str
    recommended_change: str
    verification: str
    confidence: CausalConfidence = CausalConfidence.MEDIUM


@dataclass(frozen=True, slots=True)
class LessonEvidence:
    rule: str
    prevention: str
    verification: str
    title: str | None = None
    applicability: str | None = None
    counterexamples: str | None = None


@dataclass(frozen=True, slots=True)
class RememberFailureDraft:
    summary: str
    classification: Classification
    expectation: ExpectationEvidence | None = None
    observed: ObservedEvidence | None = None
    cause: CauseEvidence | None = None
    lesson: LessonEvidence | None = None
    failure_portion: str | None = None


@dataclass(frozen=True, slots=True)
class RememberFailureResult:
    operation_id: str
    status: RememberStatus
    capture_attempt_id: str
    decision: CaptureDecision
    reason_codes: tuple[ReasonCode, ...]
    deduplication_status: DeduplicationStatus
    semantic_status: str
    total_latency_ms: int
    incident_id: str | None = None
    lesson_id: str | None = None
    lesson_version_id: str | None = None
    relation: IncidentLessonRelation | None = None
    causal_assessment_id: str | None = None
    repair_recommendation_id: str | None = None
    generalization_review_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecordingTrace:
    operation_id: str
    transport: str
    workflow_version: str
    status: RememberStatus
    decision: CaptureDecision
    deduplication_status: DeduplicationStatus
    semantic_status: str
    total_latency_ms: int
    qualification_latency_ms: int
    causal_latency_ms: int
    deduplication_latency_ms: int
    persistence_latency_ms: int
    capture_attempt_id: str
    incident_id: str | None = None
    lesson_version_id: str | None = None
    error_code: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
