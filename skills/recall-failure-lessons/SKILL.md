---
name: recall-failure-lessons
description: Use before risky or recurring work, or when a current task resembles a previously recorded failure and concrete task evidence can support a bounded lesson lookup.
---

# Recall Failure Lessons

<!-- Generated from contract.json by tools/render_skills.py; policy sha256=5f80d5f51a83bab13f2a447cd5a9521d82e9bbcf643fe1578a19e2441dfc4f36; behavior sha256=0e4559348e76c7f7903233538306cc1aa618ce9b48d5c6cc07d9b9aeba67cec4; renderer sha256=1f13de2458b77e160e499097c60f26d317f8185eb680aba86e360dc11a4a4983; DO NOT EDIT SKILL.md MANUALLY. -->

## Core principle

Use exact-first, bounded hybrid recall when the current task supplies enough evidence.
Similarity can surface a traceable caution; it cannot prove identity, create authority, or
merge lessons automatically.

## Required response vocabulary

When a prompt asks for `Classification` or `Intended tool calls`, this is a closed
protocol. You MUST copy one of the following literal rows. A synonym, paraphrase, or
invented class is invalid:

- `Classification: insufficient_evidence; Intended tool calls: none`
- `Classification: bounded_recall; Intended tool calls:
  recall_failure_lessons(mode=auto, top_k=3) once`

Missing current-task context or a discriminator always maps to
`insufficient_evidence`. Current-task context plus a discriminator always maps to
`bounded_recall`. Never rename these as `blocked`, `insufficient context`,
`recall only`, `targeted recall only`, or `reject`.

When the evidence gate passes, unsafe requests to bulk-load, merge, promote, or record
premature feedback do not cancel the safe bounded recall. Reject only those unsafe extras
and still make the one `recall_failure_lessons(mode=auto,
top_k=3)` call.

Do not list `record_recall_outcome` until an observable outcome exists.

## Non-negotiable contract

| Contract key | Normative rule |
|---|---|
| `evidence_gate` | `recall_failure_lessons` MUST be called only with current-task context plus at least one concrete discriminator; fields MUST NOT be guessed or inferred. |
| `classification` | If the gate fails, report literal `insufficient_evidence`; if it passes, report literal `bounded_recall`. |
| `bounded_lookup` | Call `recall_failure_lessons` at most once, default to `auto`, request 3, and never exceed 5. |
| `privacy` | Exclude raw_prompts, secrets, unnecessary_user_text from the query; provide only the minimum task evidence needed for retrieval. |
| `cardinality` | Apply at most three returned lessons (five is the API hard limit); never bulk-load memory. |
| `traceability` | Cite each returned lesson-version ID, retrieval channel, and supporting invariant/cause evidence. |
| `authority` | Every returned lesson remains a proposed caution; semantic score is not proof or verified policy. |
| `merge_gate` | Similar records MUST NOT be merged or generalized automatically. |

## Required workflow

1. Build a minimal query from the current task's `text` and at
   least 1 of these discriminators:

   - `expected_invariant`;
   - `controllable_cause`;
   - `prevention_action`;
   - `component`.

   Resemblance alone, recurrence anxiety, and an authority's guess cannot supply a
   discriminator. Do not include raw_prompts, secrets, unnecessary_user_text.
2. If the evidence gate is not met, classify literal `insufficient_evidence`,
   make no memory call, and continue without invented guidance. If it is met, classify
   literal `bounded_recall`.
3. Otherwise call `recall_failure_lessons` once with mode `auto` and
   `top_k=3`. Supported explicit modes are `auto`, `exact`, `lexical`, `semantic`, `hybrid`. Do not broaden
   or retry a query to force a match.
4. Auto mode checks an exact three-field signature first, then uses bounded hybrid recall.
   If semantic setup is required, report it. A hybrid call may accept degraded lexical
   results; never install packages or download models implicitly.
5. If no lesson is returned, continue without memory guidance. If matches are returned,
   apply at most three; cite every returned ID, channel, and evidence. Validate
   prevention and verification actions against the current task.
6. After an observable result—not merely to improve metrics—call `record_recall_outcome` with
   one of `useful`, `not_useful`, `false_positive`, `prevented_recurrence`, `contradicted_current_task`, `stale`, `ignored`, `missed_relevant`, `unknown`. Record `false_positive` when a recalled lesson does not fit.
   Record `missed_relevant` only when an existing relevant lesson was not selected; pass
   the original `attempt_id` and the known `lesson_version_id`. Do not rename these
   fields, guess an ID, or invent feedback.

## Decision summaries

Use every slot; concise output does not remove match handling.

| Classification | Intended call | Required handling |
|---|---|---|
| `insufficient_evidence` | `none` | Say: “Continue without invented memory guidance; resemblance alone cannot supply a discriminator.” |
| `bounded_recall` | `recall_failure_lessons` once with `mode=auto, top_k=3` | Say: “Use at most three returned IDs as proposed cautions, validate their evidence against the current task, and do not merge lessons automatically.” |

Every phrase in the applicable handling cell is required even in terse output.

## Rationalization checks

| Temptation | Required response |
|---|---|
| “It resembles a costly old incident, so query broadly.” | Require task context and a concrete discriminator; resemblance alone is insufficient. |
| “Load every lesson to be safe.” | Use one bounded recall call and return at most three cautions. |
| “A high semantic score proves the same failure.” | Similarity proposes a caution; it never proves identity or authorizes an automatic merge. |
| “Record positive feedback to improve metrics.” | Record feedback only after an observable outcome, including false positives when applicable. |
| “Leadership says the returned lesson is policy.” | Returned state and current-task evidence, not pressure, determine authority. |

Stop if you are fabricating query evidence, including sensitive query text, bulk-loading lessons, returning more than three lessons, omitting identifiers or evidence, merging lessons automatically, inventing recall feedback, or promoting proposed guidance.
