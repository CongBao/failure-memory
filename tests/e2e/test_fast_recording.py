from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
LAUNCHER = REPOSITORY / "scripts" / "failure_memory_cli.py"


def _payload() -> dict[str, object]:
    return {
        "summary": "The agent searched internals instead of using the public operation.",
        "classification": "real_failure",
        "expectation": {
            "invariant": "Use the installed public operation directly.",
            "source": "repository_contract",
            "evidence": "The loaded skill named the public operation before the outcome.",
        },
        "observed": {
            "outcome": "The agent inspected source, SQLite, and a private function.",
            "impact": "The workflow consumed many commands and unnecessary context.",
        },
        "cause": {
            "layer": "tool_contract",
            "failure_mode": "not_loaded",
            "component": "Failure Memory MCP",
            "evidence": "The harness did not expose the registered operation.",
            "recommended_change": "Provide one stable CLI fallback.",
            "verification": "Replay from a harness without native MCP registration.",
            "confidence": "high",
        },
        "lesson": {
            "rule": "Use one public operation without implementation discovery.",
            "prevention": "Invoke the stable fallback when MCP is unavailable.",
            "verification": "Observe one call and no source or database inspection.",
        },
    }


def _remember(data_root: Path, harness: str) -> tuple[dict[str, object], float]:
    environment = {
        **os.environ,
        "FAILURE_MEMORY_HOME": str(data_root),
        "FAILURE_MEMORY_HARNESS": harness,
        "PYTHONPATH": "",
    }
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, str(LAUNCHER), "remember", "--stdin"],
        input=json.dumps(_payload()),
        text=True,
        capture_output=True,
        check=False,
        cwd=REPOSITORY,
        env=environment,
        timeout=10,
    )
    elapsed = time.monotonic() - started
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout.count("\n") == 1
    return json.loads(completed.stdout), elapsed


def test_transcript_regression_is_one_call_and_reuses_global_memory(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "global-memory"

    first, first_elapsed = _remember(data_root, "copilot-vscode")
    second, second_elapsed = _remember(data_root, "generic")

    assert first["status"] == "recorded"
    assert first["deduplication_status"] == "distinct"
    assert second["status"] == "recorded"
    assert second["deduplication_status"] == "exact_reuse"
    assert second["lesson_id"] == first["lesson_id"]
    assert second["lesson_version_id"] == first["lesson_version_id"]
    assert first_elapsed < 5
    assert second_elapsed < 5
    assert len(list(data_root.rglob("failure-memory.sqlite3"))) == 1
