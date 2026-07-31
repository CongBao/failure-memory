package policy

import (
	"testing"

	"github.com/CongBao/failure-memory/internal/model"
)

func TestQualifyDistinguishesUpdatesFromFailures(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name     string
		input    model.RememberInput
		decision model.Decision
	}{
		{
			name: "new requirement",
			input: model.RememberInput{
				Summary:        "The user requested a new field after delivery.",
				Classification: model.RequirementUpdate,
			},
			decision: model.Reject,
		},
		{
			name: "new detail",
			input: model.RememberInput{
				Summary:        "A previously unavailable detail was supplied.",
				Classification: model.RequirementClarification,
			},
			decision: model.Reject,
		},
		{
			name: "first preference",
			input: model.RememberInput{
				Summary:        "The user stated a preference for the first time.",
				Classification: model.PreferenceUpdate,
			},
			decision: model.Reject,
		},
		{
			name:     "real failure",
			input:    realFailure(),
			decision: model.Accept,
		},
		{
			name: "mixed not separated",
			input: func() model.RememberInput {
				input := realFailure()
				input.Classification = model.Mixed
				return input
			}(),
			decision: model.Defer,
		},
		{
			name: "mixed separated",
			input: func() model.RememberInput {
				input := realFailure()
				input.Classification = model.Mixed
				input.FailurePortion = "The established preflight was skipped."
				return input
			}(),
			decision: model.Accept,
		},
		{
			name: "failure without evidence",
			input: model.RememberInput{
				Summary:        "Something was wrong.",
				Classification: model.RealFailure,
			},
			decision: model.Defer,
		},
		{
			name: "failure with ungoverned cause taxonomy",
			input: func() model.RememberInput {
				input := realFailure()
				input.Cause.Layer = "whatever"
				input.Cause.FailureMode = "bad"
				return input
			}(),
			decision: model.Defer,
		},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			if got := Qualify(test.input).Decision; got != test.decision {
				t.Fatalf("decision = %q, want %q", got, test.decision)
			}
		})
	}
}

func realFailure() model.RememberInput {
	return model.RememberInput{
		Summary:        "The agent skipped an established compatibility preflight.",
		Classification: model.RealFailure,
		Expectation: &model.ExpectationEvidence{
			Invariant: "Run the compatibility preflight before schema edits.",
			Source:    "loaded_skill",
			Evidence:  "The skill stated this before implementation.",
		},
		Observed: &model.ObservedEvidence{
			Outcome:        "The schema edit was made without the preflight.",
			Impact:         "The migration could write incompatible rows.",
			RecurrenceRisk: "Future migrations could repeat the omission.",
		},
		Cause: &model.CauseEvidence{
			Layer:             "skill_instruction",
			FailureMode:       "ignored",
			Component:         "schema migration workflow",
			Evidence:          "No preflight result existed before the edit.",
			RecommendedChange: "Make the preflight a completion gate.",
			Verification:      "Replay with the preflight before edits.",
			Confidence:        "high",
		},
		Lesson: &model.LessonEvidence{
			Title:         "Run schema preflight first",
			Rule:          "Do not edit persisted schema before its compatibility preflight passes.",
			Prevention:    "Run the preflight before the first edit.",
			Verification:  "Retain the passing preflight result.",
			Applicability: "Persisted schema changes.",
		},
	}
}
