# Changelog

All notable changes will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases
use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- One-call `remember_failure` qualification, causal diagnosis, exact deduplication,
  persistence, and recording telemetry.
- Portable adjacent CLI launchers for agents that cannot expose plugin MCP tools.
- Managed GitHub Copilot Chat in VS Code and generic-agent projections.
- Append-only recording-operation latency, outcome, harness, and transport metrics.

### Changed

- The normal MCP surface now advertises only `remember_failure` and
  `recall_failure_lessons`; administrative operations remain available through the CLI
  and an explicit MCP admin profile.
- Recording performs exact reuse synchronously and defers semantic reconciliation, model
  loading, and dependency installation so the agent call stays fast.
- The recording and recall skills use one bounded call and forbid plugin discovery,
  source inspection, direct SQLite access, private API imports, retries, and temporary
  payload files.

## [0.5.0] - 2026-07-31

### Added

- Public GitHub marketplace catalogs for Codex, Claude Code, and GitHub Copilot CLI.
- Append-only accept, reject, and defer reviews for lesson-generalization proposals,
  including optional broader proposed lessons that preserve every source lesson.
- A weak, traceable recall channel that may add at most one neighbor from an accepted
  reviewed cluster without merging or promoting lessons.
- A checked-in 52-case synthetic evaluation corpus and private, append-only shadow
  evaluation reports with explicit quality thresholds.
- Duplicate-safe installation inspection and application for Claude Code and Cursor,
  completing the Codex, Claude Code, Copilot CLI, and Cursor projections.

### Changed

- Reworked the README around the project's purpose, supported agents, installation, and
  natural-language usage.
- Added public repository and author metadata to every host plugin manifest.
- Expanded the MCP surface to nineteen schema-validated operations.
- Packaged the public synthetic evaluation corpus with release bundles.

## [0.4.0] - 2026-07-30

### Added

- Required second-tier generalization review with bounded exact, lexical, and semantic
  candidates before public incident recording.
- Append-only generalization review/decision events and exact signature aliases.
- Explicit reviewed reuse, generalization, and distinct-lesson dispositions.
- Codex, Claude Code, GitHub Copilot, and Cursor plugin manifests.
- Bounded session-start hooks that inject static guidance without storing prompts.
- Duplicate-safe Codex/Copilot installation planning and application, including bundle
  commit comparison for same-version Copilot rebuilds.
- Runtime harness detection with one platform-global store across every projection.

### Changed

- The recording skill now requires qualification, generalization review, and explicit
  disposition before a write.
- The MCP surface now contains seventeen schema-validated operations.

## [0.3.0] - 2026-07-30

### Added

- One owner-private global personal memory shared across trusted local harnesses.
- Read-only discovery and transactional copy-only import for earlier local stores, with
  content fingerprints, collision detection, idempotent receipts, and verification.
- Append-only missed-relevant feedback, lesson lifecycle events, ranking experiments,
  cluster runs, members, and generalization proposals.
- Learning metrics for feedback coverage, precision at one and three, usefulness,
  false positives, missed relevant lessons, exact reuse, and origin harnesses.
- Reviewed lesson transitions, shadow-only feedback ranking experiments, and
  proposal-only semantic clustering through CLI and MCP operations.
- Native-system certificate validation and deterministic non-Xet model downloads for the
  explicitly installed semantic adapter.

### Changed

- Exact reuse and lexical/vector retrieval are global; workspace and session fingerprints
  remain provenance only.
- Platform data directories are now the default global store. Harness plugin-data roots
  are treated only as possible legacy import sources.
- The MCP surface now contains sixteen schema-validated operations.
- The MCP launcher switches to the validated private adapter interpreter when the host
  Python cannot load SQLite extensions, keeping installed hybrid retrieval usable.

## [0.2.0] - 2026-07-30

### Added

- Workspace-scoped FTS5 lexical recall and optional sqlite-vec exact cosine KNN.
- Exact-first `auto` recall and application-level reciprocal-rank fusion.
- Append-only retrieval-profile, recall-attempt, candidate, selection, and outcome
  telemetry without raw query storage.
- Structured usefulness, false-positive, recurrence-prevention, contradiction, staleness,
  ignored, and unknown outcomes.
- Explicit private adapter plan/status/install commands with pinned sqlite-vec and
  FastEmbed versions.
- Retrieval index status/build, recall, feedback, and recall-metrics CLI/MCP operations.

### Changed

- Revised the recall skill for evidence-gated bounded hybrid recall, traceable returned
  IDs, and feedback only after observable outcomes.
- Prepared documentation, metadata, diagnostics, and repository hygiene for public source
  distribution.

## [0.1.0]

### Added

- Chronology-aware failure qualification.
- Append-oriented SQLite incident and lesson storage.
- Exact lesson reuse and recall.
- JSON CLI and dependency-free MCP stdio server.
- Separate Codex skills for recording and recall.
- Commit-stamped, no-deletion Codex package builder.
