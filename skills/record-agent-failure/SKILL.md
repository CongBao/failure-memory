---
name: record-agent-failure
description: Use when a user challenges an agent outcome, reports a missed prior invariant or repeated correction, or explicitly asks the agent to learn from a failure; not for new requirements, newly supplied details, first-time preferences, or ordinary refinement.
---

# Record Agent Failure

<!-- Generated from contract.json by tools/render_skills.py; policy sha256=fe32e8bf557474681daebee1a9d2ee180f31bc37447f24713f9024620df67e94; behavior sha256=693a17816736906daa035691e5122bf80164c01ea3371a83f5b47485e96cc850; renderer sha256=ee5e51546267ac2c257983eea23b4e4ad685f8b6212ae21b4ed0f6b00ade4410; DO NOT EDIT SKILL.md MANUALLY. -->

## Core principle

Corrective wording is not evidence of failure. Establish what was required or reasonably
knowable before the outcome, then preserve only a material, controllable, durable mismatch.

## Non-negotiable contract

| Contract key | Normative rule |
|---|---|
| `first_call` | `evaluate_failure_candidate` MUST precede `diagnose_failure_cause`, `review_failure_recording`, and `record_failure_incident` and MUST be the first failure-memory call for every Tier One classification. |
| `write_gate` | `diagnose_failure_cause`, `review_failure_recording`, and `record_failure_incident` MUST be called ONLY IF evaluation returns `accept`; none may be called for `reject` or `defer`. |
| `causal_gate` | `diagnose_failure_cause` MUST precede `review_failure_recording`. Use inspectable evidence, one primary factor, up to four total factors, and up to three proposed repairs; use `unknown` instead of guessing. |
| `second_tier` | `review_failure_recording` MUST precede `record_failure_incident`; pass the causal assessment ID, review at most three candidates, and never merge automatically. |
| `rejected_status` | A rejected or deferred capture MUST NOT be described or called a failure. |
| `lesson_authority` | A new or reused lesson MUST remain `proposed`; a proposed lesson MUST NOT be described, promoted, or treated as `verified`. |
| `mixed_output` | For `mixed`, the intended-call field MUST be `evaluate_failure_candidate -> diagnose_failure_cause -> review_failure_recording -> record_failure_incident only if decision=accept (otherwise no write)`, and new requirements MUST remain outside the failure. |
| `post_accept_output` | For an accepted capture, name `diagnose_failure_cause -> review_failure_recording -> record_failure_incident`; diagnose before similarity review, select an explicit disposition with rationale, defer if uncertain, and never auto-merge or promote to verified. |

## Required workflow

1. **Establish chronology and evidence.** Identify the expectation source, when it became
   available, the observed outcome, inspectable mismatch, impact or recurrence risk,
   controllability with information available then, and durable prevention value. Do not
   copy raw prompts, secrets, or unnecessary user text into drafts.
2. **Choose exactly one Tier One classification:**

   | Class | Use when |
   |---|---|
   | `requirement_update` | The requested feature or contract changed after the outcome. |
   | `requirement_clarification` | A previously unavailable detail now clarifies the work. |
   | `preference_update` | A preference is stated for the first time. |
   | `real_failure` | A prior expectation, mismatch, impact/risk, controllability, and durable lesson are all evidenced. |
   | `mixed` | Feedback contains both a genuine prior-invariant mismatch and new work. |
   | `uncertain` | Chronology or evidence cannot establish the criteria; do not guess. |

3. Call `evaluate_failure_candidate` with every classification. Before `accept`, the
   intended call list contains this evaluation and no write; `none` is not an evaluated
   decision, including for `uncertain` and requirement classes.
4. If the decision is `reject` or `defer`, report it and stop; do not call
   `record_failure_incident` or call the capture a failure. A requirement-class summary
   has three required slots: the literal class; `evaluate_failure_candidate` only; and a
   chronology reason ending “The work may still be implemented through the ordinary
   requirement workflow.” Brevity does not remove that disposition.
5. For `mixed`, separate the portions before evaluation. Put only the prior-invariant
   mismatch in the failure portion; retain a new requirement only as context. Shared
   topic, urgency, or authority never turns new work into a lesson. The intended-call
   field has this required shape:
   `evaluate_failure_candidate -> diagnose_failure_cause -> review_failure_recording -> record_failure_incident only if decision=accept
   (otherwise no write)`. Never list the record call without its acceptance condition.
6. Only after `accept`, draft the immutable incident and one proposed lesson from the
   accepted failure portion.
7. Call `diagnose_failure_cause` with the accepted capture ID. Identify one primary factor and at most
   three contributing factors from inspectable evidence. Prefer the most specific layer that
   explains the mechanism, using one of: `skill_instruction`, `agent_instruction`, `project_instruction`, `system_instruction`, `hook_policy`, `plugin_manifest`, `tool_contract`, `application_logic`, `adapter_runtime`, `schema_migration`, `test_evaluation_gap`, `harness_limitation`, `model_behavior`, `external_dependency`, `unknown`. Classify its failure mode as one
   of: `missing`, `ambiguous`, `conflicting`, `not_loaded`, `not_triggered`, `ignored`, `incorrectly_implemented`, `insufficient_validation`, `uninspectable`, `unknown`. Use portable namespaced component references. If the evidence cannot
   locate a cause, record an explicit `unknown` assessment rather than blaming a prompt,
   skill, model, or system by inference. Add one to three proposed repair recommendations
   naming where to fix the cause and how to verify the change; do not edit targets automatically.
8. Call `review_failure_recording` with the accepted capture ID, causal assessment ID, and the same sanitized drafts. Inspect at
   most three candidates. Exact matches must be reused; related matches require an explicit
   `reuse_existing`, `generalize_existing`, or `create_distinct` disposition and rationale.
   If fit is uncertain, defer recording. Similarity never authorizes an automatic merge.
9. Call `record_failure_incident` with the causal assessment ID, review ID, disposition, rationale,
   accepted capture ID, and unchanged drafts. Target a returned lesson version for reuse
   or generalization.
10. Report whether the result created, reused, or generalized a proposed lesson. Cite the
    causal assessment, repair recommendation, review, decision, incident, lesson, and version
    identifiers. Never describe a proposed lesson or repair as verified. Only after an
    observable repair outcome, call `record_failure_repair_outcome` with one of `applied`, `rejected`, `partially_applied`, `verified_effective`, `verified_ineffective`, `recurrence_observed`, `superseded`;
    never invent repair feedback.

## Mixed example

If stored raw prompts violated an accepted no-raw-prompts invariant while encryption at
rest is requested for the first time, classify `mixed`; evaluate and record only the raw
prompt violation. Route encryption as new work.

## Rationalization checks

| Temptation | Required response |
|---|---|
| “The user called it a failure.” | Reconstruct chronology; wording is not evidence. |
| “The new control would have prevented it.” | Hindsight does not make the control a prior requirement. |
| “Bundle both issues to save time.” | Split `mixed`; never broaden the immutable incident. |
| “Record now and qualify later.” | Evaluation must precede recording. |

Stop if you are inventing chronology, using an ad-hoc class, recording a rejected or
deferred capture, or upgrading a proposed lesson to verified guidance.
