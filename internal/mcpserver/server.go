package mcpserver

import (
	"context"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/CongBao/failure-memory/internal/model"
	"github.com/CongBao/failure-memory/internal/service"
	"github.com/CongBao/failure-memory/internal/version"
)

func Run(ctx context.Context, svc *service.Service) error {
	return New(svc).Run(ctx, &mcp.StdioTransport{})
}

func New(svc *service.Service) *mcp.Server {
	server := mcp.NewServer(
		&mcp.Implementation{
			Name:        "failure-memory",
			Title:       "Failure Memory",
			Description: "Local, append-only failure lessons shared across agent harnesses.",
			Version:     version.Version,
			WebsiteURL:  "https://github.com/CongBao/failure-memory",
		},
		&mcp.ServerOptions{
			Instructions: "Use remember_failure only for evidence-backed qualification, including non-failure classifications. Use recall_failure_lessons once with bounded task evidence. Never submit raw prompts, secrets, or unnecessary user text.",
			Capabilities: &mcp.ServerCapabilities{},
		},
	)
	closed := false
	openWorld := false
	mcp.AddTool(
		server,
		&mcp.Tool{
			Name:        "remember_failure",
			Title:       "Remember a failure",
			Description: "Qualify one correction and append a real failure, root cause, repair, deduplication result, lesson, and telemetry only when evidence warrants it.",
			Annotations: &mcp.ToolAnnotations{
				Title:           "Remember a failure",
				ReadOnlyHint:    false,
				DestructiveHint: &closed,
				IdempotentHint:  false,
				OpenWorldHint:   &openWorld,
			},
		},
		func(ctx context.Context, _ *mcp.CallToolRequest, input model.RememberInput) (*mcp.CallToolResult, model.RememberResult, error) {
			output, err := svc.Remember(ctx, input)
			return nil, output, err
		},
	)
	mcp.AddTool(
		server,
		&mcp.Tool{
			Name:        "recall_failure_lessons",
			Title:       "Recall failure lessons",
			Description: "Recall at most three relevant local lessons with one bounded exact, lexical, vector, semantic, or hybrid search and append a privacy-preserving trace.",
			Annotations: &mcp.ToolAnnotations{
				Title:           "Recall failure lessons",
				ReadOnlyHint:    false,
				DestructiveHint: &closed,
				IdempotentHint:  false,
				OpenWorldHint:   &openWorld,
			},
		},
		func(ctx context.Context, _ *mcp.CallToolRequest, input model.RecallInput) (*mcp.CallToolResult, model.RecallResult, error) {
			output, err := svc.Recall(ctx, input)
			return nil, output, err
		},
	)
	return server
}
