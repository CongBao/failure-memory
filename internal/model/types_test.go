package model

import (
	"encoding/json"
	"testing"
)

func TestConfidenceAcceptsCanonicalStringsAndNumericCompatibility(t *testing.T) {
	t.Parallel()
	tests := []struct {
		input string
		want  Confidence
	}{
		{input: `"high"`, want: "high"},
		{input: `"0.7"`, want: "medium"},
		{input: `0.95`, want: "high"},
		{input: `0.5`, want: "medium"},
		{input: `0.2`, want: "low"},
	}
	for _, test := range tests {
		var confidence Confidence
		if err := json.Unmarshal([]byte(test.input), &confidence); err != nil {
			t.Fatalf("unmarshal %s: %v", test.input, err)
		}
		if confidence != test.want {
			t.Fatalf("unmarshal %s = %q, want %q", test.input, confidence, test.want)
		}
	}
}

func TestConfidenceRejectsOutOfRangeOrUnknownValues(t *testing.T) {
	t.Parallel()
	for _, input := range []string{`1.1`, `-0.1`, `"certain"`, `{}`} {
		var confidence Confidence
		if err := json.Unmarshal([]byte(input), &confidence); err == nil {
			t.Fatalf("invalid confidence %s was accepted as %q", input, confidence)
		}
	}
}
