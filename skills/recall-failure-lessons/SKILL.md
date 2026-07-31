---
name: recall-failure-lessons
description: Use before risky or recurring work, or when a current task resembles a previously recorded failure and the task provides concrete evidence for a bounded lookup.
---

# Recall Failure Lessons

Use one lookup only when the task provides compact `text` plus at least one concrete discriminator: `expected_invariant`, `controllable_cause`, `prevention_action`, or `component`.

Call `recall_failure_lessons` once with `mode=auto` and `top_k=3`. Do not broaden or retry the query. Apply at most three returned lessons as proposed cautions and validate them against the current task.

If the minimum query is absent, skip recall and continue; do not create a clarification
turn solely to obtain a memory discriminator.

Only when the tool capability is absent before invocation, pass the identical JSON on
standard input to `failure-memory recall` once. Do not use the fallback after a timeout
or ambiguous tool failure. After such a tool failure, continue without memory guidance
and mention it once only when relevant. If the command is absent, continue without
memory guidance and mention the installation issue once.

Do not inspect plugin files or SQLite, install anything, create temporary files, or include raw prompts, secrets, personal data, or unnecessary paths. Similarity is not proof, policy authority, or permission to merge lessons.
