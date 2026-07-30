#!/usr/bin/env python3
"""Emit bounded, non-persistent session guidance for supported agent hosts."""

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
    "failures, then run both qualification and the required generalization review before "
    "any record. Never persist raw prompts, merge automatically, or treat a proposed "
    "lesson as verified."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--harness",
        required=True,
        choices=("codex", "claude-code", "copilot", "cursor"),
    )
    parser.add_argument("--event", required=True, choices=("session-start",))
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
    if arguments.harness in {"codex", "claude-code"}:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _GUIDANCE,
            }
        }
    elif arguments.harness == "copilot":
        output = {"additionalContext": _GUIDANCE}
    else:
        output = {"additional_context": _GUIDANCE}
    json.dump(output, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
