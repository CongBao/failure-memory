package mcpserver

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/CongBao/failure-memory/internal/model"
)

func TestPublishedRecordContractMatchesRuntimeTaxonomyAndRetryPolicy(t *testing.T) {
	data, err := os.ReadFile("../../skills/record-agent-failure/contract.json")
	if err != nil {
		t.Fatal(err)
	}
	var contract struct {
		Version int `json:"contract_version"`
		Policy  struct {
			MaximumAttempts int `json:"maximum_call_attempts"`
			CorrectionLimit int `json:"correction_retry_limit"`
			FailureFields   struct {
				Cause struct {
					Allowed map[string][]string `json:"allowed_values"`
				} `json:"cause"`
			} `json:"failure_object_fields"`
		} `json:"policy"`
	}
	if err := json.Unmarshal(data, &contract); err != nil {
		t.Fatal(err)
	}
	if contract.Version != 7 || contract.Policy.MaximumAttempts != 2 ||
		contract.Policy.CorrectionLimit != 1 {
		t.Fatalf("contract retry policy = %#v", contract)
	}
	allowed := contract.Policy.FailureFields.Cause.Allowed
	for field, want := range map[string][]string{
		"layer":        model.CauseLayerValues(),
		"failure_mode": model.FailureModeValues(),
		"confidence":   model.ConfidenceValues(),
	} {
		if !reflect.DeepEqual(allowed[field], want) {
			t.Fatalf("contract %s = %#v, want %#v", field, allowed[field], want)
		}
	}

	skill, err := os.ReadFile("../../skills/record-agent-failure/SKILL.md")
	if err != nil {
		t.Fatal(err)
	}
	text := string(skill)
	for _, phrase := range []string{
		"correction.allowed_values",
		"correction_of_capture_event_id",
		"Never make a third call",
		"Numeric `0..1` is accepted",
	} {
		if !strings.Contains(text, phrase) {
			t.Fatalf("published skill lacks %q", phrase)
		}
	}
}
