#!/usr/bin/env python3
"""Emit bounded, non-persistent guidance for supported agent-host hooks."""

from __future__ import annotations

import argparse
import json
import sys

_MAX_INPUT_BYTES = 1_048_576
_GUIDANCE = (
    "Failure Memory has two skills. Use recall-failure-lessons once before risky recurring "
    "work when the task has concrete evidence. When feedback challenges an earlier outcome, "
    "use record-agent-failure; it distinguishes new requirements from real failures and "
    "calls remember_failure once. Never persist raw prompts or treat proposed lessons as verified."
)
_PROMPT_GUIDANCE = (
    "Check whether the just-submitted user message challenges an earlier agent outcome. "
    "New requirements, new details, first-time preferences, and ordinary refinements are not "
    "failures. If a prior expectation, controllable mismatch, impact or recurrence risk, and "
    "durable lesson are evidenced, use record-agent-failure and make its one remember_failure "
    "call. Do not copy the raw prompt or infer an uninspectable cause."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--harness",
        required=True,
        choices=(
            "codex",
            "claude-code",
            "copilot-cli",
            "copilot-vscode",
            "cursor",
            "generic",
        ),
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
    elif arguments.harness == "copilot-cli":
        output = {"additionalContext": guidance}
    else:
        output = {"additional_context": guidance}
    json.dump(output, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
