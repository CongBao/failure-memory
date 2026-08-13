# Contributing

Contributions are welcome. Failure Memory is local-first, append-oriented, and designed
to keep the normal agent path small.

## Development

Requirements:

- Go 1.26 or newer;
- Git.

```bash
go test ./...
go vet ./...
test -z "$(gofmt -l .)"
go build ./cmd/failure-memory
```

## Design rules

- Keep qualification, causal diagnosis, deduplication, and persistence behind the
  one-call recording boundary.
- Keep CLI and MCP as thin adapters over the same application service.
- Put event stores, retrieval databases, and embedding/model implementations under
  adapters and implement the relevant port.
- Never download dependencies or models during `remember` or `recall`.
- Treat incidents and telemetry as append-only. Generalization is proposal-and-review,
  never an automatic merge or deletion.
- Keep causes evidence-bounded. Use `unknown` instead of assigning unsupported blame.
- Never commit runtime databases, downloaded models, credentials, raw prompts, personal
  data, absolute contributor paths, or generated release binaries.
- Do not add `docs/`, `plans/`, or `specs/` to Git.

Validate plugin JSON and both skills when changing their contracts. Keep the public MCP
surface limited to `remember_failure`, `recall_failure_lessons`, and
`report_memory_outcome`; administration belongs in the CLI.

## Pull requests

Describe the user-visible change, safety/privacy implications, tests, migration impact,
and any intentionally unsupported behavior.
