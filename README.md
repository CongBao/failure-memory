# Failure Memory

Failure Memory helps AI agents distinguish real, reusable failures from requirement
changes, store durable lessons locally, and recall those lessons before similar work.

The project is local-first and designed around adapter boundaries so storage, retrieval,
embeddings, and agent-harness integrations can evolve independently. One owner-private
global store is shared by trusted local harnesses; harness, workspace, and session values
are retained as origin metadata rather than used as retrieval boundaries. The current
release includes a dependency-free SQLite event store, exact and FTS5 recall, optional
local sqlite-vec/FastEmbed semantic search, append-only learning telemetry, a JSON CLI,
an MCP server, and a Codex plugin with separate recording and recall skills.

## Why this exists

Agent feedback is not automatically a failure. A user may be adding a requirement,
clarifying previously unavailable information, or stating a preference for the first
time. Recording every correction creates noisy memory and makes future recall less
trustworthy.

Failure Memory applies two gates:

1. **Qualification:** Was there a prior expectation, an observable mismatch, material
   impact or recurrence risk, a controllable cause, and a durable prevention lesson?
2. **Reuse:** Does the accepted incident match an existing lesson's exact invariant,
   cause, and prevention signature?

Rejected and deferred evaluations remain auditable capture attempts, but they do not
become incidents or lessons.

## Current capabilities

- Classifies `requirement_update`, `requirement_clarification`, `preference_update`,
  `real_failure`, `mixed`, and `uncertain` feedback.
- Requires evaluation before any incident write.
- Stores immutable incidents and versioned proposed lessons in SQLite.
- Reuses a lesson only when its normalized invariant, cause, and prevention action match
  exactly.
- Redacts recognized credential, bearer-token, and private-key patterns before storage.
- Performs global FTS5 lexical recall and optional exact cosine KNN with sqlite-vec.
- Fuses lexical and vector rankings with reciprocal-rank fusion while preserving exact
  signature precedence.
- Stores privacy-preserving recall attempts, candidates, selections, and outcome feedback
  in append-only tables; raw recall prompts are not stored.
- Supports structured `false_positive`, usefulness, staleness, contradiction, and
  recurrence-prevention outcomes, plus `missed_relevant` feedback for false negatives.
- Reports feedback coverage, precision at one and three, usefulness, false-positive,
  missed-relevant, exact-reuse, and per-harness metrics.
- Appends reviewed lesson lifecycle transitions; historical versions remain immutable.
- Runs feedback ranking only as recorded shadow experiments and creates semantic
  generalization proposals without activating ranking changes or merging lessons.
- Plans, verifies, and performs idempotent copy-only imports from earlier local stores.
- Exposes sixteen schema-validated MCP tools with accurate read/write annotations.
- Provides separate Codex skills for recording failures and recalling lessons.
- Keeps runtime state outside the source checkout and plugin installation.

Automatic hooks, production feedback ranking, and additional vector-store and
agent-harness packages are tracked in the [roadmap](docs/roadmap.md). Similarity never
merges or reuses lessons automatically.

## Requirements

- Python 3.13 or newer
- Git, when building a commit-stamped Codex bundle
- [`uv`](https://docs.astral.sh/uv/) for the recommended development workflow

Python 3.14 is the primary development version. Exact and lexical recall have no
third-party Python dependencies. Semantic recall uses an explicitly installed private
adapter runtime; plugin installation itself never downloads native packages or models.

## Quick start: command line

Create the environment and install the project:

```bash
uv sync --extra dev
uv run failure-memory setup-status
uv run failure-memory doctor
uv run failure-memory metrics
uv run failure-memory recall-metrics
uv run failure-memory learning-metrics
uv run failure-memory store status
uv run failure-memory index status
```

Commands emit one JSON object on standard output. Diagnostics use standard error.
`evaluate`, `record`, `recall`, and `feedback` accept one JSON object from a file or
standard input:

```bash
uv run failure-memory evaluate --input candidate.json
uv run failure-memory evaluate --input - < candidate.json

uv run failure-memory record --input accepted-incident.json
uv run failure-memory record --input - < accepted-incident.json

uv run failure-memory recall --input recall-query.json
uv run failure-memory feedback --input recall-outcome.json
```

An accepted evaluation returns a `capture_attempt_id`. Pass that identifier to `record`;
rejected or deferred captures cannot be recorded.

## Retrieval profiles

Every recall is exact-first and global to the current OS user's Failure Memory.
`auto` checks a complete invariant, cause, and prevention signature before bounded
similarity retrieval. Similarity calls require task context plus at least one explicit
discriminator such as an invariant, cause, prevention action, or component. The default
result limit is three and the hard limit is five. Origin metadata remains available for
audit and per-harness metrics, but it does not hide lessons created by another harness.

The built-in profile uses SQLite FTS5. To add local semantic search, inspect the explicit
plan and install the pinned adapter:

```bash
uv run failure-memory adapters list
uv run failure-memory adapters plan
uv run failure-memory adapters install
uv run failure-memory index build
```

The install command creates a private Python environment, installs
`sqlite-vec==0.1.9`, `fastembed==0.8.0`, and `truststore==0.10.4`, downloads the
configured embedding model into the adapter data tree using the native OS trust store,
and validates the packages before writing a readiness marker. Setup disables Hugging Face
telemetry and its optional Xet transfer path.
Until that explicit command succeeds, semantic-only recall returns `setup_required` and
hybrid recall degrades to FTS5. Nothing is installed or downloaded by an ordinary recall
call.

sqlite-vec provides exact brute-force KNN for semantic retrieval; SQL equality and joins
remain regular SQLite operations, FTS5 provides lexical search, and Failure Memory fuses
the two result lists in the application layer. The retrieval port is backend-neutral so
Milvus, Qdrant, or another vector database can be added without changing the failure
qualification model.

## Consolidate earlier stores

Version 0.3 uses one platform-global store. Earlier harness-local databases are never
modified or deleted during consolidation:

```bash
uv run failure-memory store discover
uv run failure-memory store plan --source /absolute/path/to/failure-memory.sqlite3
uv run failure-memory store import --source /absolute/path/to/failure-memory.sqlite3
uv run failure-memory store verify --source /absolute/path/to/failure-memory.sqlite3
```

The importer opens the source read-only, fingerprints logical content, checks immutable
identifier collisions before writing, copies the event graph in one transaction, and
records an append-only import receipt. Repeating the same import is a no-op.

Learning operations remain conservative:

```bash
uv run failure-memory lesson transition \
  --lesson-id LESSON_ID --to-state verified --rationale-code reviewed
uv run failure-memory learning experiment
uv run failure-memory learning cluster --distance-threshold 0.2
```

Lifecycle transitions require review. Ranking experiments are shadow-only. Clustering
requires the explicitly installed semantic adapter and produces proposals only.

## Install the Codex plugin from source

Build the installable bundle from a reviewed Git checkout:

```bash
uv sync --extra dev
uv run python packaging/build_codex.py
```

The bundle is written to `packaging/out/failure-memory`. If the Codex plugin validator is
available in your installation, validate the bundle before installing it:

```bash
CODEX_SKILL_ROOT="${CODEX_HOME:-$HOME/.codex}/skills"
python3 "$CODEX_SKILL_ROOT/.system/plugin-creator/scripts/validate_plugin.py" \
  packaging/out/failure-memory
```

Use Codex's built-in `$plugin-creator` workflow to add the built bundle to a local or
personal marketplace, then install it from that marketplace. For the default personal
marketplace, the install command is:

```bash
codex plugin add failure-memory@personal
```

Start a new Codex task after installation so the host reloads the plugin's skills and MCP
server. See the official
[plugin packaging documentation](https://developers.openai.com/plugins/build/plugins)
for marketplace layouts and current installation options.

The shared MCP/CLI integration contract and rules for future Claude, Cursor, Copilot, and
other projections are documented in [harness integrations](docs/harnesses.md).

## Distribution status

This repository supports source distribution and local Codex marketplace installation.
It is not yet submitted to the universal public Plugins Directory.

The bundled MCP server uses local stdio transport and local SQLite state. A universal
directory submission that includes MCP would require a production, publicly reachable
HTTPS MCP endpoint and the other materials described by the official
[plugin submission guide](https://developers.openai.com/plugins/deploy/submission).
A skills-only public submission is a separate option.

## Runtime data and privacy

Failure Memory never writes runtime data into the Git checkout or installed plugin.
The global data root is resolved in this order:

1. explicit `FAILURE_MEMORY_HOME`;
2. the platform default:
   - macOS: `~/Library/Application Support/failure-memory`;
   - Windows: `%LOCALAPPDATA%/FailureMemory`;
   - Linux: `$XDG_DATA_HOME/failure-memory`, falling back to
     `~/.local/share/failure-memory`.

Harness-specific `PLUGIN_DATA`, `CLAUDE_PLUGIN_DATA`, and similar locations do not select
the active store. This prevents Codex, Claude, Cursor, Copilot, and other local clients
from silently creating isolated memories. They may still be discovered as import sources.

On POSIX systems, owned directories use mode `0700`; the identity key, database, WAL,
and SHM files use `0600`. The service rejects unsafe symlinks, unexpected path types,
inaccessible paths, and paths owned by another user.

Authoritative and derived state are separated:

```text
<data-root>/adapters/event-store/sqlite/primary/failure-memory.sqlite3
<data-root>/adapters/retrieval/sqlite-vec/global-v2/<profile>/index.sqlite3
<data-root>/adapters/embedding/fastembed/<model-revision>/
<data-root>/adapters/runtime/<lock-hash>/
```

Back up the event-store database. Retrieval indexes can be rebuilt from accepted lessons;
adapter environments and models can be reinstalled from the pinned plan.

Redaction is a defense-in-depth boundary, not a complete data-loss-prevention system. Do
not submit secrets, personal data, raw transcripts, or unnecessary private paths. Health
and metrics responses intentionally omit the absolute database path.

See [operations](docs/operations.md) for backup, recovery, retention, and troubleshooting.

## Package-builder safety

`packaging/build_codex.py` uses held directory descriptors and atomic same-parent
publication. It performs **no deletion**. A replaced output is retained as a private
rollback sibling, and a failed publication reports exact recovery paths. Unresolved
artifacts block later builds until a release operator inspects them.

The builder supports macOS and Linux because it relies on their atomic no-replace and
directory-exchange primitives. A hostile process running as the same user is outside its
security boundary. The runtime service itself also contains Windows data-root and
permission handling. After a replacement passes validation, obsolete retained artifacts
may be moved to Trash or another recoverable quarantine location.

See [package-builder security](docs/packaging-security.md) for the full boundary.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run pytest --cov=failure_memory --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv build
uv run python tools/render_skills.py --check
```

Contributor guidance is in [CONTRIBUTING.md](CONTRIBUTING.md). Security reporting
instructions are in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
