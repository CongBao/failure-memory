from __future__ import annotations

from datetime import UTC

from failure_memory.domain.capture import (
    CaptureAssessment,
    CaptureDecision,
    Classification,
    ExpectationSource,
    FailureCandidate,
    ReasonCode,
)

_UPDATE_CLASSES = {
    Classification.REQUIREMENT_UPDATE,
    Classification.REQUIREMENT_CLARIFICATION,
    Classification.PREFERENCE_UPDATE,
}


def _expectation_precedes_outcome(
    candidate: FailureCandidate,
) -> bool:
    if candidate.expectation_preexisted is not None:
        return candidate.expectation_preexisted and bool(
            (candidate.expectation_evidence or "").strip()
        )
    expectation_established_at = candidate.expectation_established_at
    observed_outcome_at = candidate.observed_outcome_at
    if expectation_established_at is None:
        return False
    if expectation_established_at.utcoffset() is None:
        return False
    if observed_outcome_at.utcoffset() is None:
        return False
    expectation_utc = expectation_established_at.astimezone(UTC)
    outcome_utc = observed_outcome_at.astimezone(UTC)
    return expectation_utc < outcome_utc


def evaluate_candidate(candidate: FailureCandidate) -> CaptureAssessment:
    if candidate.classification in _UPDATE_CLASSES:
        return CaptureAssessment(
            CaptureDecision.REJECT,
            (ReasonCode.NOT_PREEXISTING_REQUIREMENT,),
            1.0,
        )
    if candidate.classification is Classification.UNCERTAIN:
        return CaptureAssessment(
            CaptureDecision.DEFER,
            (ReasonCode.UNCERTAIN_CLASSIFICATION,),
            0.0,
        )
    if (
        candidate.classification is Classification.MIXED
        and not (candidate.failure_portion_summary or "").strip()
    ):
        return CaptureAssessment(
            CaptureDecision.DEFER,
            (ReasonCode.MIXED_FAILURE_NOT_SEPARATED,),
            0.5,
        )

    reasons: list[ReasonCode] = []
    if candidate.expectation_source is ExpectationSource.NONE:
        reasons.append(ReasonCode.EXPECTATION_SOURCE_MISSING)
    if not _expectation_precedes_outcome(candidate):
        reasons.append(ReasonCode.EXPECTATION_NOT_ESTABLISHED_BEFORE_OUTCOME)
    if not candidate.outcome_mismatch:
        reasons.append(ReasonCode.NO_INSPECTABLE_MISMATCH)
    if not candidate.material_impact_or_recurrence_risk:
        reasons.append(ReasonCode.NO_MATERIAL_IMPACT_OR_RECURRENCE_RISK)
    if not candidate.controllable_with_prior_information:
        reasons.append(ReasonCode.NOT_CONTROLLABLE_WITH_PRIOR_INFORMATION)
    if not candidate.durable_lesson:
        reasons.append(ReasonCode.NO_DURABLE_LESSON)

    if reasons:
        return CaptureAssessment(CaptureDecision.REJECT, tuple(reasons), 1.0)
    return CaptureAssessment(
        CaptureDecision.ACCEPT,
        (ReasonCode.REAL_FAILURE_CRITERIA_MET,),
        1.0,
    )
