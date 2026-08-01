package e2e

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	_ "modernc.org/sqlite"

	"github.com/CongBao/failure-memory/internal/cli"
	"github.com/CongBao/failure-memory/internal/config"
	"github.com/CongBao/failure-memory/internal/model"
)

func TestCLIHelperProcess(t *testing.T) {
	if os.Getenv("FAILURE_MEMORY_E2E_HELPER") != "1" {
		return
	}
	arguments := helperArguments(os.Args)
	if len(arguments) > 0 && arguments[0] == "crash-writer" {
		crashUncommittedWriter()
	}
	os.Exit(cli.Run(arguments, os.Stdin, os.Stdout, os.Stderr))
}

func TestMCPInitializesAndCallsBothToolsWithRestrictedPATH(t *testing.T) {
	root := t.TempDir()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	command := helperCommand(root, "mcp", "--stdio")
	command.Env = helperEnvironment(root, "codex")
	var stderr bytes.Buffer
	command.Stderr = &stderr
	client := mcp.NewClient(&mcp.Implementation{
		Name: "failure-memory-e2e", Version: "1",
	}, nil)
	session, err := client.Connect(ctx, &mcp.CommandTransport{Command: command}, nil)
	if err != nil {
		t.Fatalf("initialize MCP: %v; stderr=%s", err, stderr.String())
	}
	defer session.Close()
	listed, err := session.ListTools(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(listed.Tools) != 2 {
		t.Fatalf("tools = %#v", listed.Tools)
	}
	names := map[string]bool{}
	for _, tool := range listed.Tools {
		names[tool.Name] = true
	}
	if !names["remember_failure"] || !names["recall_failure_lessons"] {
		t.Fatalf("tool names = %#v", names)
	}
	remembered, err := session.CallTool(ctx, &mcp.CallToolParams{
		Name: "remember_failure", Arguments: failureInput(),
	})
	if err != nil || remembered.IsError {
		t.Fatalf("remember_failure: result=%#v err=%v stderr=%s", remembered, err, stderr.String())
	}
	recalled, err := session.CallTool(ctx, &mcp.CallToolParams{
		Name: "recall_failure_lessons",
		Arguments: model.RecallInput{
			Text: "safe shared store migration", Component: "event store", TopK: 3,
		},
	})
	if err != nil || recalled.IsError {
		t.Fatalf("recall_failure_lessons: result=%#v err=%v stderr=%s", recalled, err, stderr.String())
	}
}

func TestConcurrentHarnessProcessesShareOneRecoverableStore(t *testing.T) {
	root := t.TempDir()
	input, err := json.Marshal(failureInput())
	if err != nil {
		t.Fatal(err)
	}
	harnesses := []string{
		"codex", "claude-code", "copilot-cli", "cursor",
		"codex", "claude-code", "copilot-cli", "cursor",
	}
	var wait sync.WaitGroup
	errorsSeen := make(chan error, len(harnesses))
	for _, harness := range harnesses {
		harness := harness
		wait.Add(1)
		go func() {
			defer wait.Done()
			command := helperCommand(root, "remember")
			command.Env = helperEnvironment(root, harness)
			command.Stdin = bytes.NewReader(input)
			output, err := command.CombinedOutput()
			if err != nil {
				errorsSeen <- fmt.Errorf("%s: %w: %s", harness, err, output)
			}
		}()
	}
	wait.Wait()
	close(errorsSeen)
	for err := range errorsSeen {
		t.Error(err)
	}
	if t.Failed() {
		return
	}

	crash := helperCommand(root, "crash-writer")
	crash.Env = helperEnvironment(root, "crash-test")
	if err := crash.Run(); err == nil {
		t.Fatal("synthetic crash writer exited successfully")
	}

	output := runHelper(t, root, "doctor", nil)
	var doctor map[string]any
	if err := json.Unmarshal(output, &doctor); err != nil {
		t.Fatal(err)
	}
	if doctor["integrity_check"] != "ok" || doctor["retrieval_index_complete"] != true {
		t.Fatalf("doctor = %#v", doctor)
	}
	counts := doctor["counts"].(map[string]any)
	if counts["lessons"] != float64(1) || counts["incidents"] != float64(len(harnesses)) {
		t.Fatalf("counts = %#v", counts)
	}
	statusOutput := runHelper(t, root, "store-status", nil)
	var status map[string]any
	if err := json.Unmarshal(statusOutput, &status); err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(fmt.Sprint(status["store_id"])) == "" {
		t.Fatalf("store status = %#v", status)
	}
}

func failureInput() model.RememberInput {
	return model.RememberInput{
		Summary:        "A persisted store migration skipped its compatibility preflight.",
		Classification: model.RealFailure,
		Expectation: &model.ExpectationEvidence{
			Invariant: "Run compatibility validation before changing persisted data.",
			Source:    "existing release gate",
			Evidence:  "The gate existed before implementation.",
		},
		Observed: &model.ObservedEvidence{
			Outcome: "The migration ran without the compatibility validation.",
			Impact:  "Existing local lessons could become unreadable.",
		},
		Cause: &model.CauseEvidence{
			Layer:             "schema_migration",
			FailureMode:       "insufficient_validation",
			Component:         "event store",
			Evidence:          "No migration fixture was executed.",
			RecommendedChange: "Gate migration changes on an old-store fixture.",
			Verification:      "Upgrade the fixture and compare all IDs and hashes.",
			Confidence:        "high",
		},
		Lesson: &model.LessonEvidence{
			Title:        "Validate event-store migrations",
			Rule:         "Persisted event-store changes require an old-version fixture.",
			Prevention:   "Run the migration fixture before publishing the schema change.",
			Verification: "All event IDs and payload hashes remain unchanged after upgrade.",
		},
	}
}

func helperArguments(arguments []string) []string {
	for index, argument := range arguments {
		if argument == "--" {
			return arguments[index+1:]
		}
	}
	return nil
}

func helperCommand(root string, arguments ...string) *exec.Cmd {
	commandArguments := []string{"-test.run=^TestCLIHelperProcess$", "--"}
	commandArguments = append(commandArguments, arguments...)
	command := exec.Command(os.Args[0], commandArguments...)
	command.Env = helperEnvironment(root, "test")
	return command
}

func helperEnvironment(root string, harness string) []string {
	result := make([]string, 0, len(os.Environ())+4)
	for _, entry := range os.Environ() {
		if strings.HasPrefix(entry, "PATH=") ||
			strings.HasPrefix(entry, "FAILURE_MEMORY_HOME=") ||
			strings.HasPrefix(entry, "FAILURE_MEMORY_HARNESS=") ||
			strings.HasPrefix(entry, "FAILURE_MEMORY_E2E_HELPER=") {
			continue
		}
		result = append(result, entry)
	}
	return append(result,
		"PATH=/usr/bin:/bin",
		"FAILURE_MEMORY_HOME="+root,
		"FAILURE_MEMORY_HARNESS="+harness,
		"FAILURE_MEMORY_E2E_HELPER=1",
	)
}

func runHelper(t *testing.T, root string, commandName string, input io.Reader) []byte {
	t.Helper()
	command := helperCommand(root, commandName)
	command.Env = helperEnvironment(root, "test")
	command.Stdin = input
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("%s: %v: %s", commandName, err, output)
	}
	return output
}

func crashUncommittedWriter() {
	paths, err := config.ResolvePaths()
	if err != nil {
		os.Exit(90)
	}
	db, err := sql.Open("sqlite", "file:"+paths.EventStore+"?_pragma=busy_timeout(5000)")
	if err != nil {
		os.Exit(90)
	}
	tx, err := db.Begin()
	if err != nil {
		os.Exit(90)
	}
	payload := `{"state":"uncommitted"}`
	digest := sha256.Sum256([]byte(payload))
	_, err = tx.Exec(`
		INSERT INTO event_log(
			event_id, event_type, schema_version, occurred_at, source_harness,
			workspace_fingerprint, session_fingerprint, transport, operation_id,
			payload_json, payload_sha256
		) VALUES (?, ?, 1, ?, ?, '', '', 'test', ?, ?, ?)
	`,
		"event-uncommitted-crash",
		"synthetic_uncommitted",
		time.Now().UTC().Format(time.RFC3339Nano),
		"crash-test",
		"crash-operation",
		payload,
		hex.EncodeToString(digest[:]),
	)
	if err != nil {
		os.Exit(90)
	}
	os.Exit(91)
}
