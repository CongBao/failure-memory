package mcpserver

import (
	"context"
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
	for _, tool := range tools.Tools {
		names[tool.Name] = true
		if tool.InputSchema == nil || tool.OutputSchema == nil {
			t.Fatalf("tool %s lacks a typed schema", tool.Name)
		}
	}
	if !names["remember_failure"] || !names["recall_failure_lessons"] {
		t.Fatalf("unexpected tools: %#v", names)
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
	cancel()
	<-serverError
}
