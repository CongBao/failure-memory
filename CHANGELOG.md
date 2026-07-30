# Changelog

All notable changes will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases
use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
