from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from failure_memory.domain.capture import (
    CaptureDecision,
    Classification,
    ExpectationSource,
    FailureCandidate,
    ReasonCode,
)
from failure_memory.domain.policy import evaluate_candidate

OBSERVED = datetime(2026, 7, 29, 12, tzinfo=UTC)


def candidate(classification: Classification, **overrides: object) -> FailureCandidate:
    values: dict[str, object] = {
        "summary": "The agent missed an accepted schema invariant.",
        "classification": classification,
        "expectation_source": ExpectationSource.ACCEPTED_DESIGN,
        "expectation_established_at": OBSERVED - timedelta(minutes=10),
        "observed_outcome_at": OBSERVED,
        "outcome_mismatch": True,
        "material_impact_or_recurrence_risk": True,
        "controllable_with_prior_information": True,
        "durable_lesson": True,
        "failure_portion_summary": None,
    }
    values.update(overrides)
    return FailureCandidate(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "classification",
    [
        Classification.REQUIREMENT_UPDATE,
        Classification.REQUIREMENT_CLARIFICATION,
        Classification.PREFERENCE_UPDATE,
    ],
)
def test_updates_and_new_details_are_rejected(classification: Classification) -> None:
    result = evaluate_candidate(candidate(classification))
    assert result.decision is CaptureDecision.REJECT
    assert ReasonCode.NOT_PREEXISTING_REQUIREMENT in result.reason_codes
    assert result.confidence == 1.0
    assert result.policy_version == "tier1-v1"


@pytest.mark.parametrize(
    "expectation_established_at",
    [
        OBSERVED,
        OBSERVED + timedelta(seconds=1),
        None,
        datetime(2026, 7, 29, 11, 59),
    ],
)
def test_real_failure_requires_a_preexisting_aware_expectation(
    expectation_established_at: datetime | None,
) -> None:
    result = evaluate_candidate(
        candidate(
            Classification.REAL_FAILURE,
            expectation_established_at=expectation_established_at,
        )
    )
    assert result.decision is CaptureDecision.REJECT
    assert ReasonCode.EXPECTATION_NOT_ESTABLISHED_BEFORE_OUTCOME in result.reason_codes
    assert result.confidence == 1.0
    assert result.policy_version == "tier1-v1"


def test_real_failure_rejects_a_naive_observed_outcome() -> None:
    result = evaluate_candidate(
        candidate(
            Classification.REAL_FAILURE,
            observed_outcome_at=datetime(2026, 7, 29, 12),
        )
    )
    assert result.decision is CaptureDecision.REJECT
    assert result.reason_codes == (ReasonCode.EXPECTATION_NOT_ESTABLISHED_BEFORE_OUTCOME,)


def test_real_failure_compares_dst_fold_timestamps_as_utc_instants() -> None:
    new_york = ZoneInfo("America/New_York")
    result = evaluate_candidate(
        candidate(
            Classification.REAL_FAILURE,
            expectation_established_at=datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=0),
            observed_outcome_at=datetime(2026, 11, 1, 1, 15, tzinfo=new_york, fold=1),
        )
    )
    assert result.decision is CaptureDecision.ACCEPT
    assert result.reason_codes == (ReasonCode.REAL_FAILURE_CRITERIA_MET,)


@pytest.mark.parametrize(
    ("override", "reason_code"),
    [
        (
            {"expectation_source": ExpectationSource.NONE},
            ReasonCode.EXPECTATION_SOURCE_MISSING,
        ),
        ({"outcome_mismatch": False}, ReasonCode.NO_INSPECTABLE_MISMATCH),
        (
            {"material_impact_or_recurrence_risk": False},
            ReasonCode.NO_MATERIAL_IMPACT_OR_RECURRENCE_RISK,
        ),
        (
            {"controllable_with_prior_information": False},
            ReasonCode.NOT_CONTROLLABLE_WITH_PRIOR_INFORMATION,
        ),
        ({"durable_lesson": False}, ReasonCode.NO_DURABLE_LESSON),
    ],
)
def test_real_failure_requires_every_value_condition(
    override: dict[str, object],
    reason_code: ReasonCode,
) -> None:
    result = evaluate_candidate(candidate(Classification.REAL_FAILURE, **override))
    assert result.decision is CaptureDecision.REJECT
    assert result.reason_codes == (reason_code,)


def test_valid_real_failure_is_accepted() -> None:
    result = evaluate_candidate(candidate(Classification.REAL_FAILURE))
    assert result.decision is CaptureDecision.ACCEPT
    assert result.reason_codes == (ReasonCode.REAL_FAILURE_CRITERIA_MET,)
    assert result.confidence == 1.0
    assert result.policy_version == "tier1-v1"


def test_mixed_feedback_must_separate_the_failure_portion() -> None:
    deferred = evaluate_candidate(candidate(Classification.MIXED))
    accepted = evaluate_candidate(
        candidate(
            Classification.MIXED,
            failure_portion_summary="The old accepted invariant was also missed.",
        )
    )
    assert deferred.decision is CaptureDecision.DEFER
    assert deferred.confidence == 0.5
    assert deferred.policy_version == "tier1-v1"
    assert accepted.decision is CaptureDecision.ACCEPT
