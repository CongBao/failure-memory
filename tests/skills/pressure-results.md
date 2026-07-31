# Failure Memory skill evaluation

This report records compact, non-private outputs from isolated forward-test agents. No
memory tools were called and no live store was modified. Contributor-specific paths are
shown as skill-relative paths.

## Method

- Scenarios: [`pressure-scenarios.md`](pressure-scenarios.md), revision `r7`.
- Each qualifying sample used a fresh agent with only one generated skill and one
  scenario.
- Agents stated an intended call; they did not execute it.
- Initial R1 and R3 samples skipped telemetry for non-failures. The recording skill was
  clarified to call the qualification gate for every classification, and fresh agents
  passed both reruns.
- Initial C3 lacked recall evidence. The scenario was corrected and rerun with a fresh
  agent.

## Results

| Case | Result | Status |
|---|---|---|
| R1 | `requirement_update`; one `remember_failure` call; YAML was requested only after acceptance | Pass after skill clarification |
| R2 | `mixed`; one `remember_failure` call; raw-prompt retention isolated from new encryption work | Pass |
| R3 | `requirement_update`; one `remember_failure` call; no retroactive diagnostic invariant invented | Pass after skill clarification |
| R4 | `real_failure`; one `remember_failure` call; no manual search, merge, or authority promotion | Pass |
| R5 | adjacent `scripts/failure_memory_cli.py remember --stdin` once; no implementation discovery or retry | Pass |
| C1 | no call because both task text and a concrete discriminator were absent | Pass |
| C2 | one `recall_failure_lessons(mode=auto, top_k=3)` call; matches remain proposed cautions | Pass |
| C3 | adjacent `scripts/failure_memory_cli.py recall --input -` once with the same bounded JSON on stdin | Pass after scenario correction |

## Conclusions

- Requirement updates and hindsight additions reach the qualification telemetry gate but
  do not create lessons.
- Mixed feedback keeps newly introduced work outside the failure portion.
- Genuine failures use one public call; the agent does not orchestrate internal database
  stages.
- Missing native tools cause one adjacent CLI fallback call without plugin discovery,
  direct SQLite access, runtime discovery, retries, or temporary payload files.
- Recall remains evidence-gated, bounded to three proposed cautions, and never treats
  semantic similarity as authority.
