# Failure Memory

[![CI](https://github.com/CongBao/failure-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/CongBao/failure-memory/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-3776AB.svg)](https://www.python.org/)
[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Failure Memory gives AI coding agents one shared, local memory of mistakes worth
remembering.

It exists because not every correction is a failure. A user may be adding a requirement,
supplying a previously unavailable detail, or stating a preference for the first time.
Saving every correction as a lesson creates noisy memory and makes future agents less
reliable.

Failure Memory separates those cases from genuine failures, records an evidence-based
root cause and proposed prevention lesson, finds related lessons, and recalls them across
agent applications.

## Why it is fast

The normal agent surface contains only two tools:

- `remember_failure` qualifies, diagnoses, deduplicates, records, and emits telemetry in
  one call.
- `recall_failure_lessons` performs one bounded exact, lexical, semantic, or hybrid
  lookup.

The agent does not coordinate a multi-step database workflow. If its harness does not
expose MCP tools, each bundled skill points directly to a one-command CLI fallback. The
agent must not search plugin files, inspect SQLite, discover runtimes, call private APIs,
or create temporary payload files.

Exact and FTS5 matching remain available when the optional vector adapter is not ready.
Recording never waits for dependency installation or model download.

## Supported agents

All projections use the same memory store for the current OS user.

| Agent or harness | Integration |
| --- | --- |
| OpenAI Codex | Plugin, skills, hooks, MCP |
| Claude Code | Plugin, skills, hooks, MCP |
| GitHub Copilot CLI | Plugin, skills, session hook, MCP |
| GitHub Copilot Chat in VS Code | User skills and VS Code MCP configuration |
| Cursor | Plugin, skills, session hook, MCP |
| Other agents | Generic skill projection, optional MCP configuration, CLI fallback |

## Requirements

- Python 3.13 or newer; Python 3.14 is recommended
- Git

```bash
python3 --version
```

## Install

### Codex

```bash
codex plugin marketplace add CongBao/failure-memory
codex plugin add failure-memory@failure-memory
```

Start a new Codex task after installation.

### Claude Code

```bash
claude plugin marketplace add CongBao/failure-memory
claude plugin install failure-memory@failure-memory
```

Run `/reload-plugins` or start a new session.

### GitHub Copilot CLI

```bash
copilot plugin install CongBao/failure-memory
```

Start a new Copilot CLI session.

### GitHub Copilot Chat in VS Code

```bash
git clone https://github.com/CongBao/failure-memory.git
cd failure-memory
python3 scripts/install_harness.py --target copilot-vscode --apply
```

Restart VS Code or run **Developer: Reload Window**. The installer preserves existing
servers in the VS Code user `mcp.json`, adds the managed Failure Memory server, and
projects both skills under `~/.copilot/skills`.

### Cursor

```bash
git clone https://github.com/CongBao/failure-memory.git
cd failure-memory
python3 scripts/install_harness.py --target cursor --apply
```

Restart Cursor or run **Developer: Reload Window**.

### Another agent

Install the two skills into the agent's skill root:

```bash
python3 scripts/install_harness.py \
  --target generic \
  --skills-dir /path/to/agent/skills \
  --agent-name my-agent \
  --apply
```

If the agent supports MCP JSON configuration, add its file:

```bash
python3 scripts/install_harness.py \
  --target generic \
  --skills-dir /path/to/agent/skills \
  --mcp-config /path/to/mcp.json \
  --agent-name my-agent \
  --apply
```

Without `--mcp-config`, the installed skills use their adjacent CLI fallback. A harness
must support either MCP tool calls or shell execution to access external memory.

### Install several local projections safely

Preview:

```bash
python3 scripts/install_harness.py \
  --target codex \
  --target claude-code \
  --target copilot-cli \
  --target copilot-vscode \
  --target cursor
```

Apply:

```bash
python3 scripts/install_harness.py \
  --target codex \
  --target claude-code \
  --target copilot-cli \
  --target copilot-vscode \
  --target cursor \
  --apply
```

The installer reports `install`, `update`, `noop`, or `conflict`. It does not overwrite
an unmanaged skill or MCP entry, and repeated targets do not create duplicate
installations.

## Use

You can speak naturally. The agent loads the appropriate skill.

### Recall lessons before risky work

```text
Before changing this installer, recall any relevant failure lessons.
```

The skill makes one bounded recall call and applies at most three returned lessons as
proposed cautions.

### Record a possible failure

```text
Check whether this was a real failure and remember it if warranted. Find the root cause
and recommend where to fix it. Do not treat my new requirements as failures.
```

The skill reconstructs only the minimum evidence and calls `remember_failure` once.
Internally the service:

1. distinguishes a real failure from a requirement update, clarification, preference,
   mixed case, or uncertain case;
2. stores rejected false-positive capture attempts for measurement without creating a
   lesson;
3. records one evidence-bounded cause and proposed repair;
4. reuses an exact lesson or records related work for later generalization;
5. appends the incident, proposed lesson, and operation latency telemetry.

For mixed feedback, only the prior-invariant mismatch enters memory. New work stays in
the normal requirement workflow.

### CLI fallback

The same one-call contract is available directly:

```bash
python3 scripts/failure_memory_cli.py remember --stdin
```

Pass one JSON object on standard input. The launcher automatically selects the validated
private adapter runtime when one is installed.

## Search

Failure Memory supports:

- exact signature lookup;
- SQL/FTS5 lexical lookup;
- local sqlite-vec semantic lookup;
- application-level hybrid ranking;
- reviewed cluster recall.

Optional vector dependencies and the embedding model live under the global adapter
directory. Install them explicitly outside an agent recording call:

```bash
python3 scripts/failure_memory_cli.py adapters plan
python3 scripts/failure_memory_cli.py adapters install
python3 scripts/failure_memory_cli.py index build
```

If semantic search is unavailable, recall degrades to lexical search and recording marks
semantic reconciliation as pending.

## Administration

Administrative operations are intentionally not advertised to every LLM. Use the CLI:

```bash
python3 scripts/failure_memory_cli.py doctor
python3 scripts/failure_memory_cli.py recording-metrics
python3 scripts/failure_memory_cli.py recall-metrics
python3 scripts/failure_memory_cli.py learning-metrics
```

## Local data and privacy

The append-only event store, derived indexes, embedding model, and adapter runtime are
kept under one owner-private data root:

- macOS: `~/Library/Application Support/failure-memory`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/failure-memory`
- Windows: `%LOCALAPPDATA%\FailureMemory`

Back up the event-store database. Derived indexes and adapter environments can be
rebuilt. Failure Memory redacts recognized credentials, but agents should not submit raw
prompts, secrets, personal data, full transcripts, or unnecessary private paths.

See [CONTRIBUTING.md](CONTRIBUTING.md) to contribute and [SECURITY.md](SECURITY.md) to
report a vulnerability.

## License

[MIT](LICENSE)
