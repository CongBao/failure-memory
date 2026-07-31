---
name: record-agent-failure
description: Use when a user challenges an agent outcome, reports a missed prior invariant or repeated correction, or explicitly asks the agent to learn from a failure; do not treat new requirements, new details, first-time preferences, or ordinary refinement as failures.
---

<!-- Generated from contract.json; policy sha256=4e97143d850152b64f3ca72d4e1ab05ed3b746594b6cac3d6809db0a7f155365; DO NOT EDIT SKILL.md MANUALLY. -->

# Record Agent Failure

Decide from evidence available before the outcome. Corrective wording alone is not a failure.

## Fast path

1. Choose one classification. For `mixed`, keep only the prior-invariant mismatch in `failure_portion`.
2. For `real_failure` or `mixed`, provide compact `expectation`, `observed`, `cause`, and `lesson` objects. Use `unknown` for an uninspectable cause.
3. Call `remember_failure` exactly once for every classification, including a non-failure. For a non-failure, send only the compact classification evidence; do not invent failure objects. The service records qualification telemetry and creates a lesson only when warranted.
4. Report the returned status briefly. A recorded lesson remains `proposed`.

If the named tool is unavailable, resolve the bundled [CLI launcher](scripts/failure_memory_cli.py) relative to this skill and execute it once with `remember --stdin`, passing the same JSON on standard input. If the launcher is unavailable, report one installation error and stop.

Never search for the plugin, inspect source or SQLite, import private APIs, discover runtimes, retry alternate commands, or create temporary files. Exclude raw prompts, secrets, unnecessary user text.

## Classifications

| Class | Meaning |
|---|---|
| `requirement_update` | The requested feature or contract changed after the outcome. |
| `requirement_clarification` | A previously unavailable detail now clarifies the work. |
| `preference_update` | A preference is stated for the first time. |
| `real_failure` | A prior expectation, controllable mismatch, impact or recurrence risk, and durable prevention lesson are evidenced. |
| `mixed` | Feedback contains both a genuine prior-invariant mismatch and new work. |
| `uncertain` | The available evidence cannot establish the chronology or failure criteria. |
