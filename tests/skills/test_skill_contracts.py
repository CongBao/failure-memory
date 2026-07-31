from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT_ROOT / "skills"
RENDERER_PATH = PROJECT_ROOT / "tools" / "render_skills.py"
SKILL_NAMES = ("record-agent-failure", "recall-failure-lessons")


def _load_renderer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("failure_memory_skill_renderer", RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _contract(name: str) -> dict[str, object]:
    value = json.loads((SKILLS_ROOT / name / "contract.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_contracts_render_exact_checked_in_skills(name: str) -> None:
    renderer = _load_renderer()
    contract = _contract(name)

    renderer.validate_contract(contract)

    assert (SKILLS_ROOT / name / "SKILL.md").read_text(
        encoding="utf-8"
    ) == renderer.render_skill(contract)


def test_recording_skill_is_one_call_and_forbids_tool_discovery() -> None:
    content = (SKILLS_ROOT / "record-agent-failure" / "SKILL.md").read_text(encoding="utf-8")

    assert "Call `remember_failure` exactly once for every classification" in content
    assert "execute it once" in content
    assert "Never search for the plugin" in content
    assert "inspect source or SQLite" in content
    assert "create temporary files" in content
    assert "evaluate_failure_candidate" not in content
    assert "diagnose_failure_cause" not in content
    assert "review_failure_recording" not in content
    assert "record_failure_incident" not in content
    assert len(content.split()) <= 350


def test_recall_skill_is_one_bounded_call_with_one_fallback() -> None:
    content = (SKILLS_ROOT / "recall-failure-lessons" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "Call `recall_failure_lessons` once" in content
    assert "Do not broaden or retry" in content
    assert "execute it once" in content
    assert "at most three" in content
    assert len(content.split()) <= 220


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_bundled_skill_fallback_is_executable_and_resolves_plugin(name: str) -> None:
    launcher = SKILLS_ROOT / name / "scripts" / "failure_memory_cli.py"

    assert launcher.is_file()
    assert os.access(launcher, os.X_OK)
    completed = subprocess.run(
        [sys.executable, str(launcher), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "remember" in completed.stdout


def test_renderer_check_is_deterministic_and_read_only(tmp_path: Path) -> None:
    for name in SKILL_NAMES:
        target = tmp_path / "skills" / name
        target.mkdir(parents=True)
        shutil.copy2(SKILLS_ROOT / name / "contract.json", target / "contract.json")
    command = [sys.executable, str(RENDERER_PATH), "--root", str(tmp_path)]

    assert subprocess.run(command, check=False).returncode == 0
    before = {
        name: (tmp_path / "skills" / name / "SKILL.md").read_bytes()
        for name in SKILL_NAMES
    }
    assert subprocess.run([*command, "--check"], check=False).returncode == 0
    assert before == {
        name: (tmp_path / "skills" / name / "SKILL.md").read_bytes()
        for name in SKILL_NAMES
    }


def test_renderer_rejects_a_multi_call_recording_contract() -> None:
    renderer = _load_renderer()
    contract = _contract("record-agent-failure")
    policy = contract["policy"]
    assert isinstance(policy, dict)
    policy["single_call"] = False

    with pytest.raises(renderer.ContractError):
        renderer.validate_contract(contract)


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_generated_frontmatter_is_minimal_and_valid(name: str) -> None:
    content = (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"---\n(?P<header>.*?)\n---\n", content, re.DOTALL)
    assert match is not None
    keys = {
        line.partition(":")[0]
        for line in match.group("header").splitlines()
        if ":" in line
    }
    assert keys == {"name", "description"}


def test_forward_test_evidence_binds_current_skills_and_scenarios() -> None:
    evidence = json.loads(
        (PROJECT_ROOT / "tests/skills/pressure-evidence.json").read_text(encoding="utf-8")
    )

    pressure = evidence["pressure"]
    assert _sha256(PROJECT_ROOT / pressure["scenarios_path"]) == pressure["scenarios_sha256"]
    assert _sha256(PROJECT_ROOT / pressure["results_path"]) == pressure["results_sha256"]
    for name in SKILL_NAMES:
        skill = evidence["skills"][name]
        assert _sha256(SKILLS_ROOT / name / "contract.json") == skill["contract_sha256"]
        assert _sha256(SKILLS_ROOT / name / "SKILL.md") == skill["skill_sha256"]
        assert _sha256(RENDERER_PATH) == skill["renderer_sha256"]
        assert skill["final_qualifying"]["passed"] == skill["final_qualifying"]["total"]
