#!/usr/bin/env python3
"""Emit bounded, non-persistent guidance for supported agent-host hooks."""

from __future__ import annotations

import argparse
import json
import sys

_MAX_INPUT_BYTES = 1_048_576
_GUIDANCE = (
    "Failure Memory is available through two separate skills. Before risky or recurring "
    "work, use recall-failure-lessons only with concrete current-task evidence and apply "
    "at most three results as proposed cautions. When feedback challenges an outcome, use "
    "record-agent-failure: distinguish requirement updates and new details from real "
    "failures, then run qualification, evidence-bounded causal diagnosis, and the required "
    "generalization review before any record. Diagnose the root cause at the most specific "
    "inspectable instruction, prompt, hook, tool, application, runtime, or external layer "
    "and recommend where to repair it. Never persist raw prompts, merge automatically, or "
    "treat a proposed lesson as verified."
)
_PROMPT_GUIDANCE = (
    "Check whether the just-submitted user message challenges an earlier agent outcome. "
    "Corrective wording alone is not a failure: new requirements, newly supplied details, "
    "first-time preferences, and ordinary refinements stay in the normal work flow. If the "
    "message evidences a prior expectation, a controllable mismatch, material impact or "
    "recurrence risk, and a durable lesson, use record-agent-failure. After qualification "
    "accepts it, inspect evidence to locate the root cause and repair target before reviewing "
    "similar lessons. Do not copy or persist the raw prompt, and do not infer a cause that "
    "cannot be inspected."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--harness",
        required=True,
        choices=("codex", "claude-code", "copilot", "cursor"),
    )
    parser.add_argument(
        "--event",
        required=True,
        choices=("session-start", "user-prompt-submit"),
    )
    return parser


def _read_input() -> None:
    payload = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(payload) > _MAX_INPUT_BYTES:
        raise ValueError("hook input exceeds the bounded size")
    if payload.strip():
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("hook input must be an object")


def main() -> int:
    arguments = _parser().parse_args()
    try:
        _read_input()
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return 0
    if arguments.event == "user-prompt-submit":
        if arguments.harness not in {"codex", "claude-code"}:
            return 0
        event_name = "UserPromptSubmit"
        guidance = _PROMPT_GUIDANCE
    else:
        event_name = "SessionStart"
        guidance = _GUIDANCE
    if arguments.harness in {"codex", "claude-code"}:
        output = {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": guidance,
            }
        }
    elif arguments.harness == "copilot":
        output = {"additionalContext": guidance}
    else:
        output = {"additional_context": guidance}
    json.dump(output, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
