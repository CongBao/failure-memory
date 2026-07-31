package redact

import (
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/CongBao/failure-memory/internal/model"
)

var patterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b`),
	regexp.MustCompile(`(?i)\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+`),
	regexp.MustCompile(`-----BEGIN [A-Z ]+PRIVATE KEY-----`),
}

func Text(value string) string {
	value = strings.TrimSpace(value)
	for _, pattern := range patterns {
		value = pattern.ReplaceAllString(value, "[REDACTED]")
	}
	const maximum = 4000
	if len(value) > maximum {
		value = value[:maximum]
		for !utf8.ValidString(value) {
			value = value[:len(value)-1]
		}
	}
	return value
}

func Remember(input model.RememberInput) model.RememberInput {
	input.Summary = Text(input.Summary)
	input.FailurePortion = Text(input.FailurePortion)
	input.PriorRecallID = Text(input.PriorRecallID)
	if input.Expectation != nil {
		copy := *input.Expectation
		copy.Invariant = Text(copy.Invariant)
		copy.Source = Text(copy.Source)
		copy.Evidence = Text(copy.Evidence)
		input.Expectation = &copy
	}
	if input.Observed != nil {
		copy := *input.Observed
		copy.Outcome = Text(copy.Outcome)
		copy.Impact = Text(copy.Impact)
		copy.RecurrenceRisk = Text(copy.RecurrenceRisk)
		input.Observed = &copy
	}
	if input.Cause != nil {
		copy := *input.Cause
		copy.Layer = Text(copy.Layer)
		copy.FailureMode = Text(copy.FailureMode)
		copy.Component = Text(copy.Component)
		copy.Evidence = Text(copy.Evidence)
		copy.RecommendedChange = Text(copy.RecommendedChange)
		copy.Verification = Text(copy.Verification)
		copy.Confidence = Text(copy.Confidence)
		input.Cause = &copy
	}
	if input.Lesson != nil {
		copy := *input.Lesson
		copy.Rule = Text(copy.Rule)
		copy.Prevention = Text(copy.Prevention)
		copy.Verification = Text(copy.Verification)
		copy.Title = Text(copy.Title)
		copy.Applicability = Text(copy.Applicability)
		copy.Counterexamples = Text(copy.Counterexamples)
		input.Lesson = &copy
	}
	return input
}

func Recall(input model.RecallInput) model.RecallInput {
	input.Text = Text(input.Text)
	input.ExpectedInvariant = Text(input.ExpectedInvariant)
	input.ControllableCause = Text(input.ControllableCause)
	input.PreventionAction = Text(input.PreventionAction)
	input.Component = Text(input.Component)
	return input
}
