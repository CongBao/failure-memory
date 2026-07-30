---
name: record-agent-failure
description: Use when a user challenges an agent outcome, reports a missed prior invariant or repeated correction, or explicitly asks the agent to learn from a failure; not for new requirements, newly supplied details, first-time preferences, or ordinary refinement.
---

# Record Agent Failure

<!-- Generated from contract.json by tools/render_skills.py; policy sha256=37acd89ff6cf98655e02f14bbe2af40836b1cef854bc58bb699d1a729c1628ce; behavior sha256=501aafd6da4a77107c4ffc38cb276db21824645d9a1b330d244b799aaa71fbf1; renderer sha256=4b524d3cba4491d5bd7825a5c6d343a60297bc6ddfafe269fa8f01af4d2c36d0; DO NOT EDIT SKILL.md MANUALLY. -->

## Core principle

Corrective wording is not evidence of failure. Establish what was required or reasonably
knowable before the outcome, then preserve only a material, controllable, durable mismatch.

## Non-negotiable contract

| Contract key | Normative rule |
|---|---|
| `first_call` | `evaluate_failure_candidate` MUST precede `record_failure_incident` and MUST be the first failure-memory call for every Tier One classification. |
| `write_gate` | `record_failure_incident` MUST be called ONLY IF evaluation returns `accept`; `record_failure_incident` MUST NOT be called for `reject` or `defer`. |
| `rejected_status` | A rejected or deferred capture MUST NOT be described or called a failure. |
| `lesson_authority` | A new or reused lesson MUST remain `proposed`; a proposed lesson MUST NOT be described, promoted, or treated as `verified`. |
| `mixed_output` | For `mixed`, the intended-call field MUST be `evaluate_failure_candidate -> record_failure_incident only if decision=accept (otherwise no write)`, and new requirements MUST remain outside the failure. |

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
   `evaluate_failure_candidate -> record_failure_incident only if decision=accept
   (otherwise no write)`. Never list the record call without its acceptance condition.
6. Only after `accept`, draft the immutable incident and one proposed lesson from the
   accepted failure portion.
7. Call `record_failure_incident` with the accepted capture ID and sanitized drafts.
8. Report whether the result created a new proposed lesson or reused an exact existing
   lesson. Cite returned identifiers. Never describe a proposed lesson as verified.

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
