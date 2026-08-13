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
			Instructions: "Use remember_failure only for evidence-backed qualification. Use recall_failure_lessons once with compact task evidence and accept an empty result. Use report_memory_outcome only when an outcome is already supported by verification or user feedback. Never submit raw prompts, secrets, or unnecessary user text.",
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
			Description: "Return zero to three lessons after calibrated relevance filtering and cluster collapse; top_k is only a maximum. Append a privacy-preserving performance trace.",
			InputSchema: recallInputSchema(),
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
	mcp.AddTool(
		server,
		&mcp.Tool{
			Name:        "report_memory_outcome",
			Title:       "Report a memory outcome",
			Description: "Append one idempotent, evidence-bounded outcome for a recall, repair recommendation, or lesson without deleting audit history.",
			InputSchema: memoryOutcomeInputSchema(),
			Annotations: &mcp.ToolAnnotations{
				Title:           "Report a memory outcome",
				ReadOnlyHint:    false,
				DestructiveHint: &closed,
				IdempotentHint:  true,
				OpenWorldHint:   &openWorld,
			},
		},
		func(ctx context.Context, _ *mcp.CallToolRequest, input model.MemoryOutcomeInput) (*mcp.CallToolResult, model.OutcomeResult, error) {
			output, err := svc.ReportOutcome(ctx, input)
			return nil, output, err
		},
	)
	return server
}

func recallInputSchema() *jsonschema.Schema {
	schema, err := jsonschema.For[model.RecallInput](nil)
	if err != nil {
		panic(err)
	}
	schema.Properties["top_k"].Minimum = jsonschema.Ptr(1.0)
	schema.Properties["top_k"].Maximum = jsonschema.Ptr(3.0)
	schema.Properties["min_relevance"].Minimum = jsonschema.Ptr(0.0)
	schema.Properties["min_relevance"].Maximum = jsonschema.Ptr(1.0)
	schema.Properties["mode"].Enum = []any{"auto", "exact", "lexical", "semantic", "hybrid"}
	return schema
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

func memoryOutcomeInputSchema() *jsonschema.Schema {
	stringEnum := func(values []string) *jsonschema.Schema {
		items := make([]any, len(values))
		for index, value := range values {
			items[index] = value
		}
		return &jsonschema.Schema{Type: "string", Enum: items}
	}
	options := &jsonschema.ForOptions{TypeSchemas: map[reflect.Type]*jsonschema.Schema{
		reflect.TypeFor[model.MemoryTargetType](): stringEnum(model.MemoryTargetTypeValues()),
		reflect.TypeFor[model.MemoryOutcome]():    stringEnum(model.MemoryOutcomeValues()),
	}}
	schema, err := jsonschema.For[model.MemoryOutcomeInput](options)
	if err != nil {
		panic(err)
	}
	schema.Properties["confidence"].Minimum = jsonschema.Ptr(0.0)
	schema.Properties["confidence"].Maximum = jsonschema.Ptr(1.0)
	return schema
}
