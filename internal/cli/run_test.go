package cli

import (
	"bytes"
	"encoding/json"
	"path/filepath"
	"testing"

	"github.com/CongBao/failure-memory/internal/model"
)

func TestBackupCommandsRoundTripTheAuthoritativeStore(t *testing.T) {
	root := t.TempDir()
	t.Setenv("FAILURE_MEMORY_HOME", root)
	t.Setenv("FAILURE_MEMORY_HARNESS", "cli-test")
	input, err := json.Marshal(cliFailureInput())
	if err != nil {
		t.Fatal(err)
	}
	if code, _, stderr := runForTest([]string{"remember"}, input); code != 0 {
		t.Fatalf("remember: code=%d stderr=%s", code, stderr)
	}
	backupPath := filepath.Join(root, "test-backup")
	if code, _, stderr := runForTest(
		[]string{"backup", "create", "--output", backupPath}, nil,
	); code != 0 {
		t.Fatalf("backup create: code=%d stderr=%s", code, stderr)
	}
	if code, _, stderr := runForTest(
		[]string{"backup", "verify", backupPath}, nil,
	); code != 0 {
		t.Fatalf("backup verify: code=%d stderr=%s", code, stderr)
	}
	if code, _, stderr := runForTest([]string{"remember"}, input); code != 0 {
		t.Fatalf("second remember: code=%d stderr=%s", code, stderr)
	}
	if code, _, _ := runForTest([]string{"backup", "restore", backupPath}, nil); code != 2 {
		t.Fatalf("restore without --replace code = %d, want 2", code)
	}
	if code, _, stderr := runForTest(
		[]string{"backup", "restore", backupPath, "--replace"}, nil,
	); code != 0 {
		t.Fatalf("backup restore: code=%d stderr=%s", code, stderr)
	}
	code, stdout, stderr := runForTest([]string{"doctor"}, nil)
	if code != 0 {
		t.Fatalf("doctor: code=%d stderr=%s", code, stderr)
	}
	var doctor map[string]any
	if err := json.Unmarshal(stdout, &doctor); err != nil {
		t.Fatal(err)
	}
	counts := doctor["counts"].(map[string]any)
	if counts["incidents"] != float64(1) || doctor["retrieval_index_complete"] != true {
		t.Fatalf("doctor after restore = %#v", doctor)
	}
}

func TestRememberFallbackAcceptsNumericConfidence(t *testing.T) {
	t.Setenv("FAILURE_MEMORY_HOME", t.TempDir())
	t.Setenv("FAILURE_MEMORY_HARNESS", "cli-test")
	payload, err := json.Marshal(cliFailureInput())
	if err != nil {
		t.Fatal(err)
	}
	var input map[string]any
	if err := json.Unmarshal(payload, &input); err != nil {
		t.Fatal(err)
	}
	input["cause"].(map[string]any)["confidence"] = 0.9
	payload, err = json.Marshal(input)
	if err != nil {
		t.Fatal(err)
	}
	code, stdout, stderr := runForTest([]string{"remember"}, payload)
	if code != 0 {
		t.Fatalf("remember: code=%d stderr=%s", code, stderr)
	}
	var result model.RememberResult
	if err := json.Unmarshal(stdout, &result); err != nil {
		t.Fatal(err)
	}
	if result.Status != model.Recorded || result.LessonID == "" || result.Retryable {
		t.Fatalf("result = %#v", result)
	}
}

func TestRememberFallbackReturnsOneMachineGuidedCorrection(t *testing.T) {
	t.Setenv("FAILURE_MEMORY_HOME", t.TempDir())
	t.Setenv("FAILURE_MEMORY_HARNESS", "cli-test")
	invalid := cliFailureInput()
	invalid.Cause.Layer = "skill_tool_contract"
	invalid.Cause.FailureMode = "schema_mismatch"
	payload, err := json.Marshal(invalid)
	if err != nil {
		t.Fatal(err)
	}
	code, stdout, stderr := runForTest([]string{"remember"}, payload)
	if code != 0 {
		t.Fatalf("first remember: code=%d stderr=%s", code, stderr)
	}
	var first model.RememberResult
	if err := json.Unmarshal(stdout, &first); err != nil {
		t.Fatal(err)
	}
	if !first.Retryable || first.Correction == nil || first.CaptureEventID == "" {
		t.Fatalf("first result = %#v", first)
	}

	corrected := cliFailureInput()
	corrected.CorrectionOf = first.CaptureEventID
	payload, err = json.Marshal(corrected)
	if err != nil {
		t.Fatal(err)
	}
	code, stdout, stderr = runForTest([]string{"remember"}, payload)
	if code != 0 {
		t.Fatalf("corrected remember: code=%d stderr=%s", code, stderr)
	}
	var second model.RememberResult
	if err := json.Unmarshal(stdout, &second); err != nil {
		t.Fatal(err)
	}
	if second.Status != model.Recorded || second.Retryable || second.LessonID == "" {
		t.Fatalf("corrected result = %#v", second)
	}
}

func runForTest(arguments []string, input []byte) (int, []byte, []byte) {
	var stdout, stderr bytes.Buffer
	code := Run(arguments, bytes.NewReader(input), &stdout, &stderr)
	return code, stdout.Bytes(), stderr.Bytes()
}

func cliFailureInput() model.RememberInput {
	return model.RememberInput{
		Summary:        "The event-store upgrade skipped its compatibility fixture.",
		Classification: model.RealFailure,
		Expectation: &model.ExpectationEvidence{
			Invariant: "Run an old-store fixture before migration.",
			Source:    "release gate",
			Evidence:  "The gate existed before the change.",
		},
		Observed: &model.ObservedEvidence{
			Outcome: "The migration changed without the fixture.",
			Impact:  "Existing lessons could become unreadable.",
		},
		Cause: &model.CauseEvidence{
			Layer:             "schema_migration",
			FailureMode:       "insufficient_validation",
			Component:         "event store",
			Evidence:          "No fixture result was retained.",
			RecommendedChange: "Make the old-store fixture a release gate.",
			Verification:      "Upgrade and compare event IDs and hashes.",
		},
		Lesson: &model.LessonEvidence{
			Rule:         "Every persisted-store migration needs an old-store fixture.",
			Prevention:   "Run the fixture before publishing migration code.",
			Verification: "All IDs and event hashes remain unchanged.",
		},
	}
}
