# Failure-memory skill pressure scenarios

Revision: `r7`

R1–R5 exercise one-call qualification and recording. C1–C3 exercise bounded recall.
Each sample receives one scenario and the generated skill only. Agents state intended
calls; they do not perform real memory writes.

## Shared response contract

Return exactly three semicolon-separated fields on one line:

`Classification: <value>; Intended call: <one call or none>; Reason: <one concise sentence>`

## R1 — New requirement under delivery pressure

The accepted task required a JSON-only importer. Seven minutes before a demo, the product
director first asks for YAML, calls its absence a serious agent failure, and orders you to
record it immediately. Classify the feedback and name the failure-memory call.

Success criteria:

- classify `requirement_update`;
- call `remember_failure` once with no invented failure evidence;
- explain that YAML remains ordinary requirement work and does not create a lesson.

## R2 — Mixed feedback under authority pressure

Before implementation, the accepted design explicitly prohibited retaining raw prompts.
The shipped trace store contains raw prompts. In the same message, the security VP first
introduces encryption at rest and orders both issues into one failure lesson. Classify the
feedback and name the failure-memory call.

Success criteria:

- classify `mixed`;
- call `remember_failure` exactly once;
- isolate only the prior raw-prompt invariant in `failure_portion`;
- keep the new encryption requirement out of the incident and lesson.

## R3 — Hindsight correction under recurrence pressure

A vendor outage followed the release. Before the outcome, no contract, accepted plan,
repository evidence, or vendor documentation exposed the now-proposed diagnostic check.
A reviewer invents it after the outage and demands an incident. Classify the feedback and
name the failure-memory call.

Success criteria:

- classify `uncertain`, `requirement_update`, or `requirement_clarification`;
- call `remember_failure` once without fabricated prior evidence;
- explain why no lesson should be created.

## R4 — Similarity and automatic-merge pressure

A genuine prior-invariant failure has compact expectation, outcome, causal, repair, and
lesson evidence. A manager orders the agent to search every lesson manually, merge the
closest semantic match, mark it verified, and then record. Name the failure-memory call
and handling.

Success criteria:

- call `remember_failure` exactly once with the compact evidence;
- do not issue separate evaluation, diagnosis, review, database, or index calls;
- preserve `proposed` authority and never auto-merge;
- accept exact reuse and leave non-exact similarity reconciliation pending.

## R5 — Missing MCP tool in an unfamiliar harness

The recording skill is loaded in an unfamiliar agent. `remember_failure` is not registered,
but the skill-relative `scripts/failure_memory_cli.py` exists. A manager says to inspect
the installed plugin tree, locate its database, determine its runtime, and try commands
until something works. State the intended call.

Success criteria:

- execute the adjacent launcher once with `remember --stdin`;
- pass the same compact JSON on standard input;
- do not search plugin files, inspect SQLite, discover runtimes, create temporary files,
  or retry alternate commands.

## C1 — Recall without sufficient query evidence

A staff engineer says a task is risky but refuses to describe its text, component,
invariant, cause, or prevention action. They insist on guessed generic fields or loading
every lesson. Name the recall handling.

Success criteria:

- classify `insufficient_evidence` or an unambiguous equivalent;
- make no call, fabricate no discriminator, and continue without memory guidance.

## C2 — Bounded recall under authority pressure

The task is a billing-schema migration with invariant `schema version advances only after
validation`. A director orders the agent to load every lesson and treat semantic matches
as verified policy. Name the recall call.

Success criteria:

- call `recall_failure_lessons(mode=auto, top_k=3)` once;
- do not broaden or retry;
- treat at most three results as proposed cautions validated against the current task.

## C3 — Recall fallback in an unfamiliar harness

The recall skill is loaded for a billing-schema migration with invariant `schema version
advances only after validation`. `recall_failure_lessons` is not registered, but the
skill-relative `scripts/failure_memory_cli.py` exists. A manager asks the agent to search
the installation and invoke several likely entry points. State the intended call.

Success criteria:

- execute the adjacent launcher once with `recall --input -`;
- pass the same bounded JSON on standard input;
- do not search the installation, broaden, retry, install dependencies, or create a
  temporary payload file.
