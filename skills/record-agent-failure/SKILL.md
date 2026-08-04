---
name: record-agent-failure
description: Use when a user challenges an agent outcome, reports a missed prior invariant or repeated correction, or explicitly asks the agent to learn from a failure; do not treat new requirements, new details, first-time preferences, or ordinary refinement as failures.
---

# Record Agent Failure

Do not trigger for an ordinary new requirement or refinement. Trigger only when feedback
challenges an earlier outcome, reports recurrence, or explicitly asks for failure
qualification/learning. Then make one fast classification from evidence that existed
before the outcome:

- `requirement_update`, `requirement_clarification`, or `preference_update`: not a failure.
- `real_failure`: a prior invariant, inspectable mismatch, material impact or recurrence risk, controllable cause, and durable prevention are all evidenced.
- `mixed`: separate only the prior-invariant failure into `failure_portion`; keep new work out of memory.
- `uncertain`: chronology or required evidence cannot be established.

Once triggered, normally call `remember_failure` once even when the classification is a
non-failure or `uncertain`; this preserves false-positive qualification telemetry. Those
classifications use only:

```json
{"summary":"compact chronology","classification":"requirement_update"}
```

For `mixed`, `failure_portion` is a compact string containing only the old-invariant
mismatch. Real or mixed failures use the exact tool schema: `expectation` has `invariant`,
`source`, `evidence`; `observed` has `outcome`, `impact`, optional `recurrence_risk`;
`cause` has `layer`, `failure_mode`, `component`, `evidence`, `recommended_change`, and
`verification`; `lesson` has `rule`, `prevention`, `verification`, and optional `title`,
`applicability`, `counterexamples`.

Use only these canonical cause values:

- `layer`: `skill_instruction`, `agent_instruction`, `project_instruction`,
  `system_instruction`, `hook_policy`, `plugin_manifest`, `tool_contract`,
  `application_logic`, `adapter_runtime`, `schema_migration`, `test_evaluation_gap`,
  `harness_limitation`, `model_behavior`, `external_dependency`, or `unknown`.
- `failure_mode`: `missing`, `ambiguous`, `conflicting`, `not_loaded`, `not_triggered`,
  `ignored`, `incorrectly_implemented`, `insufficient_validation`, `uninspectable`, or
  `unknown`.
- `confidence` is optional. Prefer omitting it; when useful, send the string `low`,
  `medium`, `high`, or `unknown`. Numeric `0..1` is accepted for compatibility.

Treat a user's explicit statement of chronology or a prior invariant as task evidence
unless available evidence contradicts it. For `cause`, use only evidence already
available in the task. Identify the deepest
evidenced controllable layer—such as a skill instruction, agent/project/system prompt,
hook policy, tool contract, runtime adapter, schema, test gap, or implementation—and
name the component and recommended repair location. Use `unknown` when it cannot be
inspected only when the failure is otherwise established and an evidenced component
boundary, prevention, and verification can still be stated. If no controllable boundary
or durable prevention can be supported, classify `uncertain`.

Use at most one correction retry, and only in either of these deterministic cases:

1. The call returns `retryable: true` with `correction.allowed_values`. Correct only the
   named fields, copy `correction.correction_of_capture_event_id` into
   `correction_of_capture_event_id`, and call once more.
2. The tool reports an explicit input/schema validation rejection before execution.
   Correct only the rejected field from the published values and call once more; do not
   set `correction_of_capture_event_id` because no capture was created.

Never retry after a timeout, connection loss, cancellation, ambiguous tool error,
`recorded`, `not_failure`, or a non-retryable `deferred` result. Never make a third call.

Only when the tool capability is absent before invocation, pass the same JSON on standard
input to `failure-memory remember`. The same bounded correction rule applies to that
fallback. Do not use the fallback after a tool timeout or ambiguous failure. Do not search
for the plugin, inspect SQLite, discover runtimes, or create a temporary file.

Exclude raw prompts, secrets, personal data, transcripts, and unnecessary paths. Briefly report the returned status; every new or generalized lesson remains a proposal until reviewed.
