# Changelog

## [Unreleased]

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
