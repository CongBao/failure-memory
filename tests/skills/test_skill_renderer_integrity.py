"""Security and evidence tests for generated failure-memory skills.

Expected digests and contract decisions are derived independently from the renderer.
The production break named by this suite is a contract, template, or filesystem change
that can silently alter generated guidance or write outside the managed plugin tree.
"""

from __future__ import annotations

import copy
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
from typing import Any, Final

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parents[2]
RENDERER_PATH = PROJECT_ROOT / "tools" / "render_skills.py"
EVIDENCE_PATH = PROJECT_ROOT / "tests" / "skills" / "pressure-evidence.json"
SCENARIOS_PATH = PROJECT_ROOT / "tests" / "skills" / "pressure-scenarios.md"
RESULTS_PATH = PROJECT_ROOT / "tests" / "skills" / "pressure-results.md"
SKILL_NAMES: Final = ("record-agent-failure", "recall-failure-lessons")
GENERATED_NOTE = re.compile(
    rb"<!-- Generated from contract\.json by tools/render_skills\.py; "
    rb"policy sha256=[0-9a-f]{64}; behavior sha256=[0-9a-f]{64}; "
    rb"renderer sha256=[0-9a-f]{64}; DO NOT EDIT SKILL\.md MANUALLY\. -->\n\n"
)
HISTORICAL_GENERATED_NOTE = re.compile(
    rb"<!-- Generated from contract\.json by tools/render_skills\.py; "
    rb"policy sha256=[0-9a-f]{64}; DO NOT EDIT SKILL\.md MANUALLY\. -->\n\n"
)


def _load_renderer(path: Path = RENDERER_PATH) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"failure_memory_renderer_{hash(path)}",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _contract_path(root: Path, skill: str) -> Path:
    return root / "skills" / skill / "contract.json"


def _skill_path(root: Path, skill: str) -> Path:
    return root / "skills" / skill / "SKILL.md"


def _load_contract(root: Path, skill: str) -> dict[str, Any]:
    document = json.loads(_contract_path(root, skill).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _copy_render_root(root: Path, *, renderer: bool = False) -> Path:
    for skill in SKILL_NAMES:
        target = root / "skills" / skill
        target.mkdir(parents=True)
        shutil.copy2(_contract_path(PROJECT_ROOT, skill), target / "contract.json")
        shutil.copy2(_skill_path(PROJECT_ROOT, skill), target / "SKILL.md")
    if renderer:
        tools = root / "tools"
        tools.mkdir()
        shutil.copy2(RENDERER_PATH, tools / "render_skills.py")
        evidence = root / "tests" / "skills"
        evidence.mkdir(parents=True)
        if EVIDENCE_PATH.exists():
            shutil.copy2(EVIDENCE_PATH, evidence / EVIDENCE_PATH.name)
        shutil.copy2(SCENARIOS_PATH, evidence / SCENARIOS_PATH.name)
        shutil.copy2(RESULTS_PATH, evidence / RESULTS_PATH.name)
        return tools / "render_skills.py"
    return RENDERER_PATH


def _run_renderer(
    root: Path,
    *,
    check: bool = False,
    renderer_path: Path = RENDERER_PATH,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(renderer_path), "--root", str(root)]
    if check:
        command.append("--check")
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_behavior_bytes(artifact: bytes, source: object) -> bytes:
    current_stripped, current_count = GENERATED_NOTE.subn(b"", artifact)
    historical_stripped, historical_count = HISTORICAL_GENERATED_NOTE.subn(b"", artifact)
    assert current_count + historical_count == 1, (
        f"{source}: expected exactly one generated metadata note"
    )
    return current_stripped if current_count else historical_stripped


def _behavior_bytes(path: Path) -> bytes:
    return _normalized_behavior_bytes(path.read_bytes(), path)


def _git_output(*arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *arguments],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise ValueError("git object lookup failed")
    return completed.stdout


def _reviewed_behavior_bytes(skill: str, reference: str) -> bytes:
    resolved = (
        _git_output(
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{reference}^{{commit}}",
        )
        .decode("ascii")
        .strip()
    )
    if re.fullmatch(r"[0-9a-f]{40,64}", resolved) is None:
        raise ValueError("reviewed commit did not resolve to an object id")
    artifact = _git_output(
        "cat-file",
        "blob",
        f"{resolved}:skills/{skill}/SKILL.md",
    )
    return _normalized_behavior_bytes(artifact, f"{resolved}:{skill}")


def _binding_payload(
    manifest: dict[str, Any],
    skill: str,
    *,
    policy_sha256: str | None = None,
    behavior_sha256: str | None = None,
    renderer_sha256: str | None = None,
) -> dict[str, Any]:
    entry = manifest["skills"][skill]
    return {
        "policy_sha256": policy_sha256 or entry["policy_sha256"],
        "behavior_sha256": behavior_sha256 or entry["behavior_sha256"],
        "renderer_sha256": renderer_sha256 or entry["renderer_sha256"],
        "scenarios_sha256": manifest["pressure"]["scenarios_sha256"],
        "results_sha256": manifest["pressure"]["results_sha256"],
        "pressure_families": entry["pressure_families"],
        "final_qualifying": entry["final_qualifying"],
        "adjudication": entry["adjudication"],
    }


def _evidence_errors(root: Path, renderer_path: Path) -> list[str]:
    manifest_path = root / "tests" / "skills" / "pressure-evidence.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    renderer_digest = _file_sha256(renderer_path)
    if manifest != {
        "evidence_version": 1,
        "algorithm": "sha256",
        "behavior_digest_definition": (
            "sha256 of exact SKILL.md bytes after removing exactly one generated "
            "metadata note block and its following separator"
        ),
        "renderer_digest_definition": "sha256 of exact tools/render_skills.py bytes",
        "pressure": {
            "scenarios_path": "tests/skills/pressure-scenarios.md",
            "scenarios_sha256": manifest.get("pressure", {}).get("scenarios_sha256"),
            "results_path": "tests/skills/pressure-results.md",
            "results_sha256": manifest.get("pressure", {}).get("results_sha256"),
        },
        "skills": manifest.get("skills"),
    }:
        errors.append("manifest_schema")
        return errors
    pressure = manifest["pressure"]
    if pressure["scenarios_sha256"] != _file_sha256(root / pressure["scenarios_path"]):
        errors.append("scenarios_sha256")
    if pressure["results_sha256"] != _file_sha256(root / pressure["results_path"]):
        errors.append("results_sha256")

    expected_families = {
        "record-agent-failure": (["R1", "R2", "R3"], {"passed": 15, "total": 15}),
        "recall-failure-lessons": (["C1", "C2", "C3"], {"passed": 3, "total": 3}),
    }
    for skill, (families, final_qualifying) in expected_families.items():
        entry = manifest["skills"].get(skill)
        if not isinstance(entry, dict):
            errors.append(f"{skill}.entry")
            continue
        policy_digest = _canonical_sha256(_load_contract(root, skill))
        behavior_bytes = _behavior_bytes(_skill_path(root, skill))
        behavior_digest = hashlib.sha256(behavior_bytes).hexdigest()
        adjudication = entry.get("adjudication")
        expected_entry = {
            "policy_sha256": policy_digest,
            "behavior_sha256": behavior_digest,
            "renderer_sha256": renderer_digest,
            "pressure_families": families,
            "final_qualifying": final_qualifying,
            "adjudication": adjudication,
            "binding_sha256": entry.get("binding_sha256"),
        }
        for key in ("policy_sha256", "behavior_sha256", "renderer_sha256"):
            if entry.get(key) != expected_entry[key]:
                errors.append(f"{skill}.{key}")
        if entry.get("pressure_families") != families:
            errors.append(f"{skill}.pressure_families")
        if entry.get("final_qualifying") != final_qualifying:
            errors.append(f"{skill}.final_qualifying")
        if (
            not isinstance(adjudication, dict)
            or set(adjudication)
            != {
                "kind",
                "basis",
                "reviewed_against_commit",
                "pressure_rerun",
            }
            or not isinstance(adjudication.get("reviewed_against_commit"), str)
            or not adjudication["reviewed_against_commit"]
            or type(adjudication.get("pressure_rerun")) is not bool
        ):
            errors.append(f"{skill}.adjudication")
        elif adjudication["pressure_rerun"]:
            if (
                adjudication.get("kind") != "pressure_rerun"
                or adjudication.get("basis") != "fresh_forward_test"
            ):
                errors.append(f"{skill}.adjudication")
        elif (
            adjudication.get("kind") != "unchanged_semantics"
            or adjudication.get("basis") != "exact_behavior_bytes_unchanged"
        ):
            errors.append(f"{skill}.adjudication")
        else:
            try:
                reviewed_behavior = _reviewed_behavior_bytes(
                    skill,
                    adjudication["reviewed_against_commit"],
                )
            except (AssertionError, OSError, UnicodeError, ValueError):
                errors.append(f"{skill}.adjudication.reviewed_against_commit")
            else:
                if reviewed_behavior != behavior_bytes:
                    errors.append(f"{skill}.adjudication.pressure_rerun")
        if entry.get("binding_sha256") != _canonical_sha256(
            _binding_payload(
                manifest,
                skill,
                policy_sha256=policy_digest,
                behavior_sha256=behavior_digest,
                renderer_sha256=renderer_digest,
            )
        ):
            errors.append(f"{skill}.binding_sha256")
        if set(entry) != set(expected_entry):
            errors.append(f"{skill}.schema")
    return errors


def _leaf_paths(value: object, path: str = "$") -> set[str]:
    if isinstance(value, dict):
        return {
            leaf for key, child in value.items() for leaf in _leaf_paths(child, f"{path}.{key}")
        }
    if isinstance(value, list):
        return {
            leaf
            for index, child in enumerate(value)
            for leaf in _leaf_paths(child, f"{path}[{index}]")
        }
    return {path}


def _mutated_leaf_value(value: object) -> object:
    if type(value) is bool:
        return not value
    if type(value) is int:
        return value + 1
    if isinstance(value, str):
        return f"{value}-mutated"
    raise AssertionError(f"unsupported leaf type: {type(value).__name__}")


def _set_leaf(document: object, path: str, value: object) -> None:
    components = re.findall(r"(?:^|\.)([^.\[\]]+)|\[(\d+)\]", path.removeprefix("$"))
    target = document
    flattened: list[str | int] = [int(index) if index else key for key, index in components]
    for component in flattened[:-1]:
        target = target[component]  # type: ignore[index]
    target[flattened[-1]] = value  # type: ignore[index]


def _get_leaf(document: object, path: str) -> object:
    components = re.findall(r"(?:^|\.)([^.\[\]]+)|\[(\d+)\]", path.removeprefix("$"))
    target = document
    for key, index in components:
        target = target[int(index) if index else key]  # type: ignore[index]
    return target


def test_contracts_represent_all_required_record_and_recall_rules() -> None:
    record = _load_contract(PROJECT_ROOT, "record-agent-failure")["policy"]
    recall = _load_contract(PROJECT_ROOT, "recall-failure-lessons")["policy"]

    assert record["evidence"] == {
        "establish_chronology": True,
        "fields": [
            "expectation_source",
            "availability_time",
            "observed_outcome",
            "inspectable_mismatch",
            "impact_or_recurrence_risk",
            "controllability_with_then_available_information",
            "durable_prevention_value",
        ],
        "prohibit_invention": True,
        "draft_exclusions": [
            "raw_prompts",
            "secrets",
            "unnecessary_user_text",
        ],
    }
    assert record["classification"]["cardinality"] == 1
    assert [item["id"] for item in record["classification"]["classes"]] == [
        "requirement_update",
        "requirement_clarification",
        "preference_update",
        "real_failure",
        "mixed",
        "uncertain",
    ]
    assert record["accepted_capture"] == {
        "draft_only_after": "accept",
        "incident_mutability": "immutable",
        "lesson_count": 1,
        "lesson_authority": "proposed",
        "source_portion": "accepted_failure_portion",
        "record_capture_id_state": "accepted",
        "record_drafts_sanitized": True,
    }
    assert record["result_reporting"] == {
        "distinguish": [
            "created_new_proposed_lesson",
            "reused_exact_existing_lesson",
        ],
        "cite_returned_identifiers": True,
        "allow_verified_description": False,
    }
    assert recall["evidence"] == {
        "context_field": "text",
        "discriminator_fields": [
            "expected_invariant",
            "controllable_cause",
            "prevention_action",
            "component",
        ],
        "minimum_discriminators": 1,
        "source": "current_task_evidence",
        "allow_inference": False,
        "query_exclusions": ["raw_prompts", "secrets", "unnecessary_user_text"],
        "forbidden_fill_basis": [
            "resemblance_alone",
            "recurrence_anxiety",
            "authority_guess",
        ],
    }
    assert recall["lookup"] == {
        "max_calls": 1,
        "default_mode": "auto",
        "exact_first": True,
        "allow_modes": ["auto", "exact", "lexical", "semantic", "hybrid"],
        "default_top_k": 3,
        "hard_max_top_k": 5,
        "allow_bulk": False,
        "allow_query_broadening": False,
    }
    assert recall["fallback"]["automatic_install"] is False
    assert recall["feedback"]["false_positive_supported"] is True
    assert "missed_relevant" in recall["feedback"]["allowed_outcomes"]
    assert recall["result"]["max_lessons"] == 3
    assert recall["result"]["identifier_required"] is True
    assert recall["result"]["evidence_required"] is True
    assert recall["result"]["validate_against_current_task"] is True
    assert recall["result"]["actions_to_validate"] == ["prevention", "verification"]


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_renderer_trace_consumes_every_contract_leaf(skill: str) -> None:
    renderer = _load_renderer()
    assert hasattr(renderer, "render_skill_with_trace")
    contract = _load_contract(PROJECT_ROOT, skill)

    rendered = renderer.render_skill_with_trace(contract)
    consumed = set(rendered.behavior_paths) | set(rendered.metadata_paths)

    assert consumed == _leaf_paths(contract)
    assert set(rendered.behavior_paths).isdisjoint(rendered.metadata_paths)
    allowed_metadata = {
        "$.contract_version",
        "$.policy_kind",
        *{
            path
            for path in _leaf_paths(contract)
            if re.fullmatch(r"\$\.policy\.workflow\.edges\[\d+\]\.id", path)
        },
    }
    assert set(rendered.metadata_paths) == allowed_metadata


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_production_validator_rejects_every_mutated_contract_leaf(skill: str) -> None:
    renderer = _load_renderer()
    assert hasattr(renderer, "validate_contract")
    contract = _load_contract(PROJECT_ROOT, skill)

    for path in sorted(_leaf_paths(contract)):
        mutated = copy.deepcopy(contract)
        _set_leaf(mutated, path, _mutated_leaf_value(_get_leaf(mutated, path)))
        with pytest.raises(renderer.ContractError, match=re.escape(path)):
            renderer.validate_contract(skill, mutated)


@pytest.mark.parametrize(
    ("skill", "mutation"),
    [
        ("record-agent-failure", "missing"),
        ("record-agent-failure", "extra"),
        ("record-agent-failure", "wrong_type"),
        ("recall-failure-lessons", "missing"),
        ("recall-failure-lessons", "extra"),
        ("recall-failure-lessons", "wrong_value"),
    ],
)
def test_production_validator_rejects_schema_and_value_drift(
    skill: str,
    mutation: str,
) -> None:
    renderer = _load_renderer()
    assert hasattr(renderer, "validate_contract")
    contract = _load_contract(PROJECT_ROOT, skill)
    if mutation == "missing":
        del contract["skill"]["description"]
    elif mutation == "extra":
        contract["policy"]["unbounded"] = "silently accepted"
    elif mutation == "wrong_type":
        contract["contract_version"] = "2"
    else:
        contract["policy"]["lookup"]["max_calls"] = 2

    with pytest.raises(renderer.ContractError):
        renderer.validate_contract(skill, contract)


def test_all_contracts_validate_before_any_artifact_write(tmp_path: Path) -> None:
    _copy_render_root(tmp_path)
    sentinel = b"first artifact must remain untouched\n"
    first_artifact = _skill_path(tmp_path, SKILL_NAMES[0])
    first_artifact.write_bytes(sentinel)
    second = _load_contract(tmp_path, SKILL_NAMES[1])
    second["policy"]["lookup"]["max_calls"] = 2
    _contract_path(tmp_path, SKILL_NAMES[1]).write_text(
        json.dumps(second),
        encoding="utf-8",
    )

    completed = _run_renderer(tmp_path)

    assert completed.returncode != 0
    assert first_artifact.read_bytes() == sentinel


def test_malformed_second_contract_leaves_first_artifact_unchanged(
    tmp_path: Path,
) -> None:
    _copy_render_root(tmp_path)
    sentinel = b"first artifact must remain untouched\n"
    first_artifact = _skill_path(tmp_path, SKILL_NAMES[0])
    first_artifact.write_bytes(sentinel)
    _contract_path(tmp_path, SKILL_NAMES[1]).write_text("{", encoding="utf-8")

    completed = _run_renderer(tmp_path)

    assert completed.returncode != 0
    assert first_artifact.read_bytes() == sentinel


def test_invalid_workflow_edge_leaves_all_artifacts_unchanged(tmp_path: Path) -> None:
    _copy_render_root(tmp_path)
    before = {skill: _skill_path(tmp_path, skill).read_bytes() for skill in SKILL_NAMES}
    second = _load_contract(tmp_path, SKILL_NAMES[1])
    second["policy"]["workflow"]["edges"][0]["to"] = "return_lesson"
    _contract_path(tmp_path, SKILL_NAMES[1]).write_text(
        json.dumps(second),
        encoding="utf-8",
    )

    completed = _run_renderer(tmp_path)

    assert completed.returncode != 0
    assert before == {skill: _skill_path(tmp_path, skill).read_bytes() for skill in SKILL_NAMES}


def test_contract_symlink_is_rejected(tmp_path: Path) -> None:
    _copy_render_root(tmp_path)
    contract = _contract_path(tmp_path, SKILL_NAMES[1])
    shadow = tmp_path / "shadow-contract.json"
    shutil.copy2(contract, shadow)
    contract.unlink()
    contract.symlink_to(shadow)

    completed = _run_renderer(tmp_path, check=True)

    assert completed.returncode != 0
    assert "symlink" in completed.stderr.lower()


@pytest.mark.parametrize("check", [False, True])
def test_artifact_symlink_is_rejected_without_touching_external_target(
    tmp_path: Path,
    check: bool,
) -> None:
    _copy_render_root(tmp_path)
    artifact = _skill_path(tmp_path, SKILL_NAMES[0])
    external = tmp_path / "external-sentinel.md"
    sentinel = artifact.read_bytes()
    external.write_bytes(sentinel)
    artifact.unlink()
    artifact.symlink_to(external)

    completed = _run_renderer(tmp_path, check=check)

    assert completed.returncode != 0
    assert "symlink" in completed.stderr.lower()
    assert external.read_bytes() == sentinel


def test_root_symlink_is_rejected(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    _copy_render_root(real_root)
    root_link = tmp_path / "linked-root"
    root_link.symlink_to(real_root, target_is_directory=True)

    completed = _run_renderer(root_link, check=True)

    assert completed.returncode != 0
    assert "root" in completed.stderr.lower()
    assert "symlink" in completed.stderr.lower()


def test_skill_directory_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _copy_render_root(root)
    original = root / "skills" / SKILL_NAMES[0]
    external = tmp_path / "external-skill"
    original.rename(external)
    original.symlink_to(external, target_is_directory=True)

    completed = _run_renderer(root, check=True)

    assert completed.returncode != 0
    assert "symlink" in completed.stderr.lower()


def test_nonregular_artifact_is_rejected_without_opening_fifo(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    renderer = _load_renderer()
    assert hasattr(renderer, "_read_regular_bytes")
    fifo = tmp_path / "artifact.fifo"
    os.mkfifo(fifo)

    with pytest.raises(renderer.PathSecurityError, match="regular"):
        renderer._read_regular_bytes(fifo, "artifact")


def test_managed_path_rejects_absolute_and_traversal_components(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    assert hasattr(renderer, "_validated_root")
    assert hasattr(renderer, "_managed_path")
    root = renderer._validated_root(tmp_path)

    with pytest.raises(renderer.PathSecurityError):
        renderer._managed_path(root, Path("/tmp/external"))
    with pytest.raises(renderer.PathSecurityError):
        renderer._managed_path(root, Path("skills/../external"))


def test_check_mode_detects_crlf_byte_tamper(tmp_path: Path) -> None:
    _copy_render_root(tmp_path)
    artifact = _skill_path(tmp_path, SKILL_NAMES[0])
    artifact.write_bytes(artifact.read_bytes().replace(b"\n", b"\r\n"))

    completed = _run_renderer(tmp_path, check=True)

    assert completed.returncode == 1
    assert f"skills/{SKILL_NAMES[0]}/SKILL.md" in completed.stderr


def test_atomic_write_cleans_exclusive_temp_after_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    assert hasattr(renderer, "_atomic_write")
    destination = tmp_path / "SKILL.md"
    original = b"original\n"
    destination.write_bytes(original)

    def fail_replace(source: object, target: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(renderer.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        renderer._atomic_write(destination, b"replacement\n")

    assert destination.read_bytes() == original
    assert list(tmp_path.glob(".failure-memory-*.tmp")) == []


def test_atomic_batch_rolls_back_first_artifact_when_second_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    assert hasattr(renderer, "_atomic_write_all")
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    originals = {first: b"first original\n", second: b"second original\n"}
    for path, content in originals.items():
        path.write_bytes(content)
    real_replace = renderer.os.replace
    calls = 0

    def fail_second_replace(source: object, target: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second replace failure")
        real_replace(source, target)

    monkeypatch.setattr(renderer.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="injected second"):
        renderer._atomic_write_all(
            [(first, b"first replacement\n"), (second, b"second replacement\n")]
        )

    assert {path: path.read_bytes() for path in originals} == originals
    assert list(tmp_path.glob(".failure-memory-*.tmp")) == []


def test_atomic_write_cleans_temp_on_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    destination = tmp_path / "SKILL.md"
    original = b"original\n"
    destination.write_bytes(original)

    def interrupt_replace(source: object, target: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(renderer.os, "replace", interrupt_replace)
    with pytest.raises(KeyboardInterrupt):
        renderer._atomic_write(destination, b"replacement\n")

    assert destination.read_bytes() == original
    assert list(tmp_path.glob(".failure-memory-*.tmp")) == []


def test_pressure_evidence_manifest_binds_policy_behavior_and_renderer() -> None:
    assert EVIDENCE_PATH.is_file()
    assert _evidence_errors(PROJECT_ROOT, RENDERER_PATH) == []
    results = RESULTS_PATH.read_text(encoding="utf-8")
    assert "[`pressure-evidence.json`](pressure-evidence.json)" in results


def test_ci_checkout_makes_reviewed_evidence_commits_available(
    tmp_path: Path,
) -> None:
    manifest = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    reviewed_commits = {
        entry["adjudication"]["reviewed_against_commit"]
        for entry in manifest["skills"].values()
        if not entry["adjudication"]["pressure_rerun"]
    }
    assert reviewed_commits

    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["test"]["steps"]
    checkout_steps = [
        step
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("uses"), str)
        and step["uses"].startswith("actions/checkout@")
    ]
    assert len(checkout_steps) == 1
    checkout_options = checkout_steps[0].get("with", {})
    assert isinstance(checkout_options, dict)
    fetch_depth = checkout_options.get("fetch-depth", 1)
    assert type(fetch_depth) is int and fetch_depth >= 0

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "init", "--quiet", str(checkout)],
        check=True,
        timeout=10,
    )
    fetch = ["git", "-C", str(checkout), "fetch", "--quiet"]
    if fetch_depth:
        fetch.append(f"--depth={fetch_depth}")
    fetch.extend([PROJECT_ROOT.as_uri(), "HEAD"])
    fetched = subprocess.run(
        fetch,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert fetched.returncode == 0, fetched.stderr

    unavailable = [
        reference
        for reference in sorted(reviewed_commits)
        if subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "cat-file",
                "-e",
                f"{reference}^{{commit}}",
            ],
            capture_output=True,
            check=False,
            timeout=10,
        ).returncode
        != 0
    ]
    assert unavailable == []


def test_generated_note_carries_independent_binding_identifiers() -> None:
    manifest = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    for skill in SKILL_NAMES:
        artifact = _skill_path(PROJECT_ROOT, skill).read_bytes()
        match = GENERATED_NOTE.search(artifact)
        assert match is not None
        note = match.group().decode("utf-8")
        entry = manifest["skills"][skill]
        assert f"policy sha256={entry['policy_sha256']}" in note
        assert f"behavior sha256={entry['behavior_sha256']}" in note
        assert f"renderer sha256={entry['renderer_sha256']}" in note


def test_template_behavior_change_breaks_evidence_after_regeneration(
    tmp_path: Path,
) -> None:
    renderer_path = _copy_render_root(tmp_path, renderer=True)
    source = renderer_path.read_text(encoding="utf-8")
    mutated = source.replace(
        "Similarity can surface a traceable caution",
        "Similarity always proves the same failure",
        1,
    )
    assert mutated != source
    renderer_path.write_text(mutated, encoding="utf-8")

    completed = _run_renderer(tmp_path, renderer_path=renderer_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    errors = _evidence_errors(tmp_path, renderer_path)
    assert "recall-failure-lessons.behavior_sha256" in errors
    assert "recall-failure-lessons.renderer_sha256" in errors
    assert "recall-failure-lessons.binding_sha256" in errors


def test_unchanged_semantics_adjudication_rejects_historical_behavior_drift(
    tmp_path: Path,
) -> None:
    renderer_path = _copy_render_root(tmp_path, renderer=True)
    source = renderer_path.read_text(encoding="utf-8")
    mutated = source.replace(
        "Similarity can surface a traceable caution",
        "Similarity always proves the same failure",
        1,
    )
    assert mutated != source
    renderer_path.write_text(mutated, encoding="utf-8")

    completed = _run_renderer(tmp_path, renderer_path=renderer_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr

    manifest_path = tmp_path / "tests" / "skills" / "pressure-evidence.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recall_adjudication = manifest["skills"]["recall-failure-lessons"]["adjudication"]
    recall_adjudication.update(
        {
            "kind": "unchanged_semantics",
            "basis": "exact_behavior_bytes_unchanged",
            "pressure_rerun": False,
        }
    )
    renderer_digest = _file_sha256(renderer_path)
    for skill in SKILL_NAMES:
        entry = manifest["skills"][skill]
        entry["policy_sha256"] = _canonical_sha256(_load_contract(tmp_path, skill))
        entry["behavior_sha256"] = hashlib.sha256(
            _behavior_bytes(_skill_path(tmp_path, skill))
        ).hexdigest()
        entry["renderer_sha256"] = renderer_digest
    for skill in SKILL_NAMES:
        manifest["skills"][skill]["binding_sha256"] = _canonical_sha256(
            _binding_payload(manifest, skill)
        )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    assert _evidence_errors(tmp_path, renderer_path) == [
        "recall-failure-lessons.adjudication.pressure_rerun"
    ]
