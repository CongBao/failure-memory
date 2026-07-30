# ruff: noqa: E501
"""Independent contracts for generated failure-memory skills.

The literal expectations and graph checks in this file do not import production
validation. SKILL.md files are exact generated artifacts, while behavioral confidence
is bound separately in pressure-evidence.json.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
SKILLS_ROOT = PROJECT_ROOT / "skills"
RENDERER_PATH = PROJECT_ROOT / "tools" / "render_skills.py"
EVIDENCE_PATH = PROJECT_ROOT / "tests" / "skills" / "pressure-evidence.json"
VALIDATOR_OVERRIDE_ENV: Final = "FAILURE_MEMORY_SKILL_VALIDATOR"
RECORD_SKILL = "record-agent-failure"
RECALL_SKILL = "recall-failure-lessons"
SKILL_NAMES: Final = (RECORD_SKILL, RECALL_SKILL)

_EXPECTED_RECORD_JSON = r"""{"contract_version":3,"policy_kind":"record_failure","skill":{"name":"record-agent-failure","description":"Use when a user challenges an agent outcome, reports a missed prior invariant or repeated correction, or explicitly asks the agent to learn from a failure; not for new requirements, newly supplied details, first-time preferences, or ordinary refinement."},"policy":{"core":{"corrective_wording_is_failure_evidence":false,"expectation_time_basis":"before_outcome","required_mismatch_qualities":["material","controllable","durable"]},"evidence":{"establish_chronology":true,"fields":["expectation_source","availability_time","observed_outcome","inspectable_mismatch","impact_or_recurrence_risk","controllability_with_then_available_information","durable_prevention_value"],"prohibit_invention":true,"draft_exclusions":["raw_prompts","secrets","unnecessary_user_text"]},"classification":{"cardinality":1,"tier":"Tier One","classes":[{"id":"requirement_update","criterion":"The requested feature or contract changed after the outcome."},{"id":"requirement_clarification","criterion":"A previously unavailable detail now clarifies the work."},{"id":"preference_update","criterion":"A preference is stated for the first time."},{"id":"real_failure","criterion":"A prior expectation, mismatch, impact/risk, controllability, and durable lesson are all evidenced."},{"id":"mixed","criterion":"Feedback contains both a genuine prior-invariant mismatch and new work."},{"id":"uncertain","criterion":"Chronology or evidence cannot establish the criteria; do not guess."}]},"tools":{"evaluate":"evaluate_failure_candidate","review":"review_failure_recording","record":"record_failure_incident"},"workflow":{"entry":"classify","tool_order":["evaluate_failure_candidate","review_failure_recording","record_failure_incident"],"edges":[{"id":"classify_to_evaluate","from":"classify","to":"evaluate","condition":"always"},{"id":"evaluate_accept_to_review","from":"evaluate","to":"review","condition":"decision == accept"},{"id":"review_to_record","from":"review","to":"record","condition":"explicit disposition selected"},{"id":"evaluate_reject_defer_to_terminal","from":"evaluate","to":"terminal_no_write","condition":"decision in [reject, defer]"}]},"evaluation":{"every_classification":true,"pre_accept_record_allowed":false,"none_is_evaluated_decision":false,"applies_to":["uncertain","requirement_classes"]},"terminal_decisions":{"reject":{"report_and_stop":true,"write":false,"label_as_failure":false},"defer":{"report_and_stop":true,"write":false,"label_as_failure":false}},"requirement_summary":{"required_slots":["literal_class","evaluation_only","chronology_reason"],"required_ending":"The work may still be implemented through the ordinary requirement workflow.","brevity_may_omit":false},"mixed":{"split_before_evaluation":true,"record_portion":"prior_invariant_only","new_work_role":"context_only","new_work_route":"ordinary_requirement_workflow","include_new_work_in_lesson":false,"shared_topic_converts_new_work":false,"urgency_converts_new_work":false,"authority_converts_new_work":false,"intended_call_format":"evaluate_then_review_then_record_only_if_accept_otherwise_no_write","unconditioned_record_listing_allowed":false},"accepted_capture":{"draft_only_after":"accept","incident_mutability":"immutable","lesson_count":1,"lesson_authority":"proposed","source_portion":"accepted_failure_portion","record_capture_id_state":"accepted","record_drafts_sanitized":true},"generalization_review":{"required_before_record":true,"candidate_limit":3,"automatic_merge":false,"allowed_dispositions":["reuse_existing","generalize_existing","create_distinct"],"exact_reuse_required":true,"rationale_code_required":true,"defer_if_fit_is_uncertain":true},"result_reporting":{"distinguish":["created_new_proposed_lesson","reused_existing_lesson","generalized_existing_lesson"],"cite_returned_identifiers":true,"allow_verified_description":false},"example":{"prior_invariant":"no_raw_prompts","observed_violation":"stored_raw_prompts","new_work":"encryption_at_rest","classification":"mixed","record_only":"raw_prompt_violation","new_work_route":"ordinary_requirement_workflow"},"rationalization_checks":[{"temptation":"The user called it a failure.","response":"Reconstruct chronology; wording is not evidence."},{"temptation":"The new control would have prevented it.","response":"Hindsight does not make the control a prior requirement."},{"temptation":"Bundle both issues to save time.","response":"Split `mixed`; never broaden the immutable incident."},{"temptation":"Record now and qualify later.","response":"Evaluation must precede recording."}],"stop_conditions":["inventing_chronology","using_ad_hoc_class","recording_rejected_or_deferred_capture","upgrading_proposed_lesson_to_verified"]}}"""
_EXPECTED_RECALL_JSON = r"""{"contract_version":4,"policy_kind":"recall_failure","skill":{"name":"recall-failure-lessons","description":"Use before risky or recurring work, or when a current task resembles a previously recorded failure and concrete task evidence can support a bounded lesson lookup."},"policy":{"core":{"mode":"exact_first_bounded_hybrid","similarity_search":true,"returned_lesson_role":"traceable_caution","manufacture_memory":false,"manufacture_authority":false,"automatic_merge":false},"evidence":{"context_field":"text","discriminator_fields":["expected_invariant","controllable_cause","prevention_action","component"],"minimum_discriminators":1,"source":"current_task_evidence","allow_inference":false,"query_exclusions":["raw_prompts","secrets","unnecessary_user_text"],"forbidden_fill_basis":["resemblance_alone","recurrence_anxiety","authority_guess"]},"tools":{"recall":"recall_failure_lessons","feedback":"record_recall_outcome"},"lookup":{"max_calls":1,"default_mode":"auto","exact_first":true,"allow_modes":["auto","exact","lexical","semantic","hybrid"],"default_top_k":3,"hard_max_top_k":5,"allow_bulk":false,"allow_query_broadening":false},"workflow":{"entry":"check_evidence","edges":[{"id":"sufficient_evidence_to_recall","from":"check_evidence","to":"recall","condition":"context_and_discriminator_present"},{"id":"insufficient_evidence_to_continue","from":"check_evidence","to":"continue_without_guidance","condition":"evidence_insufficient"},{"id":"matches_to_return","from":"recall","to":"return_cautions","condition":"matches_returned"},{"id":"no_match_to_continue","from":"recall","to":"continue_without_guidance","condition":"no_match_or_setup_required"}]},"fallback":{"semantic_setup_required":"report_setup_required","hybrid_without_semantic":"accept_degraded_lexical","automatic_install":false,"invented_guidance":false},"result":{"max_lessons":3,"hard_max_lessons":5,"identifier_required":true,"evidence_required":true,"retrieval_channel_required":true,"authority":"proposed_caution","allow_verified":false,"validate_against_current_task":true,"actions_to_validate":["prevention","verification"]},"feedback":{"only_after_observable_outcome":true,"allowed_outcomes":["useful","not_useful","false_positive","prevented_recurrence","contradicted_current_task","stale","ignored","missed_relevant","unknown"],"false_positive_supported":true,"do_not_invent":true},"decision_summary":{"all_slots_required":true,"brevity_may_omit":false,"classifications":[{"id":"insufficient_evidence","intended_call":"none","required_handling":"Continue without invented memory guidance; resemblance alone cannot supply a discriminator."},{"id":"bounded_recall","intended_call":"recall_once","required_handling":"Use at most three returned IDs as proposed cautions, validate their evidence against the current task, and do not merge lessons automatically."}],"every_phrase_required":true},"rationalization_checks":[{"temptation":"It resembles a costly old incident, so query broadly.","response":"Require task context and a concrete discriminator; resemblance alone is insufficient."},{"temptation":"Load every lesson to be safe.","response":"Use one bounded recall call and return at most three cautions."},{"temptation":"A high semantic score proves the same failure.","response":"Similarity proposes a caution; it never proves identity or authorizes an automatic merge."},{"temptation":"Record positive feedback to improve metrics.","response":"Record feedback only after an observable outcome, including false positives when applicable."},{"temptation":"Leadership says the returned lesson is policy.","response":"Returned state and current-task evidence, not pressure, determine authority."}],"stop_conditions":["fabricating_query_evidence","including_sensitive_query_text","bulk_loading_lessons","returning_more_than_three_lessons","omitting_identifiers_or_evidence","automatic_lesson_merge","inventing_recall_feedback","promoting_proposed_guidance"]}}"""
EXPECTED_CONTRACTS: Final = {
    RECORD_SKILL: json.loads(_EXPECTED_RECORD_JSON),
    RECALL_SKILL: json.loads(_EXPECTED_RECALL_JSON),
}


def _validator_path() -> Path:
    override = os.environ.get(VALIDATOR_OVERRIDE_ENV)
    if override:
        return Path(override).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return root / "skills" / ".system" / "skill-creator" / "scripts" / "quick_validate.py"


def _contract_path(skill: str, root: Path = PROJECT_ROOT) -> Path:
    return root / "skills" / skill / "contract.json"


def _load_contract(skill: str, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    document = json.loads(_contract_path(skill, root).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _compare_literal(actual: object, expected: object, path: str, errors: list[str]) -> None:
    if type(actual) is not type(expected):
        errors.append(f"{path}: expected {type(expected).__name__}, got {type(actual).__name__}")
        return
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        if set(actual) != set(expected):
            errors.append(f"{path}: expected keys {sorted(expected)}, got {sorted(actual)}")
            return
        for key, expected_value in expected.items():
            _compare_literal(actual[key], expected_value, f"{path}.{key}", errors)
        return
    if isinstance(expected, list):
        assert isinstance(actual, list)
        if len(actual) != len(expected):
            errors.append(f"{path}: expected {len(expected)} items, got {len(actual)}")
            return
        for index, expected_value in enumerate(expected):
            _compare_literal(actual[index], expected_value, f"{path}[{index}]", errors)
        return
    if actual != expected:
        errors.append(f"{path}: expected {expected!r}, got {actual!r}")


def _validate_graph(skill: str, workflow: dict[str, Any], errors: list[str]) -> None:
    nodes = (
        {"classify", "evaluate", "review", "record", "terminal_no_write"}
        if skill == RECORD_SKILL
        else {"check_evidence", "recall", "return_cautions", "continue_without_guidance"}
    )
    if workflow.get("entry") not in nodes:
        errors.append(f"$.policy.workflow.entry: unknown node {workflow.get('entry')!r}")
    edge_ids: list[object] = []
    for index, edge in enumerate(workflow.get("edges", [])):
        if not isinstance(edge, dict):
            errors.append(f"$.policy.workflow.edges[{index}]: expected object")
            continue
        edge_ids.append(edge.get("id"))
        for endpoint in ("from", "to"):
            if edge.get(endpoint) not in nodes:
                errors.append(
                    f"$.policy.workflow.edges[{index}].{endpoint}: unknown node {edge.get(endpoint)!r}"
                )
    if len(edge_ids) != len(set(edge_ids)):
        errors.append("$.policy.workflow.edges: duplicate edge id")


def validate_policy_contract(skill: str, document: dict[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    _compare_literal(document, EXPECTED_CONTRACTS[skill], "$", errors)
    policy = document.get("policy")
    if not isinstance(policy, dict):
        errors.append("$.policy: expected object")
        return tuple(dict.fromkeys(errors))
    workflow = policy.get("workflow")
    if not isinstance(workflow, dict):
        errors.append("$.policy.workflow: expected object")
        return tuple(dict.fromkeys(errors))
    _validate_graph(skill, workflow, errors)
    return tuple(dict.fromkeys(errors))


def _leaf_paths(value: object, path: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    if isinstance(value, dict):
        return [leaf for key, child in value.items() for leaf in _leaf_paths(child, (*path, key))]
    if isinstance(value, list):
        return [
            leaf for index, child in enumerate(value) for leaf in _leaf_paths(child, (*path, index))
        ]
    return [path]


def _get_path(document: object, path: tuple[str | int, ...]) -> object:
    target = document
    for component in path:
        target = target[component]  # type: ignore[index]
    return target


def _set_path(document: object, path: tuple[str | int, ...], value: object) -> None:
    target = document
    for component in path[:-1]:
        target = target[component]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]


def _wrong_value(value: object) -> object:
    if type(value) is bool:
        return not value
    if type(value) is int:
        return value + 1
    if isinstance(value, str):
        return value + "-mutated"
    raise AssertionError(type(value))


@dataclass(frozen=True)
class ContractMutation:
    skill: str
    path: tuple[str | int, ...]

    @property
    def id(self) -> str:
        return f"{self.skill}-{'-'.join(map(str, self.path))}"


CONTRACT_MUTATIONS: Final = tuple(
    ContractMutation(skill, path)
    for skill, expected in EXPECTED_CONTRACTS.items()
    for path in _leaf_paths(expected)
)


def _load_renderer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("failure_memory_skill_renderer", RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_contracts(root: Path) -> None:
    for skill in SKILL_NAMES:
        target = root / "skills" / skill
        target.mkdir(parents=True)
        shutil.copy2(_contract_path(skill), target / "contract.json")


def _read_frontmatter(skill: str) -> dict[str, str]:
    content = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
    match = re.fullmatch(r"---\n(?P<frontmatter>.*?)\n---\n.*", content, re.DOTALL)
    assert match is not None
    return {
        key.strip(): value.strip()
        for line in match.group("frontmatter").splitlines()
        for key, separator, value in [line.partition(":")]
        if separator
    }


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_canonical_json_matches_independent_literal_schema_and_graph(skill: str) -> None:
    assert validate_policy_contract(skill, _load_contract(skill)) == ()


def test_mutation_corpus_covers_every_contract_leaf() -> None:
    expected_count = sum(len(_leaf_paths(value)) for value in EXPECTED_CONTRACTS.values())
    assert len(CONTRACT_MUTATIONS) == expected_count
    assert len({mutation.id for mutation in CONTRACT_MUTATIONS}) == expected_count


@pytest.mark.parametrize("mutation", CONTRACT_MUTATIONS, ids=lambda item: item.id)
def test_independent_validator_rejects_every_contract_leaf_mutation(
    mutation: ContractMutation,
) -> None:
    document = copy.deepcopy(EXPECTED_CONTRACTS[mutation.skill])
    current = _get_path(document, mutation.path)
    _set_path(document, mutation.path, _wrong_value(current))
    assert validate_policy_contract(mutation.skill, document)


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_checked_in_skill_is_exact_renderer_output(skill: str) -> None:
    renderer = _load_renderer()
    rendered = renderer.render_skill(_load_contract(skill))
    assert (SKILLS_ROOT / skill / "SKILL.md").read_bytes() == rendered.encode("utf-8")


@pytest.mark.parametrize(
    ("skill", "old", "new"),
    [
        (
            RECORD_SKILL,
            "`evaluate_failure_candidate` MUST precede `review_failure_recording` and `record_failure_incident`",
            "`record_failure_incident` MAY precede `evaluate_failure_candidate`",
        ),
        (
            RECALL_SKILL,
            "Call `recall_failure_lessons` at most once",
            "Call `recall_failure_lessons` twice",
        ),
    ],
)
def test_check_rejects_manual_artifact_replacement(
    tmp_path: Path,
    skill: str,
    old: str,
    new: str,
) -> None:
    _copy_contracts(tmp_path)
    command = [sys.executable, str(RENDERER_PATH), "--root", str(tmp_path)]
    assert subprocess.run(command, check=False).returncode == 0
    artifact = tmp_path / "skills" / skill / "SKILL.md"
    mutated = artifact.read_text(encoding="utf-8").replace(old, new, 1)
    assert mutated != artifact.read_text(encoding="utf-8")
    artifact.write_text(mutated, encoding="utf-8")
    completed = subprocess.run([*command, "--check"], capture_output=True, text=True, check=False)
    assert completed.returncode == 1
    assert f"skills/{skill}/SKILL.md" in completed.stderr


def test_renderer_is_deterministic_idempotent_and_check_is_read_only(tmp_path: Path) -> None:
    _copy_contracts(tmp_path)
    renderer = _load_renderer()
    first = renderer.render_all(PROJECT_ROOT)
    second = renderer.render_all(PROJECT_ROOT)
    assert first == second
    command = [sys.executable, str(RENDERER_PATH), "--root", str(tmp_path)]
    assert subprocess.run(command, check=False).returncode == 0
    before = {
        skill: (tmp_path / "skills" / skill / "SKILL.md").read_bytes() for skill in SKILL_NAMES
    }
    assert subprocess.run(command, check=False).returncode == 0
    assert subprocess.run([*command, "--check"], check=False).returncode == 0
    assert before == {
        skill: (tmp_path / "skills" / skill / "SKILL.md").read_bytes() for skill in SKILL_NAMES
    }


def test_renderer_is_stdlib_only_and_has_no_machine_absolute_path() -> None:
    source = RENDERER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imported_roots <= sys.stdlib_module_names
    assert "/Users/" not in source
    assert re.search(r"[A-Za-z]:\\\\", source) is None


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_generated_frontmatter_and_official_validator(skill: str) -> None:
    assert _read_frontmatter(skill) == EXPECTED_CONTRACTS[skill]["skill"]
    validator = _validator_path()
    if not validator.is_file():
        pytest.skip("official skill validator is unavailable")
    uv = shutil.which("uv")
    assert uv is not None
    completed = subprocess.run(
        [
            uv,
            "run",
            "--with",
            "PyYAML>=6,<7",
            "python",
            str(validator),
            str(SKILLS_ROOT / skill),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_validator_path_supports_override_and_codex_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    override = tmp_path / "custom-validator.py"
    monkeypatch.setenv(VALIDATOR_OVERRIDE_ENV, str(override))
    assert _validator_path() == override
    monkeypatch.delenv(VALIDATOR_OVERRIDE_ENV)
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert (
        _validator_path()
        == codex_home / "skills" / ".system" / "skill-creator" / "scripts" / "quick_validate.py"
    )


def test_discovery_descriptions_separate_record_and_recall_triggers() -> None:
    record = _read_frontmatter(RECORD_SKILL)["description"].lower()
    recall = _read_frontmatter(RECALL_SKILL)["description"].lower()
    assert "not for new requirements" in record
    assert "correction" in record
    assert "previously recorded failure" in recall
    assert "bounded lesson lookup" in recall


def test_contract_renderer_and_evidence_resources_are_packageable() -> None:
    manifest = json.loads((PROJECT_ROOT / ".codex-plugin" / "plugin.json").read_text())
    assert manifest["skills"] == "./skills/"
    paths = [
        RENDERER_PATH,
        EVIDENCE_PATH,
        *(_contract_path(skill) for skill in SKILL_NAMES),
    ]
    for path in paths:
        assert path.is_file()
        completed = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path.relative_to(PROJECT_ROOT))],
            cwd=PROJECT_ROOT,
            check=False,
        )
        assert completed.returncode == 1, f"{path} must remain packageable"
