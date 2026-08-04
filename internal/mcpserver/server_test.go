package mcpserver

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/CongBao/failure-memory/internal/service"
)

func TestServerExposesOnlyTwoPublicTools(t *testing.T) {
	t.Setenv("FAILURE_MEMORY_HOME", t.TempDir())
	svc, err := service.Open("mcp-test")
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()

	clientTransport, serverTransport := mcp.NewInMemoryTransports()
	server := New(svc)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	serverError := make(chan error, 1)
	go func() {
		serverError <- server.Run(ctx, serverTransport)
	}()

	client := mcp.NewClient(&mcp.Implementation{
		Name:    "failure-memory-test",
		Version: "1.0.0",
	}, nil)
	session, err := client.Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	tools, err := session.ListTools(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(tools.Tools) != 2 {
		t.Fatalf("tool count = %d, want 2", len(tools.Tools))
	}
	names := map[string]bool{}
	var rememberSchema any
	for _, tool := range tools.Tools {
		names[tool.Name] = true
		if tool.InputSchema == nil || tool.OutputSchema == nil {
			t.Fatalf("tool %s lacks a typed schema", tool.Name)
		}
		if tool.Name == "remember_failure" {
			rememberSchema = tool.InputSchema
		}
	}
	if !names["remember_failure"] || !names["recall_failure_lessons"] {
		t.Fatalf("unexpected tools: %#v", names)
	}
	schemaJSON, err := json.Marshal(rememberSchema)
	if err != nil {
		t.Fatal(err)
	}
	schemaText := string(schemaJSON)
	for _, required := range []string{`"enum"`, `"skill_instruction"`, `"anyOf"`} {
		if !strings.Contains(schemaText, required) {
			t.Fatalf("remember schema lacks %s: %s", required, schemaText)
		}
	}

	remembered, err := session.CallTool(ctx, &mcp.CallToolParams{
		Name: "remember_failure", Arguments: mcpFailureArguments(0.9, "skill_instruction"),
	})
	if err != nil || remembered.IsError {
		t.Fatalf("numeric confidence was rejected: result=%#v err=%v", remembered, err)
	}
	invalid, invalidErr := session.CallTool(ctx, &mcp.CallToolParams{
		Name: "remember_failure", Arguments: mcpFailureArguments(0.9, "invented_layer"),
	})
	if invalidErr == nil && (invalid == nil || !invalid.IsError) {
		t.Fatalf("schema accepted an unknown cause layer: result=%#v err=%v", invalid, invalidErr)
	}
	metrics, err := svc.Metrics(ctx)
	if err != nil {
		t.Fatal(err)
	}
	counts := metrics["counts"].(map[string]int64)
	if counts["captures"] != 1 || counts["lessons"] != 1 {
		t.Fatalf("schema rejection reached persistence: %#v", counts)
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
	cancel()
	<-serverError
}

func mcpFailureArguments(confidence float64, layer string) map[string]any {
	return map[string]any{
		"summary":        "The agent skipped an established compatibility preflight.",
		"classification": "real_failure",
		"expectation": map[string]any{
			"invariant": "Run the compatibility preflight before schema edits.",
			"source":    "loaded skill",
			"evidence":  "The invariant was available before implementation.",
		},
		"observed": map[string]any{
			"outcome": "The edit occurred without the preflight.",
			"impact":  "The migration could write incompatible rows.",
		},
		"cause": map[string]any{
			"layer":              layer,
			"failure_mode":       "ignored",
			"component":          "schema workflow",
			"evidence":           "No preflight result existed.",
			"recommended_change": "Gate edits on the preflight.",
			"verification":       "Replay with a passing preflight.",
			"confidence":         confidence,
		},
		"lesson": map[string]any{
			"rule":         "Run compatibility checks before persisted schema edits.",
			"prevention":   "Run the preflight first.",
			"verification": "Retain its passing result.",
		},
	}
}
