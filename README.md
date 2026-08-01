# Failure Memory

[![CI](https://github.com/CongBao/failure-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/CongBao/failure-memory/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/CongBao/failure-memory)](https://github.com/CongBao/failure-memory/releases/latest)
[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Failure Memory gives AI coding agents a shared, local memory of verified failures.
It helps agents avoid repeating a mistake without treating every correction, preference,
or newly supplied requirement as a failure.

It can:

- distinguish real failures from requirement changes, clarifications, and preferences;
- record the root cause, recommended repair location, and durable prevention lesson;
- reuse an exact existing lesson instead of creating duplicates;
- surface related lessons for review without silently merging or deleting history;
- recall up to three relevant lessons before similar work;
- retain recall and outcome history for metrics and future improvement.

## Supported agents

| Agent | Integration |
| --- | --- |
| OpenAI Codex | Plugin, record/recall skills, and prompt hooks |
| Claude Code | Plugin, record/recall skills, and prompt hooks |
| GitHub Copilot CLI | Plugin, record/recall skills, and prompt hooks |
| GitHub Copilot Chat in VS Code | Record/recall skills |
| Cursor | Plugin, record/recall skills, and session hook |
| Other agents | The two skills plus the local `failure-memory` command |

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
one shared `failure-memory` executable, and uses each detected agent's native plugin
manager. Codex, Claude Code, and GitHub Copilot CLI are completed automatically. If
Cursor is detected, the installer prints the `/add-plugin` command to run because Cursor
does not currently expose a stable non-interactive plugin installer.

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

A recall needs a short task description plus something concrete, such as the component,
expected invariant, suspected cause, or prevention action. It returns at most three
lessons. Treat recalled lessons as cautions to validate against the current task, not as
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

If feedback mixes an old failure with a new requirement, only the old-invariant mismatch
can enter memory. The new requirement remains normal work.

### Match against existing lessons

Before adding a lesson, Failure Memory checks the existing store:

- an exact match reuses the existing lesson;
- a related case remains separate and may produce a generalization proposal for review;
- history is not silently merged or deleted.

### Run a recall from the command line

```bash
printf '%s' '{"text":"schema migration","component":"migration workflow"}' \
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
failure-memory cluster propose
failure-memory feedback recall
failure-memory feedback repair
```

Clustering and feedback commands append proposals or observations. They do not delete,
silently merge, or automatically promote lessons.

## Local data and privacy

The append-only event store, retrieval index, optional model, and installation receipt
live in one owner-private directory:

- macOS: `~/Library/Application Support/failure-memory`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/failure-memory`
- Windows: `%LOCALAPPDATA%\FailureMemory`

Back up the event-store database; derived indexes can be rebuilt. Submit only compact
evidence. Do not store raw prompts, full transcripts, credentials, or unnecessary
personal information.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Report security issues according to
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
