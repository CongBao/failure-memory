package mcpserver

import (
	"context"
	"reflect"

	"github.com/google/jsonschema-go/jsonschema"
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
			Instructions: "Use remember_failure only for evidence-backed qualification. Normally call it once; correct once only after retryable=true or explicit pre-execution schema validation, never after an ambiguous failure. Use recall_failure_lessons once with bounded task evidence. Never submit raw prompts, secrets, or unnecessary user text.",
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
			Description: "Qualify one correction and append a real failure, root cause, repair, deduplication result, lesson, and telemetry only when evidence warrants it. A deterministic validation mismatch may return one machine-guided correction.",
			InputSchema: rememberInputSchema(),
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

func rememberInputSchema() *jsonschema.Schema {
	stringEnum := func(values []string) *jsonschema.Schema {
		items := make([]any, len(values))
		for index, value := range values {
			items[index] = value
		}
		return &jsonschema.Schema{Type: "string", Enum: items}
	}
	options := &jsonschema.ForOptions{TypeSchemas: map[reflect.Type]*jsonschema.Schema{
		reflect.TypeFor[model.Classification](): stringEnum(model.ClassificationValues()),
		reflect.TypeFor[model.CauseLayer]():     stringEnum(model.CauseLayerValues()),
		reflect.TypeFor[model.FailureMode]():    stringEnum(model.FailureModeValues()),
		reflect.TypeFor[model.Confidence](): {
			AnyOf: []*jsonschema.Schema{
				stringEnum(model.ConfidenceValues()),
				{
					Type:    "number",
					Minimum: jsonschema.Ptr(0.0),
					Maximum: jsonschema.Ptr(1.0),
				},
			},
		},
	}}
	schema, err := jsonschema.For[model.RememberInput](options)
	if err != nil {
		panic(err)
	}
	return schema
}
