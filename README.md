# Failure Memory

[![CI](https://github.com/CongBao/failure-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/CongBao/failure-memory/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-3776AB.svg)](https://www.python.org/)
[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Failure Memory gives AI coding agents a shared, local memory of mistakes worth
remembering.

It exists because **not every correction is a failure**. A user may be adding a new
requirement, clarifying missing information, or changing a preference. Saving all of
those as “lessons” creates noisy memory and makes the agent less reliable.

Failure Memory records only durable lessons from real failures, recalls them before
similar work, and learns whether each recall was useful.

## Why use it?

Without a deliberate failure-memory system, agents tend to:

- repeat mistakes across sessions and tools;
- treat requirement changes as failures;
- save several copies of the same lesson;
- recall loosely related advice without measuring whether it helped.

Failure Memory adds two review gates:

1. **Failure qualification** — was there an established expectation, an observable
   mismatch, meaningful impact or recurrence risk, a controllable cause, and a durable
   prevention lesson?
2. **Generalization review** — should the incident reuse an existing lesson, support a
   broader version of one, or remain distinct?

Similarity search suggests at most three related lessons. It never merges or promotes a
lesson automatically.

## Supported agents

| Agent or harness | Support | Installation |
| --- | --- | --- |
| OpenAI Codex | Plugin, skills, hooks, and MCP tools | Public GitHub marketplace |
| Claude Code | Plugin, skills, hooks, and MCP tools | Public GitHub marketplace |
| GitHub Copilot CLI | Plugin, skills, hooks, and MCP tools | Public GitHub marketplace |
| Cursor | Plugin, skills, hooks, and MCP tools | Local plugin or team marketplace |
| Other MCP clients | Core MCP tools | Configure the bundled stdio server manually |

All supported harnesses use the same memory for the current OS user. Installing the
plugin in a second agent does not create a second database.

## Install

### Requirements

- Python 3.13 or newer; Python 3.14 is recommended
- Git

Check Python before installing:

```bash
python3 --version
```

### Codex

```bash
codex plugin marketplace add CongBao/failure-memory
codex plugin add failure-memory@failure-memory
```

Start a new Codex task after installation so the skills, hooks, and MCP server are
loaded.

### Claude Code

```bash
claude plugin marketplace add CongBao/failure-memory
claude plugin install failure-memory@failure-memory
```

Run `/reload-plugins` in an active Claude Code session, or start a new session.

### GitHub Copilot CLI

```bash
copilot plugin install CongBao/failure-memory
```

This direct GitHub installation uses the repository's Copilot plugin manifest. The
repository can also be added as a Copilot marketplace for browsing and managed updates.
Start a new Copilot CLI session after installation.

### Cursor

Failure Memory includes a Cursor plugin projection. Until it is listed in the public
Cursor Marketplace, install it as a local plugin:

```bash
git clone https://github.com/CongBao/failure-memory.git
mkdir -p ~/.cursor/plugins/local
ln -s "$PWD/failure-memory" ~/.cursor/plugins/local/failure-memory
```

Restart Cursor or run **Developer: Reload Window**. Teams and Enterprise users can also
import this repository from **Dashboard → Plugins → Add Marketplace**.

## How to use it

Failure Memory provides two agent skills:

- **Recall failure lessons** before risky, repeated, or failure-prone work.
- **Record an agent failure** after a real mismatch has been confirmed.

You can ask naturally; you do not need to call database tools yourself.

### 1. Recall lessons before work

Example prompts:

```text
Before changing this installer, recall any relevant failure lessons.
```

```text
Check Failure Memory for lessons about plugin identity and duplicate installs.
```

Recall is exact-first, then similarity-based. Results come from the global personal
store, even when another supported agent recorded the lesson.

### 2. Evaluate a possible failure

After something goes wrong, ask the agent to evaluate it:

```text
Evaluate whether this was a real failure. Do not record it if I only changed or
clarified the requirement.
```

The evaluation distinguishes:

- real failures;
- requirement updates;
- requirement clarifications;
- preference updates;
- mixed or uncertain feedback.

Rejected and deferred evaluations remain auditable, but they do not become incidents or
lessons.

### 3. Review and record the lesson

If the evaluation is accepted, ask the agent to complete the second review:

```text
Review this accepted failure against existing lessons. Reuse an exact match, propose a
carefully generalized lesson when justified, or keep it distinct.
```

Then explicitly approve recording:

```text
Record the reviewed failure and proposed prevention lesson.
```

The plugin stores immutable incidents and versioned lessons. Existing history is
preserved rather than overwritten.

### 4. Give feedback on recalled lessons

Recall feedback is important because it tells the memory which lessons actually help:

```text
That recalled lesson was useful and prevented the same failure. Record the outcome.
```

```text
That lesson was a false positive for this task. Record that outcome.
```

Failure Memory stores append-only recall attempts, candidates, selections, outcomes,
false positives, and missed-relevant feedback. It does not store raw recall prompts.

### 5. Check health and learning metrics

Example prompts:

```text
Check Failure Memory health and retrieval status.
```

```text
Show recall feedback coverage, usefulness, false-positive, and missed-relevant metrics.
```

## Search modes

Exact signature and SQLite FTS5 lexical search work without third-party dependencies.
Optional semantic search uses local `sqlite-vec` and FastEmbed, and hybrid search fuses
lexical and vector rankings.

Semantic dependencies and the embedding model are installed only after explicit
approval:

```bash
uv run failure-memory adapters plan
uv run failure-memory adapters install
uv run failure-memory index build
```

## Local data and privacy

Failure Memory is local-first. It does not require a hosted database or cloud account.
The default global data root is:

- macOS: `~/Library/Application Support/failure-memory`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/failure-memory`
- Windows: `%LOCALAPPDATA%\FailureMemory`

Back up the event-store database; indexes, models, and adapter environments can be
rebuilt. Recognized credentials and private keys are redacted, but do not submit secrets,
personal data, raw transcripts, or unnecessary private paths.

See [CONTRIBUTING.md](CONTRIBUTING.md) to contribute and [SECURITY.md](SECURITY.md) to
report a vulnerability.

## License

[MIT](LICENSE)
