# Changelog

## [Unreleased]

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
