package policy

import (
	"strings"

	"github.com/CongBao/failure-memory/internal/model"
)

type Assessment struct {
	Decision    model.Decision
	Status      model.RememberStatus
	ReasonCodes []string
}

func Qualify(input model.RememberInput) Assessment {
	switch input.Classification {
	case model.RequirementUpdate, model.RequirementClarification, model.PreferenceUpdate:
		return Assessment{
			Decision:    model.Reject,
			Status:      model.NotFailure,
			ReasonCodes: []string{"not_preexisting_requirement"},
		}
	case model.Uncertain:
		return Assessment{
			Decision:    model.Defer,
			Status:      model.Deferred,
			ReasonCodes: []string{"uncertain_classification"},
		}
	case model.Mixed:
		if blank(input.FailurePortion) {
			return Assessment{
				Decision:    model.Defer,
				Status:      model.Deferred,
				ReasonCodes: []string{"mixed_failure_not_separated"},
			}
		}
	case model.RealFailure:
		// Continue with evidence checks.
	default:
		return Assessment{
			Decision:    model.Defer,
			Status:      model.Deferred,
			ReasonCodes: []string{"invalid_classification"},
		}
	}

	var reasons []string
	if input.Expectation == nil || blank(input.Expectation.Invariant) ||
		blank(input.Expectation.Source) || blank(input.Expectation.Evidence) {
		reasons = append(reasons, "prior_expectation_evidence_missing")
	}
	if input.Observed == nil || blank(input.Observed.Outcome) || blank(input.Observed.Impact) {
		reasons = append(reasons, "inspectable_mismatch_or_impact_missing")
	}
	if input.Cause == nil || blank(string(input.Cause.Layer)) || blank(string(input.Cause.FailureMode)) ||
		blank(input.Cause.Component) || blank(input.Cause.Evidence) ||
		blank(input.Cause.RecommendedChange) || blank(input.Cause.Verification) {
		reasons = append(reasons, "controllable_cause_evidence_missing")
	} else {
		if !allowedCauseLayer(input.Cause.Layer) {
			reasons = append(reasons, "cause_layer_invalid")
		}
		if !allowedFailureMode(input.Cause.FailureMode) {
			reasons = append(reasons, "failure_mode_invalid")
		}
		if input.Cause.Confidence != "" && !allowedConfidence(input.Cause.Confidence) {
			reasons = append(reasons, "cause_confidence_invalid")
		}
	}
	if input.Lesson == nil || blank(input.Lesson.Rule) || blank(input.Lesson.Prevention) ||
		blank(input.Lesson.Verification) {
		reasons = append(reasons, "durable_lesson_missing")
	}
	if len(reasons) > 0 {
		return Assessment{
			Decision:    model.Defer,
			Status:      model.Deferred,
			ReasonCodes: reasons,
		}
	}
	return Assessment{
		Decision:    model.Accept,
		Status:      model.Recorded,
		ReasonCodes: []string{"real_failure_criteria_met"},
	}
}

func blank(value string) bool {
	return strings.TrimSpace(value) == ""
}

func allowedCauseLayer(value model.CauseLayer) bool {
	return oneOf(string(value), model.CauseLayerValues()...)
}

func allowedFailureMode(value model.FailureMode) bool {
	return oneOf(string(value), model.FailureModeValues()...)
}

func allowedConfidence(value model.Confidence) bool {
	return oneOf(string(value), model.ConfidenceValues()...)
}

func oneOf(value string, allowed ...string) bool {
	value = strings.ToLower(strings.TrimSpace(value))
	for _, candidate := range allowed {
		if value == candidate {
			return true
		}
	}
	return false
}
