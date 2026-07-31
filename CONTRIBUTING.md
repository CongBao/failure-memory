# Contributing

Contributions are welcome. Keep changes small, testable, and consistent with the
local-first privacy boundary.

## Development setup

Requirements:

- Python 3.13 or newer;
- Git;
- [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv build
```

## Project conventions

- Keep domain and application layers independent of concrete vector databases and agent
  harnesses.
- Put database, embedding, model, and index implementations under adapters.
- Preserve the qualification, diagnosis, exact-review, and persistence gates behind the
  one-call recording boundary.
- Keep causal assessments evidence-bounded; store an explicit unknown instead of
  assigning blame without inspectable support.
- Treat incidents as immutable and lessons as versioned, proposed guidance.
- Never add real credentials, raw prompts, personal data, runtime databases, or absolute
  contributor paths to fixtures or documentation.
- Use synthetic, obviously non-production values in redaction tests.
- Update user-facing documentation when behavior, requirements, or limitations change.

## Skills

`skills/*/SKILL.md` files are generated from their adjacent `contract.json` files. Change
the contract and `tools/render_skills.py`, then regenerate:

```bash
uv run python tools/render_skills.py
uv run python tools/render_skills.py --check
```

Validate each skill with the official skill validator when it is available in your Codex
installation.

## Codex plugin bundle

Build and test the installable projection:

```bash
uv run python packaging/build_codex.py
uv run pytest tests/packaging
```

The builder performs no deletion. It retains replaced output as a recoverable rollback
and reports unresolved artifacts instead of overwriting them.
A release operator may move obsolete rollbacks to Trash after validation.
A hostile process running as the same user is outside the builder's security boundary.

## Pull requests

Describe:

- the user-visible problem;
- the safety or privacy implications;
- tests added or changed;
- compatibility and migration considerations;
- any capability that remains intentionally unsupported.

Do not include generated runtime state or packaging output.
