# Failure Memory

[![CI](https://github.com/CongBao/failure-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/CongBao/failure-memory/actions/workflows/ci.yml)
[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

AI agents are often told to “learn from this,” but most implementations only collect
postmortems. They do not reliably distinguish a real mistake from a new requirement,
find an existing lesson, or bring that lesson back when it matters.

Failure Memory provides one private, local memory shared by your coding agents. It:

- rejects requirement changes, new details, and first-time preferences as failures;
- records an evidenced root cause, repair location, and prevention lesson in one call;
- reuses exact lessons and proposes review for related lessons instead of silently
  duplicating or merging them;
- recalls up to three relevant lessons with exact, full-text, vector, or hybrid search;
- keeps append-only recall and outcome history for metrics and future improvement.

## Supported agents

| Agent | Integration |
| --- | --- |
| OpenAI Codex | Plugin, skills, hooks, optional MCP |
| Claude Code | Plugin, skills, hooks, optional MCP |
| GitHub Copilot CLI | Plugin, skills, hooks, optional MCP |
| GitHub Copilot Chat in VS Code | Skills and MCP configuration |
| Cursor | Plugin, skills, session hook, optional MCP |
| Other agents | Skills plus CLI; MCP is optional |

Every integration uses the same OS-user store. Installing another plugin does not create
another memory database.

## Install

Failure Memory is a native executable. Python, Node.js, and a local database server are
not required.

### 1. Install the native runtime

macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/CongBao/failure-memory/main/scripts/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/CongBao/failure-memory/main/scripts/install.ps1 | iex
```

The installer verifies the release checksum and places one executable in the user's
local binary directory.

### 2. Install the plugin

Codex:

```bash
codex plugin marketplace add CongBao/failure-memory
codex plugin add failure-memory@failure-memory
```

Claude Code:

```bash
claude plugin marketplace add CongBao/failure-memory
claude plugin install failure-memory@failure-memory
```

GitHub Copilot CLI:

```bash
copilot plugin install CongBao/failure-memory
```

Cursor:

```text
/add-plugin CongBao/failure-memory
```

For another CLI-capable agent, copy `skills/record-agent-failure` and
`skills/recall-failure-lessons` into its skill directory. The skills call the global
`failure-memory` command directly when MCP is unavailable. If the agent accepts MCP
configuration, reuse [`.mcp.json`](.mcp.json); this improves tool discovery but does not
change the stored data or behavior.

Restart an already-open agent after installation. Run this to verify the shared runtime
and detect existing plugin identities:

```bash
failure-memory install status
```

## Use

The plugin exposes two deliberately small agent operations.

### Recall before risky work

Ask naturally:

```text
Before changing this installer, recall relevant failure lessons.
```

The recall skill makes one bounded lookup and treats returned lessons as proposed
cautions, not as authority.

### Record a failure

Ask naturally:

```text
Check whether this was a real failure. If it was, find the root cause, say where it
should be fixed, and remember the durable lesson. Do not treat my new requirements as
failures.
```

The recording skill makes one call. The service then:

1. classifies the feedback as a requirement update, clarification, preference, real
   failure, mixed case, or uncertain case;
2. stores the classification attempt for false-positive measurement;
3. creates a lesson only when a prior invariant, mismatch, impact or recurrence risk,
   controllable cause, and prevention are evidenced;
4. identifies the responsible layer, such as a skill, agent/project/system instruction,
   hook, tool contract, runtime adapter, schema, test gap, or implementation;
5. reuses an exact lesson or queues related lessons for non-destructive generalization
   review.

For mixed feedback, only the old-invariant failure enters memory. New work remains a
normal requirement.

### CLI fallback

Any agent that can run a local command can use Failure Memory without MCP:

```bash
printf '%s' '{"text":"schema migration","component":"migration workflow"}' \
  | failure-memory recall
```

`remember` and `recall` each accept one JSON object on standard input and return one JSON
object. Skills are instructed never to search plugin files, inspect SQLite, create
temporary payloads, or retry multiple fallbacks.

## Why both CLI and MCP?

The CLI is the universal interface and the administration surface. MCP is an optional,
thin adapter for agents that support it:

- the agent discovers the two operations automatically;
- JSON Schema validates arguments before execution;
- no shell quoting or command construction is needed;
- a persistent MCP process can keep the optional semantic model warm.

Both call the same in-process service. There is no separate MCP database or behavior.

## Search and adapters

The default local backend combines:

- exact keys and SQL lookup;
- SQLite FTS5 full-text search, including CJK bigrams;
- sqlite-vec cosine vector search;
- application-level hybrid ranking.

sqlite-vec supplies the vector operator; exact and SQL/FTS search come from SQLite, and
Failure Memory fuses their rankings for hybrid search.

The built-in vector fallback needs no model download and is not advertised as semantic
search. To add multilingual semantic search, explicitly install the pinned adapter:

```bash
failure-memory adapters install
failure-memory index build
```

The model and every database/index implementation live below `adapters/`. No adapter is
downloaded during an agent call. The retrieval port is implementation-neutral so another
backend, such as Milvus, can be added without changing skills, MCP, CLI, or the event
store. SQLite is the default because a personal local memory needs no daemon, stays in
one backup-friendly directory, and supports exact, full-text, and vector lookup in one
process. Milvus becomes attractive for a large shared or distributed deployment, not for
the default single-user plugin.

## Administration

Administrative commands are not exposed to the normal LLM tool list:

```bash
failure-memory doctor
failure-memory metrics
failure-memory store-status
failure-memory cluster propose
failure-memory feedback recall
failure-memory feedback repair
```

Clustering and feedback only append proposals or observations. They never delete,
silently merge, or automatically promote a lesson.

## Local data and privacy

The append-only event store, derived retrieval index, optional model, and installation
receipt live under one owner-private directory:

- macOS: `~/Library/Application Support/failure-memory`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/failure-memory`
- Windows: `%LOCALAPPDATA%\FailureMemory`

Back up the event-store database; derived indexes can be rebuilt. Agents should submit
compact evidence, never raw prompts, full transcripts, credentials, or unnecessary
personal data.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues should follow
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
