<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="./assets/logo.svg">
    <img alt="Failure Memory logo" src="./assets/logo.svg" width="120">
  </picture>
</p>

<h1 align="center">Failure Memory</h1>

<p align="center"><strong>Remember verified failures. Recall the lesson before repeating them.</strong></p>

[![CI](https://github.com/CongBao/failure-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/CongBao/failure-memory/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/CongBao/failure-memory)](https://github.com/CongBao/failure-memory/releases/latest)
[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

AI coding agents receive valuable corrections, but those lessons usually disappear with
the session. Saving every correction creates the opposite problem: preferences, new
details, and changed requirements pollute memory as if they were mistakes.

Failure Memory gives agents one shared, local store of verified failures. It recalls
relevant lessons before similar work and tracks whether those lessons actually helped,
without treating every user correction as a failure.

## What it does

It can:

- distinguish real failures from requirement changes, clarifications, and preferences;
- record the root cause, recommended repair location, and durable prevention lesson;
- reuse an exact existing lesson instead of creating duplicates;
- surface related lessons for review without silently merging or deleting history;
- recall only lessons that clear a calibrated relevance threshold before similar work;
- keep false positives and superseded lessons in history while removing them from recall;
- retain recall, lifecycle, outcome, cost, and harness history for measurement and improvement.

## Supported agents

| Agent | Integration |
| --- | --- |
| OpenAI Codex | Plugin, skills, MCP tools, and prompt hooks |
| Claude Code | Plugin, skills, MCP tools, and prompt hooks |
| GitHub Copilot CLI and Chat | Plugin, skills, MCP tools, and prompt hooks |
| Cursor | Plugin, skills, MCP tools, and session hook |
| Other agents | The skills plus the local `failure-memory` command or MCP server |

All integrations use the same store for the current OS user. Installing Failure Memory
in another supported agent does not create another memory database.

## Install

One command installs the native runtime and the plugin for every supported agent it
detects. It is safe to run again to update an existing installation. Failure Memory does
not require Python, Node.js, or a database server.

macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/CongBao/failure-memory/main/scripts/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/CongBao/failure-memory/main/scripts/install.ps1 | iex
```

The installer downloads the release for your platform, verifies its checksum, installs
one shared `failure-memory` executable, adds the plugin through each detected agent's
native plugin manager, and registers the three fast MCP tools with the executable's
absolute path. Codex, Claude Code, and GitHub Copilot CLI are completed automatically.
If Cursor is detected, its MCP tools are configured automatically and the installer
prints the `/add-plugin` command needed to enable the skills and hook because Cursor does
not currently expose a stable non-interactive plugin installer.

To target selected agents on macOS or Linux, pass a comma-separated list:

```bash
curl -fsSL https://raw.githubusercontent.com/CongBao/failure-memory/main/scripts/install.sh \
  | sh -s -- --harness codex,copilot
```

Accepted names are `codex`, `claude`, `copilot`, `cursor`, and `auto`. Use
`--runtime-only` only when an administrator manages plugins separately.

For another agent, copy `skills/record-agent-failure` and
`skills/recall-failure-lessons` into its skills directory. Applications that accept an
MCP server command can register:

```text
failure-memory mcp --stdio
```

Restart any agent application that was already open, then verify the installation:

```bash
failure-memory install status
failure-memory doctor
```

## Use

Use Failure Memory through natural prompts. The skills perform one bounded operation and
do not interrupt ordinary work when no useful memory action is needed.

### Recall lessons before risky or recurring work

```text
Before changing this migration workflow, recall relevant failure lessons.
```

A short task description is enough. Add a component, expected invariant, suspected
cause, or prevention action only when it is already known. Failure Memory filters by a
retrieval-profile relevance threshold, collapses lessons from the same cluster, and then
applies `top_k` as a maximum. A healthy recall can therefore return zero, one, two, or
three lessons. It never lowers the threshold just to fill the result list.

Treat recalled lessons as cautions to validate against the current task, not as
instructions that override current requirements.

### Record a real failure

```text
The migration changed persisted data without the required compatibility check. Find the
root cause and remember a lesson that prevents this from recurring.
```

Failure Memory first checks whether the feedback describes a genuine failure:

1. Did an expectation or invariant exist before the outcome?
2. Is there an inspectable mismatch and meaningful impact or recurrence risk?
3. Is there an evidenced, controllable cause?
4. Can a concrete prevention and verification step be stated?

If the evidence is insufficient, the case is stored only as a qualification attempt and
does not become a lesson. This makes false-positive rates measurable without polluting
future recall.

The normal path uses one tool call. Cause taxonomies are published in the tool schema,
and optional confidence accepts both `low`/`medium`/`high` strings and numeric `0..1`
values. If a deterministic input-validation response explicitly marks the request as
retryable, the skill may correct only the named fields once. Timeouts and ambiguous
transport failures are never retried.

If feedback mixes an old failure with a new requirement, only the old-invariant mismatch
can enter memory. The new requirement remains normal work.

### Match against existing lessons

Before adding a lesson, Failure Memory checks the existing store:

- an exact match reuses the existing lesson;
- a related case remains separate and may produce a generalization proposal for review;
- history is not silently merged or deleted.

When reviewed related lessons express one durable rule, `cluster review` can create a
new generalized parent lesson. The original lessons remain in the append-only history
and point to the parent instead of competing with it in recall.

### Correct or evaluate memory

Verified outcomes make memory quality measurable. For example, mark a lesson that was
actually a requirement change as a false positive:

```bash
printf '%s' '{"target_type":"lesson","target_id":"lessonv_...","outcome":"false_positive","evidence_code":"confirmed_requirement_change"}' \
  | failure-memory outcome
```

You can also report outcomes for a `recall` attempt or a `repair` recommendation by
using the identifier returned by the original operation. Repeating the same outcome is
idempotent. Lesson corrections never delete the original evidence; false-positive,
stale, and superseded lessons are retained but excluded from normal recall.

### Run a recall from the command line

```bash
printf '%s' '{"text":"schema migration","component":"migration workflow"}' \
  | failure-memory recall
```

Normally omit `mode`, `top_k`, and `min_relevance`. For a stricter bounded lookup,
set `min_relevance` above zero; zero and omission select the profile default. The
threshold is always applied before `top_k`:

```bash
printf '%s' '{"text":"schema migration","top_k":2,"min_relevance":0.9}' \
  | failure-memory recall
```

The command reads one JSON object from standard input and returns one JSON object. Run
`failure-memory` without arguments to see all available commands.

## Search

Failure Memory supports:

- exact lookup;
- SQLite FTS5 full-text search, including CJK bigrams;
- local sqlite-vec vector search;
- hybrid ranking across exact, full-text, and vector results.

Every search mode returns a calibrated `relevance_score`. Exact matches score `1`;
semantic and fallback profiles use separately calibrated default thresholds. An empty
result means no candidate met the threshold, not that the search failed.

Exact and full-text recall work immediately. A deterministic local vector index supports
hybrid ranking without a model download. To enable model-based multilingual semantic
search, install the pinned model once and rebuild the derived index:

```bash
failure-memory adapters install
failure-memory index build
```

Models are never downloaded during a record or recall operation.

## Maintenance

Useful commands:

```bash
failure-memory doctor
failure-memory metrics
failure-memory store-status
failure-memory backup create
failure-memory backup verify <backup-directory>
failure-memory cluster propose
failure-memory outcome
```

`failure-memory metrics` reports recall abstention and filtering rates, latency
percentiles, input/output size, outcome coverage, lesson lifecycle counts,
generalization backlog, and usage by agent harness. Clustering and outcome commands
append proposals or observations. They do not delete or silently merge history.

To restore a backup, first stop every agent application using Failure Memory, then run:

```bash
failure-memory backup restore <backup-directory> --replace
```

Restore verifies the backup before replacing the store, creates a safety backup of the
current store, and rebuilds the derived retrieval index. Interrupted restores are
recovered automatically the next time Failure Memory opens the store.

## Local data and privacy

The append-only event store, retrieval index, optional model, and installation receipt
live in one owner-private directory:

- macOS: `~/Library/Application Support/failure-memory`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/failure-memory`
- Windows: `%LOCALAPPDATA%\FailureMemory`

The event store is authoritative and append-only. Retrieval indexes are disposable and
reconciled from it automatically, so backups contain only the verified event store and a
checksum manifest. Submit only compact evidence. Do not store raw prompts, full
transcripts, credentials, or unnecessary personal information.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Report security issues according to
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
