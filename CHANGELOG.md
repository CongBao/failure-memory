# Changelog

## [Unreleased]

## [0.11.0] - 2026-08-13

### Added

- Calibrated relevance thresholds are applied before `top_k`, with zero-result
  abstention, cluster collapse, per-result relevance scores, and selection diagnostics.
- A third idempotent MCP/CLI operation records evidence-bounded recall, repair, and
  lesson outcomes without deleting history.
- Append-only lesson lifecycle projections retain false-positive, stale, superseded,
  and generalized lessons while excluding inactive versions from normal recall.
- Accepted generalization reviews create a parent lesson and retain child lessons as
  superseded audit history.
- Metrics now include recall filtering and abstention rates, latency percentiles,
  input/output size, outcome coverage, lifecycle counts, generalization backlog, and
  per-harness usage.
- Integration coverage calibrates multilingual semantic recall against a real pinned
  E5 model, including a required abstention for an unrelated query.

### Changed

- Recall requires only compact task text. Optional discriminators are no longer
  invented, and an empty result is treated as a successful bounded lookup.
- FTS5 evidence uses rank-based calibration because raw BM25 magnitudes vary with
  corpus size; model-based semantic profiles use a separately verified threshold.
- The shared prompt guidance and all supported harness integrations expose the same
  three-operation memory workflow.

## [0.10.5] - 2026-08-06

### Fixed

- Long multilingual lessons no longer prevent semantic index repair when the
  pure-Go tokenizer omits usable token spans. Embedding projections now bound
  tokenizer work and use verified token counts to enforce the model window
  while preserving the complete authoritative lesson.

## [0.10.4] - 2026-08-06

### Changed

- Replaced the failure exclamation mark with a stronger red cross throughout
  the plugin, recording, and recall icon system for clearer recognition at
  small sizes.
- Simplified the compact composer icon to the durable-memory container and
  failure mark so it remains legible down to 16 pixels.

## [0.10.3] - 2026-08-05

### Added

- A compact Failure Memory brand system with dedicated plugin, recording, and
  recall icons, including light- and dark-surface plugin artwork.
- Codex plugin and skill metadata now reference the packaged visual assets and
  use distinct failure and recall accent colors; Cursor's plugin manifest uses
  the shared logo through its native `logo` field.

## [0.10.2] - 2026-08-04

### Fixed

- Codex installation now refreshes an existing marketplace when the same public
  repository was previously registered with an equivalent URL form.

## [0.10.1] - 2026-08-04

### Fixed

- Numeric `0..1` cause confidence values are accepted and normalized consistently by
  MCP and CLI recording paths.
- The public tool schema now exposes the allowed classification, cause-layer, and
  failure-mode values instead of leaving callers to infer hidden runtime enums.
- Deterministic input-validation rejections can return one machine-guided correction;
  the skill still forbids retries after timeouts, ambiguous failures, or persisted
  outcomes.

## [0.10.0] - 2026-08-01

### Added

- Versioned, checksummed SQLite schema migrations with automatic pre-migration
  snapshots and forward-version rejection.
- Verified backup, backup inspection, and guarded restore commands with pre-restore
  safety backups and interrupted-restore recovery.
- Cross-process tests for concurrent agent writes, writer crashes, MCP startup with a
  restricted `PATH`, and shared-store behavior.
- Race detection, installer analysis, isolated installer smoke tests, packaged-runtime
  smoke tests, and expanded cross-platform release gates.

### Changed

- Native MCP registration now uses each detected agent's user-level configuration and
  the absolute path to the shared runtime instead of a plugin-relative launcher.
- Derived retrieval indexes track the authoritative lesson revision and deterministic
  lesson manifest, and repair missing or stale vectors automatically.
- The installation identity key is now created atomically under a cross-process lock.

### Fixed

- Already-running writers from an older schema remain compatible after a migration.
- Restore refuses to run while any agent process is using the event store.

## [0.9.0] - 2026-08-01

### Added

- One-command installation of the shared native runtime and every detected Codex,
  Claude Code, or GitHub Copilot CLI plugin.
- Agent selection, idempotent marketplace updates, macOS application discovery, and an
  explicit Cursor handoff when automatic plugin installation is unavailable.

## [0.8.0] - 2026-07-31

### Added

- Native, dependency-free end-user executable for macOS, Linux, and Windows.
- One shared personal store across Codex, Claude Code, GitHub Copilot, Cursor, and generic
  CLI-capable agents.
- Exact, FTS5, sqlite-vec, optional multilingual semantic, and hybrid retrieval.
- Append-only capture, incident, lesson, recall, repair, outcome, clustering, and
  generalization-review events.
- Copy-only, idempotent migration from the earlier v0 SQLite store.
- Silent-by-default corrective prompt hooks and two concise agent skills.
- Checksum-verified release installers and cross-platform release builds.

### Changed

- MCP is now an optional two-tool adapter over the same native service used by the CLI.
- Requirement changes, clarifications, and first-time preferences are recorded as
  classification attempts but never create failure lessons.
- Related lessons are preserved separately and queued for review; no lesson is
  automatically merged or deleted.
