from __future__ import annotations

import pytest

from failure_memory.domain.causal import (
    CausalAssessmentDraft,
    CausalAssessmentState,
    CausalConfidence,
    CausalFactorDraft,
    CausalFactorRole,
    CauseLayer,
    FailureMode,
    RepairRecommendationDraft,
)


def _factor(
    *,
    role: CausalFactorRole = CausalFactorRole.PRIMARY,
    layer: CauseLayer = CauseLayer.SKILL_INSTRUCTION,
    failure_mode: FailureMode = FailureMode.AMBIGUOUS,
) -> CausalFactorDraft:
    return CausalFactorDraft(
        role=role,
        layer=layer,
        failure_mode=failure_mode,
        component_reference="skill:record-agent-failure",
        evidence_summary="The applicable instruction did not define the required causal step.",
        confidence=CausalConfidence.HIGH,
    )


def _recommendation(
    *,
    layer: CauseLayer = CauseLayer.SKILL_INSTRUCTION,
) -> RepairRecommendationDraft:
    return RepairRecommendationDraft(
        target_layer=layer,
        target_reference="skill:record-agent-failure",
        recommended_change="Require causal diagnosis before similarity review.",
        verification_action="Test the accepted capture workflow order.",
        rationale="The missing workflow gate allowed symptom-only lessons.",
        confidence=CausalConfidence.HIGH,
    )


def test_supported_assessment_requires_one_primary_and_bounded_recommendations() -> None:
    assessment = CausalAssessmentDraft(
        state=CausalAssessmentState.SUPPORTED,
        factors=(
            _factor(),
            _factor(role=CausalFactorRole.CONTRIBUTING, layer=CauseLayer.HOOK_POLICY),
        ),
        recommendations=(_recommendation(),),
    )

    assert assessment.factors[0].role is CausalFactorRole.PRIMARY
    assert assessment.recommendations[0].target_layer is CauseLayer.SKILL_INSTRUCTION


def test_unknown_assessment_preserves_uncertainty_instead_of_guessing() -> None:
    assessment = CausalAssessmentDraft(
        state=CausalAssessmentState.UNKNOWN,
        factors=(
            _factor(
                layer=CauseLayer.UNKNOWN,
                failure_mode=FailureMode.UNINSPECTABLE,
            ),
        ),
        recommendations=(_recommendation(layer=CauseLayer.UNKNOWN),),
        unknown_reason="The controlling instruction source is not inspectable.",
    )

    assert assessment.unknown_reason is not None


@pytest.mark.parametrize(
    "reference",
    ["/absolute/private/path", "skill with spaces", "skill:", "SKILL:record"],
)
def test_component_references_are_portable_and_namespaced(reference: str) -> None:
    with pytest.raises(ValueError, match="portable namespaced reference"):
        CausalFactorDraft(
            role=CausalFactorRole.PRIMARY,
            layer=CauseLayer.SKILL_INSTRUCTION,
            failure_mode=FailureMode.MISSING,
            component_reference=reference,
            evidence_summary="Evidence.",
            confidence=CausalConfidence.MEDIUM,
        )


def test_unknown_assessment_rejects_a_guessed_layer() -> None:
    with pytest.raises(ValueError, match="unknown primary layer"):
        CausalAssessmentDraft(
            state=CausalAssessmentState.UNKNOWN,
            factors=(_factor(),),
            recommendations=(_recommendation(),),
            unknown_reason="Evidence is incomplete.",
        )
