from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RENDERER = PROJECT_ROOT / "tools" / "render_skills.py"
SKILL_NAMES = ("record-agent-failure", "recall-failure-lessons")


def _copy_root(target: Path) -> None:
    for name in SKILL_NAMES:
        destination = target / "skills" / name
        destination.mkdir(parents=True)
        for filename in ("contract.json", "SKILL.md"):
            shutil.copy2(PROJECT_ROOT / "skills" / name / filename, destination / filename)


def _run(root: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(RENDERER), "--root", str(root)]
    if check:
        command.append("--check")
    return subprocess.run(command, capture_output=True, text=True, check=False)


def test_contracts_encode_the_fast_path_invariants() -> None:
    record = json.loads(
        (PROJECT_ROOT / "skills/record-agent-failure/contract.json").read_text()
    )
    recall = json.loads(
        (PROJECT_ROOT / "skills/recall-failure-lessons/contract.json").read_text()
    )

    assert record["policy"]["tool"] == "remember_failure"
    assert record["policy"]["single_call"] is True
    assert record["policy"]["record_qualification_attempts"] is True
    assert record["policy"]["fallback_call_limit"] == 1
    assert record["policy"]["inspect_implementation"] is False
    assert record["policy"]["inspect_database"] is False
    assert record["policy"]["temporary_files"] is False
    assert recall["policy"]["tool"] == "recall_failure_lessons"
    assert recall["policy"]["call_limit"] == 1
    assert recall["policy"]["maximum_top_k"] == 3


def test_check_detects_byte_level_artifact_drift(tmp_path: Path) -> None:
    _copy_root(tmp_path)
    artifact = tmp_path / "skills" / SKILL_NAMES[0] / "SKILL.md"
    artifact.write_bytes(artifact.read_bytes().replace(b"\n", b"\r\n"))

    completed = _run(tmp_path)

    assert completed.returncode == 1
    assert "SKILL.md" in completed.stderr


@pytest.mark.parametrize("managed", ["contract.json", "SKILL.md"])
def test_managed_file_symlinks_are_rejected(tmp_path: Path, managed: str) -> None:
    _copy_root(tmp_path)
    path = tmp_path / "skills" / SKILL_NAMES[0] / managed
    external = tmp_path / f"external-{managed}"
    external.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(external)

    completed = _run(tmp_path, check=managed == "contract.json")

    assert completed.returncode == 2
    assert external.read_bytes()


def test_root_and_skill_directory_symlinks_are_rejected(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    _copy_root(real_root)
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)

    assert _run(linked_root).returncode == 2

    external_skill = tmp_path / "external-skill"
    skill = real_root / "skills" / SKILL_NAMES[0]
    skill.rename(external_skill)
    skill.symlink_to(external_skill, target_is_directory=True)
    assert _run(real_root).returncode == 2


def test_invalid_contract_prevents_all_artifact_writes(tmp_path: Path) -> None:
    _copy_root(tmp_path)
    artifacts = {
        name: (tmp_path / "skills" / name / "SKILL.md").read_bytes()
        for name in SKILL_NAMES
    }
    contract_path = tmp_path / "skills" / SKILL_NAMES[1] / "contract.json"
    contract = json.loads(contract_path.read_text())
    contract["policy"]["call_limit"] = 2
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    completed = _run(tmp_path, check=False)

    assert completed.returncode == 2
    assert artifacts == {
        name: (tmp_path / "skills" / name / "SKILL.md").read_bytes()
        for name in SKILL_NAMES
    }


def test_renderer_has_no_machine_paths_and_uses_private_atomic_files() -> None:
    source = RENDERER.read_text(encoding="utf-8")

    assert "/Users/" not in source
    assert "mkstemp" in source
    assert "os.replace" in source
    assert "0o644" in source
    assert os.path.isabs(str(RENDERER))
