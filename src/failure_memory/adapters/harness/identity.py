from __future__ import annotations

import re
from collections.abc import Mapping

SUPPORTED_HARNESSES = ("codex", "claude-code", "copilot", "cursor", "local")


def detect_harness(env: Mapping[str, str]) -> str:
    """Identify the active host without allowing plugin data to select storage."""
    explicit = env.get("FAILURE_MEMORY_HARNESS", "").strip().casefold()
    if explicit:
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", explicit) is None:
            raise ValueError(f"unsupported failure-memory harness: {explicit}")
        return explicit
    if env.get("COPILOT_PLUGIN_DATA") or env.get("COPILOT_PLUGIN_ROOT"):
        return "copilot"
    if env.get("CURSOR_PLUGIN_DATA") or env.get("CURSOR_PLUGIN_ROOT"):
        return "cursor"
    # Codex exports both PLUGIN_* and CLAUDE_PLUGIN_* compatibility variables.
    if env.get("PLUGIN_ROOT") or env.get("PLUGIN_DATA"):
        return "codex"
    if env.get("CLAUDE_PLUGIN_ROOT") or env.get("CLAUDE_PLUGIN_DATA"):
        return "claude-code"
    return "local"
