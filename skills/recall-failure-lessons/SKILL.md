---
name: recall-failure-lessons
description: Use before risky or recurring work, or when a current task resembles a previously recorded failure and the task provides concrete evidence for a bounded lookup.
---

<!-- Generated from contract.json; policy sha256=f38cc5152064279ec63724159888440986859176e47404dac81e99c2c25281fc; DO NOT EDIT SKILL.md MANUALLY. -->

# Recall Failure Lessons

Use one bounded lookup only when the current task provides `text` plus at least one concrete discriminator: `expected_invariant`, `controllable_cause`, `prevention_action`, `component`.

Call `recall_failure_lessons` once with `mode=auto` and `top_k=3`. Do not broaden or retry the query. Apply at most three returned lessons as proposed cautions and validate them against the current task.

If the named tool is unavailable, resolve the bundled [CLI launcher](scripts/failure_memory_cli.py) relative to this skill and execute it once with `recall --input -`, passing the same JSON on standard input. If the launcher is unavailable, continue without memory guidance.

Do not install dependencies during recall or include raw prompts, secrets, unnecessary user text. Similarity is not proof, policy authority, or permission to merge lessons.
