---
name: recall-failure-lessons
description: Use before risky or recurring work, or when a current task resembles a previously recorded failure and the task provides concrete evidence for a bounded lookup.
---

# Recall Failure Lessons

Call `recall_failure_lessons` once with compact task evidence in `text`. Add
`component` only when it is already known. Do not invent an invariant, cause, or
prevention action merely to make the query look complete.

Normally omit `mode`, `top_k`, and `min_relevance`; the tool applies its calibrated
profile threshold, collapses related lessons, and returns zero to three results.
Treat `top_k` only as a maximum and `min_relevance` as an optional stricter floor.
Never lower the threshold, broaden, or retry to fill a result list.

Accept an empty result and continue. Apply returned lessons only as proposed cautions
after validating them against the current task.

If later evidence in the same task clearly establishes whether the recall was applied,
not applicable, contradicted, or prevented/failed to prevent recurrence, call
`report_memory_outcome` once with the returned `attempt_id`, the affected returned
lesson IDs, and a compact `evidence_code`. Do not pause the task to manufacture an
outcome or report one when it remains unknown.

Only when the tool capability is absent before invocation, pass the identical JSON on
standard input to `failure-memory recall` once. Do not use the fallback after a timeout
or ambiguous tool failure. After such a tool failure, continue without memory guidance
and mention it once only when relevant. If the command is absent, continue without
memory guidance and mention the installation issue once.

Do not inspect plugin files or SQLite, install anything, create temporary files, or
include raw prompts, secrets, personal data, or unnecessary paths. Similarity is not
proof, policy authority, or permission to merge lessons.
