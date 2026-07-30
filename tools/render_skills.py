#!/usr/bin/env python3
# ruff: noqa: E501
"""Validate and atomically render Agent Skills from canonical JSON contracts.

Run ``python tools/render_skills.py`` after an approved policy/template change.
Run ``python tools/render_skills.py --check`` in validation workflows.

This development tool is standard-library-only and has no plugin runtime role.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

SKILL_NAMES = ("record-agent-failure", "recall-failure-lessons")
_RECORD_CONTRACT_JSON = r"""{"contract_version":2,"policy_kind":"record_failure","skill":{"name":"record-agent-failure","description":"Use when a user challenges an agent outcome, reports a missed prior invariant or repeated correction, or explicitly asks the agent to learn from a failure; not for new requirements, newly supplied details, first-time preferences, or ordinary refinement."},"policy":{"core":{"corrective_wording_is_failure_evidence":false,"expectation_time_basis":"before_outcome","required_mismatch_qualities":["material","controllable","durable"]},"evidence":{"establish_chronology":true,"fields":["expectation_source","availability_time","observed_outcome","inspectable_mismatch","impact_or_recurrence_risk","controllability_with_then_available_information","durable_prevention_value"],"prohibit_invention":true,"draft_exclusions":["raw_prompts","secrets","unnecessary_user_text"]},"classification":{"cardinality":1,"tier":"Tier One","classes":[{"id":"requirement_update","criterion":"The requested feature or contract changed after the outcome."},{"id":"requirement_clarification","criterion":"A previously unavailable detail now clarifies the work."},{"id":"preference_update","criterion":"A preference is stated for the first time."},{"id":"real_failure","criterion":"A prior expectation, mismatch, impact/risk, controllability, and durable lesson are all evidenced."},{"id":"mixed","criterion":"Feedback contains both a genuine prior-invariant mismatch and new work."},{"id":"uncertain","criterion":"Chronology or evidence cannot establish the criteria; do not guess."}]},"tools":{"evaluate":"evaluate_failure_candidate","record":"record_failure_incident"},"workflow":{"entry":"classify","tool_order":["evaluate_failure_candidate","record_failure_incident"],"edges":[{"id":"classify_to_evaluate","from":"classify","to":"evaluate","condition":"always"},{"id":"evaluate_accept_to_record","from":"evaluate","to":"record","condition":"decision == accept"},{"id":"evaluate_reject_defer_to_terminal","from":"evaluate","to":"terminal_no_write","condition":"decision in [reject, defer]"}]},"evaluation":{"every_classification":true,"pre_accept_record_allowed":false,"none_is_evaluated_decision":false,"applies_to":["uncertain","requirement_classes"]},"terminal_decisions":{"reject":{"report_and_stop":true,"write":false,"label_as_failure":false},"defer":{"report_and_stop":true,"write":false,"label_as_failure":false}},"requirement_summary":{"required_slots":["literal_class","evaluation_only","chronology_reason"],"required_ending":"The work may still be implemented through the ordinary requirement workflow.","brevity_may_omit":false},"mixed":{"split_before_evaluation":true,"record_portion":"prior_invariant_only","new_work_role":"context_only","new_work_route":"ordinary_requirement_workflow","include_new_work_in_lesson":false,"shared_topic_converts_new_work":false,"urgency_converts_new_work":false,"authority_converts_new_work":false,"intended_call_format":"evaluate_then_record_only_if_accept_otherwise_no_write","unconditioned_record_listing_allowed":false},"accepted_capture":{"draft_only_after":"accept","incident_mutability":"immutable","lesson_count":1,"lesson_authority":"proposed","source_portion":"accepted_failure_portion","record_capture_id_state":"accepted","record_drafts_sanitized":true},"result_reporting":{"distinguish":["created_new_proposed_lesson","reused_exact_existing_lesson"],"cite_returned_identifiers":true,"allow_verified_description":false},"example":{"prior_invariant":"no_raw_prompts","observed_violation":"stored_raw_prompts","new_work":"encryption_at_rest","classification":"mixed","record_only":"raw_prompt_violation","new_work_route":"ordinary_requirement_workflow"},"rationalization_checks":[{"temptation":"The user called it a failure.","response":"Reconstruct chronology; wording is not evidence."},{"temptation":"The new control would have prevented it.","response":"Hindsight does not make the control a prior requirement."},{"temptation":"Bundle both issues to save time.","response":"Split `mixed`; never broaden the immutable incident."},{"temptation":"Record now and qualify later.","response":"Evaluation must precede recording."}],"stop_conditions":["inventing_chronology","using_ad_hoc_class","recording_rejected_or_deferred_capture","upgrading_proposed_lesson_to_verified"]}}"""
_RECALL_CONTRACT_JSON = r"""{"contract_version":4,"policy_kind":"recall_failure","skill":{"name":"recall-failure-lessons","description":"Use before risky or recurring work, or when a current task resembles a previously recorded failure and concrete task evidence can support a bounded lesson lookup."},"policy":{"core":{"mode":"exact_first_bounded_hybrid","similarity_search":true,"returned_lesson_role":"traceable_caution","manufacture_memory":false,"manufacture_authority":false,"automatic_merge":false},"evidence":{"context_field":"text","discriminator_fields":["expected_invariant","controllable_cause","prevention_action","component"],"minimum_discriminators":1,"source":"current_task_evidence","allow_inference":false,"query_exclusions":["raw_prompts","secrets","unnecessary_user_text"],"forbidden_fill_basis":["resemblance_alone","recurrence_anxiety","authority_guess"]},"tools":{"recall":"recall_failure_lessons","feedback":"record_recall_outcome"},"lookup":{"max_calls":1,"default_mode":"auto","exact_first":true,"allow_modes":["auto","exact","lexical","semantic","hybrid"],"default_top_k":3,"hard_max_top_k":5,"allow_bulk":false,"allow_query_broadening":false},"workflow":{"entry":"check_evidence","edges":[{"id":"sufficient_evidence_to_recall","from":"check_evidence","to":"recall","condition":"context_and_discriminator_present"},{"id":"insufficient_evidence_to_continue","from":"check_evidence","to":"continue_without_guidance","condition":"evidence_insufficient"},{"id":"matches_to_return","from":"recall","to":"return_cautions","condition":"matches_returned"},{"id":"no_match_to_continue","from":"recall","to":"continue_without_guidance","condition":"no_match_or_setup_required"}]},"fallback":{"semantic_setup_required":"report_setup_required","hybrid_without_semantic":"accept_degraded_lexical","automatic_install":false,"invented_guidance":false},"result":{"max_lessons":3,"hard_max_lessons":5,"identifier_required":true,"evidence_required":true,"retrieval_channel_required":true,"authority":"proposed_caution","allow_verified":false,"validate_against_current_task":true,"actions_to_validate":["prevention","verification"]},"feedback":{"only_after_observable_outcome":true,"allowed_outcomes":["useful","not_useful","false_positive","prevented_recurrence","contradicted_current_task","stale","ignored","missed_relevant","unknown"],"false_positive_supported":true,"do_not_invent":true},"decision_summary":{"all_slots_required":true,"brevity_may_omit":false,"classifications":[{"id":"insufficient_evidence","intended_call":"none","required_handling":"Continue without invented memory guidance; resemblance alone cannot supply a discriminator."},{"id":"bounded_recall","intended_call":"recall_once","required_handling":"Use at most three returned IDs as proposed cautions, validate their evidence against the current task, and do not merge lessons automatically."}],"every_phrase_required":true},"rationalization_checks":[{"temptation":"It resembles a costly old incident, so query broadly.","response":"Require task context and a concrete discriminator; resemblance alone is insufficient."},{"temptation":"Load every lesson to be safe.","response":"Use one bounded recall call and return at most three cautions."},{"temptation":"A high semantic score proves the same failure.","response":"Similarity proposes a caution; it never proves identity or authorizes an automatic merge."},{"temptation":"Record positive feedback to improve metrics.","response":"Record feedback only after an observable outcome, including false positives when applicable."},{"temptation":"Leadership says the returned lesson is policy.","response":"Returned state and current-task evidence, not pressure, determine authority."}],"stop_conditions":["fabricating_query_evidence","including_sensitive_query_text","bulk_loading_lessons","returning_more_than_three_lessons","omitting_identifiers_or_evidence","automatic_lesson_merge","inventing_recall_feedback","promoting_proposed_guidance"]}}"""
EXPECTED_CONTRACTS: dict[str, dict[str, Any]] = {
    "record-agent-failure": json.loads(_RECORD_CONTRACT_JSON),
    "recall-failure-lessons": json.loads(_RECALL_CONTRACT_JSON),
}

_SIGNATURE_LABELS = {
    "expected_invariant": "expected invariant",
    "controllable_causal_mechanism": "controllable causal mechanism",
    "prevention_action": "prevention action",
}
_RECORD_STOP_LABELS = {
    "inventing_chronology": "inventing chronology",
    "using_ad_hoc_class": "using an ad-hoc class",
    "recording_rejected_or_deferred_capture": "recording a rejected or deferred capture",
    "upgrading_proposed_lesson_to_verified": ("upgrading a proposed lesson to verified guidance"),
}
_RECALL_STOP_LABELS = {
    "fabricating_query_evidence": "fabricating query evidence",
    "including_sensitive_query_text": "including sensitive query text",
    "bulk_loading_lessons": "bulk-loading lessons",
    "returning_more_than_three_lessons": "returning more than three lessons",
    "omitting_identifiers_or_evidence": "omitting identifiers or evidence",
    "automatic_lesson_merge": "merging lessons automatically",
    "inventing_recall_feedback": "inventing recall feedback",
    "promoting_proposed_guidance": "promoting proposed guidance",
}


class RenderError(Exception):
    """Base class for deterministic renderer failures."""


class ContractError(RenderError):
    """Raised when a policy contract differs from its bounded schema."""


class PathSecurityError(RenderError):
    """Raised when a managed path is unsafe or not the expected file type."""


class RenderedSkill(NamedTuple):
    content: str
    behavior_content: str
    policy_sha256: str
    behavior_sha256: str
    renderer_sha256: str
    behavior_paths: frozenset[str]
    metadata_paths: frozenset[str]


class _TrackedContract:
    """Record the exact contract leaves used to produce behavior or metadata."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.behavior_paths: set[str] = set()
        self.metadata_paths: set[str] = set()

    def root(self) -> _TrackedValue:
        return _TrackedValue(self, self.document, "$")

    def mark(self, path: str) -> None:
        if path in {"$.contract_version", "$.policy_kind"} or (
            path.startswith("$.policy.workflow.edges[") and path.endswith(".id")
        ):
            self.metadata_paths.add(path)
        else:
            self.behavior_paths.add(path)

    def mark_all(self, value: object, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                self.mark_all(child, f"{path}.{key}")
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                self.mark_all(child, f"{path}[{index}]")
            return
        self.mark(path)


class _TrackedValue:
    def __init__(self, tracker: _TrackedContract, value: object, path: str) -> None:
        self._tracker = tracker
        self._value = value
        self._path = path

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(self._value, dict):
            child = self._value[key]
            path = f"{self._path}.{key}"
        elif isinstance(self._value, list):
            assert isinstance(key, int)
            child = self._value[key]
            path = f"{self._path}[{key}]"
        else:
            raise TypeError(f"{self._path} is not indexable")
        if isinstance(child, (dict, list)):
            return _TrackedValue(self._tracker, child, path)
        self._tracker.mark(path)
        return child

    def __iter__(self) -> Any:
        if not isinstance(self._value, list):
            raise TypeError(f"{self._path} is not iterable")
        for index in range(len(self._value)):
            yield self[index]

    def __len__(self) -> int:
        if not isinstance(self._value, (dict, list)):
            raise TypeError(f"{self._path} has no length")
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        self._tracker.mark_all(self._value, self._path)
        return self._value == other


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _compare_exact(actual: object, expected: object, path: str) -> None:
    if type(actual) is not type(expected):
        raise ContractError(
            f"{path}: expected {type(expected).__name__}, got {type(actual).__name__}"
        )
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        actual_keys = set(actual)
        expected_keys = set(expected)
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        if missing:
            raise ContractError(f"{path}: missing keys {missing}")
        if extra:
            raise ContractError(f"{path}: extra keys {extra}")
        for key, expected_value in expected.items():
            _compare_exact(actual[key], expected_value, f"{path}.{key}")
        return
    if isinstance(expected, list):
        assert isinstance(actual, list)
        if len(actual) != len(expected):
            raise ContractError(f"{path}: expected {len(expected)} items, got {len(actual)}")
        for index, expected_value in enumerate(expected):
            _compare_exact(actual[index], expected_value, f"{path}[{index}]")
        return
    if actual != expected:
        raise ContractError(f"{path}: expected {expected!r}, got {actual!r}")


def _validate_graph(skill: str, workflow: dict[str, Any]) -> None:
    expected_nodes = (
        {"classify", "evaluate", "record", "terminal_no_write"}
        if skill == "record-agent-failure"
        else {
            "check_evidence",
            "recall",
            "return_cautions",
            "continue_without_guidance",
        }
    )
    if workflow["entry"] not in expected_nodes:
        raise ContractError(f"$.policy.workflow.entry: unknown node {workflow['entry']!r}")
    edge_ids: set[str] = set()
    for index, edge in enumerate(workflow["edges"]):
        edge_id = edge["id"]
        if edge_id in edge_ids:
            raise ContractError(f"$.policy.workflow.edges[{index}].id: duplicate {edge_id!r}")
        edge_ids.add(edge_id)
        for endpoint in ("from", "to"):
            if edge[endpoint] not in expected_nodes:
                raise ContractError(
                    f"$.policy.workflow.edges[{index}].{endpoint}: unknown node {edge[endpoint]!r}"
                )


def validate_contract(skill: str, document: dict[str, Any]) -> None:
    """Reject missing, extra, mistyped, changed, or invalid graph values."""

    expected = EXPECTED_CONTRACTS.get(skill)
    if expected is None:
        raise ContractError(f"$: unsupported skill {skill!r}")
    _compare_exact(document, expected, "$")
    _validate_graph(skill, document["policy"]["workflow"])


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


def _number_word(value: int) -> str:
    words = {1: "one", 3: "three", 5: "five"}
    try:
        return words[value]
    except KeyError as error:
        raise ContractError(f"unsupported bounded count: {value}") from error


def _edge_by_id(policy: Any, edge_id: str) -> Any:
    return next(edge for edge in policy["workflow"]["edges"] if edge["id"] == edge_id)


def _render_record_behavior(contract: Any) -> str:
    skill = contract["skill"]
    policy = contract["policy"]
    core = policy["core"]
    evidence = policy["evidence"]
    classification = policy["classification"]
    evaluate = policy["tools"]["evaluate"]
    record = policy["tools"]["record"]
    workflow = policy["workflow"]
    evaluation = policy["evaluation"]
    terminals = policy["terminal_decisions"]
    requirement_summary = policy["requirement_summary"]
    mixed = policy["mixed"]
    accepted = policy["accepted_capture"]
    result = policy["result_reporting"]
    example = policy["example"]

    corrective_relation = "is" if core["corrective_wording_is_failure_evidence"] else "is not"
    qualities = ", ".join(core["required_mismatch_qualities"])
    before_outcome = core["expectation_time_basis"] == "before_outcome"
    chronology_required = evidence["establish_chronology"]
    draft_exclusions = evidence["draft_exclusions"]
    class_rows = "\n".join(
        f"   | `{item['id']}` | {item['criterion']} |" for item in classification["classes"]
    )
    class_count = _number_word(classification["cardinality"])
    tier = classification["tier"]
    classify_edge = _edge_by_id(policy, "classify_to_evaluate")
    accept_edge = _edge_by_id(policy, "evaluate_accept_to_record")
    terminal_edge = _edge_by_id(policy, "evaluate_reject_defer_to_terminal")
    accept_decision = accept_edge["condition"].removeprefix("decision == ")
    terminal_decisions = (
        terminal_edge["condition"].removeprefix("decision in [").removesuffix("]").split(", ")
    )
    terminal_words = " or ".join(f"`{decision}`" for decision in terminal_decisions)
    terminal_write_allowed = any(terminals[decision]["write"] for decision in terminal_decisions)
    terminal_failure_label_allowed = any(
        terminals[decision]["label_as_failure"] for decision in terminal_decisions
    )
    initial_authority = accepted["lesson_authority"]
    lesson_count = _number_word(accepted["lesson_count"])
    summary_ending = requirement_summary["required_ending"]
    wrapped_summary_ending = summary_ending.replace(
        " ordinary requirement",
        " ordinary\n   requirement",
    )
    rationalization_rows = "\n".join(
        f"| “{item['temptation']}” | {item['response']} |"
        for item in policy["rationalization_checks"]
    )
    stop_labels = [_RECORD_STOP_LABELS[item] for item in policy["stop_conditions"]]
    stop_text = ", ".join(stop_labels[:-1]) + f", or {stop_labels[-1]}"
    wrapped_stop_text = stop_text.replace(
        "recording a rejected or deferred capture",
        "recording a rejected or\ndeferred capture",
    )

    if (
        not before_outcome
        or not chronology_required
        or evidence["fields"]
        != [
            "expectation_source",
            "availability_time",
            "observed_outcome",
            "inspectable_mismatch",
            "impact_or_recurrence_risk",
            "controllability_with_then_available_information",
            "durable_prevention_value",
        ]
        or evidence["prohibit_invention"] is not True
        or draft_exclusions != ["raw_prompts", "secrets", "unnecessary_user_text"]
        or workflow["entry"] != classify_edge["from"]
        or workflow["tool_order"] != [evaluate, record]
        or classify_edge["to"] != "evaluate"
        or classify_edge["condition"] != "always"
        or accept_edge["from"] != "evaluate"
        or accept_edge["to"] != "record"
        or terminal_edge["from"] != "evaluate"
        or terminal_edge["to"] != "terminal_no_write"
        or evaluation["every_classification"] is not True
        or evaluation["pre_accept_record_allowed"] is not False
        or evaluation["none_is_evaluated_decision"] is not False
        or evaluation["applies_to"] != ["uncertain", "requirement_classes"]
        or terminals["reject"]["report_and_stop"] is not True
        or terminals["defer"]["report_and_stop"] is not True
        or terminal_write_allowed
        or terminal_failure_label_allowed
        or mixed["split_before_evaluation"] is not True
        or mixed["record_portion"] != "prior_invariant_only"
        or mixed["new_work_role"] != "context_only"
        or mixed["new_work_route"] != "ordinary_requirement_workflow"
        or mixed["include_new_work_in_lesson"] is not False
        or mixed["shared_topic_converts_new_work"]
        or mixed["urgency_converts_new_work"]
        or mixed["authority_converts_new_work"]
        or mixed["intended_call_format"] != "evaluate_then_record_only_if_accept_otherwise_no_write"
        or mixed["unconditioned_record_listing_allowed"]
        or accepted["draft_only_after"] != accept_decision
        or accepted["incident_mutability"] != "immutable"
        or accepted["source_portion"] != "accepted_failure_portion"
        or accepted["record_capture_id_state"] != "accepted"
        or accepted["record_drafts_sanitized"] is not True
        or result["cite_returned_identifiers"] is not True
        or result["allow_verified_description"] is not False
        or result["distinguish"] != ["created_new_proposed_lesson", "reused_exact_existing_lesson"]
        or requirement_summary["required_slots"]
        != ["literal_class", "evaluation_only", "chronology_reason"]
        or requirement_summary["brevity_may_omit"]
        or example["prior_invariant"] != "no_raw_prompts"
        or example["observed_violation"] != "stored_raw_prompts"
        or example["new_work"] != "encryption_at_rest"
        or example["record_only"] != "raw_prompt_violation"
        or example["new_work_route"] != "ordinary_requirement_workflow"
    ):
        raise ContractError("record policy cannot be rendered safely")

    return f"""---
name: {skill["name"]}
description: {skill["description"]}
---

# Record Agent Failure

## Core principle

Corrective wording {corrective_relation} evidence of failure. Establish what was required or reasonably
knowable before the outcome, then preserve only a {qualities} mismatch.

## Non-negotiable contract

| Contract key | Normative rule |
|---|---|
| `first_call` | `{evaluate}` MUST precede `{record}` and MUST be the first failure-memory call for every {tier} classification. |
| `write_gate` | `{record}` MUST be called ONLY IF evaluation returns `{accept_decision}`; `{record}` MUST NOT be called for `{terminal_decisions[0]}` or `{terminal_decisions[1]}`. |
| `rejected_status` | A rejected or deferred capture MUST NOT be described or called a failure. |
| `lesson_authority` | A new or reused lesson MUST remain `{initial_authority}`; a proposed lesson MUST NOT be described, promoted, or treated as `verified`. |
| `mixed_output` | For `mixed`, the intended-call field MUST be `{evaluate} -> {record} only if decision={accept_decision} (otherwise no write)`, and new requirements MUST remain outside the failure. |

## Required workflow

1. **Establish chronology and evidence.** Identify the expectation source, when it became
   available, the observed outcome, inspectable mismatch, impact or recurrence risk,
   controllability with information available then, and durable prevention value. Do not
   copy raw prompts, secrets, or unnecessary user text into drafts.
2. **Choose exactly {class_count} {tier} classification:**

   | Class | Use when |
   |---|---|
{class_rows}

3. Call `{evaluate}` with every classification. Before `{accept_decision}`, the
   intended call list contains this evaluation and no write; `none` is not an evaluated
   decision, including for `{evaluation["applies_to"][0]}` and requirement classes.
4. If the decision is {terminal_words}, report it and stop; do not call
   `{record}` or call the capture a failure. A requirement-class summary
   has three required slots: the literal class; `{evaluate}` only; and a
   chronology reason ending “{wrapped_summary_ending}” Brevity does not remove that disposition.
5. For `mixed`, separate the portions before evaluation. Put only the prior-invariant
   mismatch in the failure portion; retain a new requirement only as context. Shared
   topic, urgency, or authority never turns new work into a lesson. The intended-call
   field has this required shape:
   `{evaluate} -> {record} only if decision={accept_decision}
   (otherwise no write)`. Never list the record call without its acceptance condition.
6. Only after `{accept_decision}`, draft the immutable incident and {lesson_count} {initial_authority} lesson from the
   accepted failure portion.
7. Call `{record}` with the accepted capture ID and sanitized drafts.
8. Report whether the result created a new proposed lesson or reused an exact existing
   lesson. Cite returned identifiers. Never describe a proposed lesson as verified.

## Mixed example

If stored raw prompts violated an accepted no-raw-prompts invariant while encryption at
rest is requested for the first time, classify `{example["classification"]}`; evaluate and record only the raw
prompt violation. Route encryption as new work.

## Rationalization checks

| Temptation | Required response |
|---|---|
{rationalization_rows}

Stop if you are {wrapped_stop_text}.
"""


def _render_recall_behavior(contract: Any) -> str:
    skill = contract["skill"]
    policy = contract["policy"]
    core = policy["core"]
    evidence = policy["evidence"]
    recall = policy["tools"]["recall"]
    feedback_tool = policy["tools"]["feedback"]
    lookup = policy["lookup"]
    workflow = policy["workflow"]
    fallback = policy["fallback"]
    result = policy["result"]
    feedback = policy["feedback"]
    decision = policy["decision_summary"]

    discriminator_rows = "\n".join(
        f"   - `{field}`{';' if index < len(evidence['discriminator_fields']) - 1 else '.'}"
        for index, field in enumerate(evidence["discriminator_fields"])
    )
    max_lessons = _number_word(result["max_lessons"])
    hard_max_lessons = _number_word(result["hard_max_lessons"])
    authority = result["authority"].replace("_", " ")
    modes = ", ".join(f"`{mode}`" for mode in lookup["allow_modes"])
    exclusions = ", ".join(evidence["query_exclusions"])
    feedback_outcomes = ", ".join(f"`{item}`" for item in feedback["allowed_outcomes"])
    summary_rows = decision["classifications"]
    insufficient_summary = summary_rows[0]
    recall_summary = summary_rows[1]
    intended_recall = (
        f"`{recall}` once"
        if recall_summary["intended_call"] == "recall_once"
        else recall_summary["intended_call"]
    )
    rationalization_rows = "\n".join(
        f"| “{item['temptation']}” | {item['response']} |"
        for item in policy["rationalization_checks"]
    )
    stop_labels = [_RECALL_STOP_LABELS[item] for item in policy["stop_conditions"]]
    stop_text = ", ".join(stop_labels[:-1]) + f", or {stop_labels[-1]}"
    sufficient_edge = _edge_by_id(policy, "sufficient_evidence_to_recall")
    insufficient_edge = _edge_by_id(policy, "insufficient_evidence_to_continue")
    matches_edge = _edge_by_id(policy, "matches_to_return")
    no_match_edge = _edge_by_id(policy, "no_match_to_continue")
    if (
        core["mode"] != "exact_first_bounded_hybrid"
        or core["similarity_search"] is not True
        or core["returned_lesson_role"] != "traceable_caution"
        or core["manufacture_memory"]
        or core["manufacture_authority"]
        or core["automatic_merge"]
        or evidence["context_field"] != "text"
        or evidence["minimum_discriminators"] != 1
        or evidence["source"] != "current_task_evidence"
        or evidence["allow_inference"]
        or evidence["forbidden_fill_basis"]
        != ["resemblance_alone", "recurrence_anxiety", "authority_guess"]
        or workflow["entry"] != sufficient_edge["from"]
        or sufficient_edge["from"] != "check_evidence"
        or sufficient_edge["to"] != "recall"
        or sufficient_edge["condition"] != "context_and_discriminator_present"
        or insufficient_edge["from"] != "check_evidence"
        or insufficient_edge["to"] != "continue_without_guidance"
        or insufficient_edge["condition"] != "evidence_insufficient"
        or matches_edge["from"] != "recall"
        or matches_edge["to"] != "return_cautions"
        or matches_edge["condition"] != "matches_returned"
        or no_match_edge["from"] != "recall"
        or no_match_edge["to"] != "continue_without_guidance"
        or no_match_edge["condition"] != "no_match_or_setup_required"
        or lookup["max_calls"] != 1
        or lookup["default_mode"] != "auto"
        or lookup["exact_first"] is not True
        or lookup["default_top_k"] != result["max_lessons"]
        or lookup["hard_max_top_k"] != result["hard_max_lessons"]
        or lookup["allow_bulk"]
        or lookup["allow_query_broadening"]
        or fallback["semantic_setup_required"] != "report_setup_required"
        or fallback["hybrid_without_semantic"] != "accept_degraded_lexical"
        or fallback["automatic_install"]
        or fallback["invented_guidance"]
        or result["identifier_required"] is not True
        or result["evidence_required"] is not True
        or result["retrieval_channel_required"] is not True
        or result["allow_verified"]
        or result["validate_against_current_task"] is not True
        or result["actions_to_validate"] != ["prevention", "verification"]
        or feedback["only_after_observable_outcome"] is not True
        or feedback["false_positive_supported"] is not True
        or feedback["do_not_invent"] is not True
        or decision["all_slots_required"] is not True
        or decision["brevity_may_omit"]
        or decision["every_phrase_required"] is not True
    ):
        raise ContractError("recall policy cannot be rendered safely")

    return f"""---
name: {skill["name"]}
description: {skill["description"]}
---

# Recall Failure Lessons

## Core principle

Use exact-first, bounded hybrid recall when the current task supplies enough evidence.
Similarity can surface a traceable caution; it cannot prove identity, create authority, or
merge lessons automatically.

## Required response vocabulary

When a prompt asks for `Classification` or `Intended tool calls`, this is a closed
protocol. You MUST copy one of the following literal rows. A synonym, paraphrase, or
invented class is invalid:

- `Classification: {insufficient_summary["id"]}; Intended tool calls: none`
- `Classification: {recall_summary["id"]}; Intended tool calls:
  {recall}(mode={lookup["default_mode"]}, top_k={lookup["default_top_k"]}) once`

Missing current-task context or a discriminator always maps to
`{insufficient_summary["id"]}`. Current-task context plus a discriminator always maps to
`{recall_summary["id"]}`. Never rename these as `blocked`, `insufficient context`,
`recall only`, `targeted recall only`, or `reject`.

When the evidence gate passes, unsafe requests to bulk-load, merge, promote, or record
premature feedback do not cancel the safe bounded recall. Reject only those unsafe extras
and still make the one `{recall}(mode={lookup["default_mode"]},
top_k={lookup["default_top_k"]})` call.

Do not list `{feedback_tool}` until an observable outcome exists.

## Non-negotiable contract

| Contract key | Normative rule |
|---|---|
| `evidence_gate` | `{recall}` MUST be called only with current-task context plus at least one concrete discriminator; fields MUST NOT be guessed or inferred. |
| `classification` | If the gate fails, report literal `{insufficient_summary["id"]}`; if it passes, report literal `{recall_summary["id"]}`. |
| `bounded_lookup` | Call `{recall}` at most once, default to `{lookup["default_mode"]}`, request {lookup["default_top_k"]}, and never exceed {lookup["hard_max_top_k"]}. |
| `privacy` | Exclude {exclusions} from the query; provide only the minimum task evidence needed for retrieval. |
| `cardinality` | Apply at most {max_lessons} returned lessons ({hard_max_lessons} is the API hard limit); never bulk-load memory. |
| `traceability` | Cite each returned lesson-version ID, retrieval channel, and supporting invariant/cause evidence. |
| `authority` | Every returned lesson remains a {authority}; semantic score is not proof or verified policy. |
| `merge_gate` | Similar records MUST NOT be merged or generalized automatically. |

## Required workflow

1. Build a minimal query from the current task's `{evidence["context_field"]}` and at
   least {evidence["minimum_discriminators"]} of these discriminators:

{discriminator_rows}

   Resemblance alone, recurrence anxiety, and an authority's guess cannot supply a
   discriminator. Do not include {exclusions}.
2. If the evidence gate is not met, classify literal `{insufficient_summary["id"]}`,
   make no memory call, and continue without invented guidance. If it is met, classify
   literal `{recall_summary["id"]}`.
3. Otherwise call `{recall}` once with mode `{lookup["default_mode"]}` and
   `top_k={lookup["default_top_k"]}`. Supported explicit modes are {modes}. Do not broaden
   or retry a query to force a match.
4. Auto mode checks an exact three-field signature first, then uses bounded hybrid recall.
   If semantic setup is required, report it. A hybrid call may accept degraded lexical
   results; never install packages or download models implicitly.
5. If no lesson is returned, continue without memory guidance. If matches are returned,
   apply at most {max_lessons}; cite every returned ID, channel, and evidence. Validate
   prevention and verification actions against the current task.
6. After an observable result—not merely to improve metrics—call `{feedback_tool}` with
   one of {feedback_outcomes}. Record `false_positive` when a recalled lesson does not fit.
   Record `missed_relevant` only when an existing relevant lesson was not selected; pass
   the original `attempt_id` and the known `lesson_version_id`. Do not rename these
   fields, guess an ID, or invent feedback.

## Decision summaries

Use every slot; concise output does not remove match handling.

| Classification | Intended call | Required handling |
|---|---|---|
| `{insufficient_summary["id"]}` | `{insufficient_summary["intended_call"]}` | Say: “{insufficient_summary["required_handling"]}” |
| `{recall_summary["id"]}` | {intended_recall} with `mode={lookup["default_mode"]}, top_k={lookup["default_top_k"]}` | Say: “{recall_summary["required_handling"]}” |

Every phrase in the applicable handling cell is required even in terse output.

## Rationalization checks

| Temptation | Required response |
|---|---|
{rationalization_rows}

Stop if you are {stop_text}.
"""


def _behavior_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_regular_bytes(path: Path, label: str) -> bytes:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as error:
        raise PathSecurityError(f"{label} does not exist: {path}") from error
    if stat.S_ISLNK(file_stat.st_mode):
        raise PathSecurityError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISREG(file_stat.st_mode):
        raise PathSecurityError(f"{label} must be a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PathSecurityError(f"cannot safely open {label}: {path}") from error
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise PathSecurityError(f"{label} must be a regular file: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _renderer_digest() -> str:
    return hashlib.sha256(_read_regular_bytes(Path(__file__), "renderer source")).hexdigest()


def _insert_generated_note(
    behavior: str,
    policy_sha256: str,
    behavior_sha256: str,
    renderer_sha256: str,
) -> str:
    heading_end = behavior.find("\n\n", behavior.find("\n# ") + 1)
    if heading_end < 0:
        raise RenderError("generated behavior has no skill heading")
    note = (
        "<!-- Generated from contract.json by tools/render_skills.py; "
        f"policy sha256={policy_sha256}; behavior sha256={behavior_sha256}; "
        f"renderer sha256={renderer_sha256}; "
        "DO NOT EDIT SKILL.md MANUALLY. -->"
    )
    insertion = heading_end + 2
    return behavior[:insertion] + note + "\n\n" + behavior[insertion:]


def render_skill_with_trace(contract: dict[str, Any]) -> RenderedSkill:
    kind = contract.get("policy_kind")
    skill = {
        "record_failure": "record-agent-failure",
        "recall_failure": "recall-failure-lessons",
    }.get(kind)
    if skill is None:
        raise ContractError(f"$.policy_kind: unsupported value {kind!r}")
    validate_contract(skill, contract)
    tracker = _TrackedContract(contract)
    tracked_contract = tracker.root()
    tracked_contract["contract_version"]
    tracked_contract["policy_kind"]
    behavior = (
        _render_record_behavior(tracked_contract)
        if skill == "record-agent-failure"
        else _render_recall_behavior(tracked_contract)
    )
    policy_digest = _canonical_sha256(contract)
    behavior_digest = _behavior_digest(behavior)
    renderer_digest = _renderer_digest()
    all_paths = _leaf_paths(contract)
    consumed_paths = tracker.behavior_paths | tracker.metadata_paths
    unconsumed = sorted(all_paths - consumed_paths)
    if unconsumed:
        raise ContractError(f"unconsumed contract leaves: {unconsumed}")
    return RenderedSkill(
        content=_insert_generated_note(
            behavior,
            policy_digest,
            behavior_digest,
            renderer_digest,
        ),
        behavior_content=behavior,
        policy_sha256=policy_digest,
        behavior_sha256=behavior_digest,
        renderer_sha256=renderer_digest,
        behavior_paths=frozenset(tracker.behavior_paths),
        metadata_paths=frozenset(tracker.metadata_paths),
    )


def render_skill(contract: dict[str, Any]) -> str:
    return render_skill_with_trace(contract).content


def _validated_root(root: Path) -> Path:
    candidate = Path(os.path.abspath(root))
    try:
        root_stat = candidate.lstat()
    except FileNotFoundError as error:
        raise PathSecurityError(f"root does not exist: {candidate}") from error
    if stat.S_ISLNK(root_stat.st_mode):
        raise PathSecurityError(f"root must not be a symlink: {candidate}")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise PathSecurityError(f"root must be a directory: {candidate}")
    resolved = candidate.resolve(strict=True)
    if resolved != candidate:
        raise PathSecurityError(f"root must resolve without symlink traversal: {candidate}")
    return resolved


def _managed_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise PathSecurityError(f"managed path must be a safe relative path: {relative}")
    candidate = root.joinpath(relative)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise PathSecurityError(f"managed path escapes root: {relative}") from error
    return candidate


def _validate_directory(path: Path, root: Path, label: str) -> None:
    try:
        directory_stat = path.lstat()
    except FileNotFoundError as error:
        raise PathSecurityError(f"{label} does not exist: {path}") from error
    if stat.S_ISLNK(directory_stat.st_mode):
        raise PathSecurityError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise PathSecurityError(f"{label} must be a directory: {path}")
    if path.resolve(strict=True).parent != root and root not in path.resolve(strict=True).parents:
        raise PathSecurityError(f"{label} resolves outside root: {path}")


def _preflight_directories(root: Path) -> None:
    skills = _managed_path(root, Path("skills"))
    _validate_directory(skills, root, "skills directory")
    for skill in SKILL_NAMES:
        directory = _managed_path(root, Path("skills") / skill)
        _validate_directory(directory, root, f"{skill} directory")


def _artifact_preflight(path: Path) -> None:
    try:
        artifact_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(artifact_stat.st_mode):
        raise PathSecurityError(f"artifact must not be a symlink: {path}")
    if not stat.S_ISREG(artifact_stat.st_mode):
        raise PathSecurityError(f"artifact must be a regular file: {path}")


def _load_contract(root: Path, skill: str) -> dict[str, Any]:
    path = _managed_path(root, Path("skills") / skill / "contract.json")
    raw = _read_regular_bytes(path, f"{skill} contract")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{path}: invalid UTF-8 JSON: {error}") from error
    if not isinstance(document, dict):
        raise ContractError(f"{path}: contract root must be an object")
    return document


def _load_validate_render_all(root: Path) -> tuple[Path, dict[str, RenderedSkill]]:
    validated_root = _validated_root(root)
    _preflight_directories(validated_root)
    contracts = {skill: _load_contract(validated_root, skill) for skill in SKILL_NAMES}
    for skill in SKILL_NAMES:
        validate_contract(skill, contracts[skill])
    rendered = {skill: render_skill_with_trace(contracts[skill]) for skill in SKILL_NAMES}
    for skill in SKILL_NAMES:
        artifact = _managed_path(
            validated_root,
            Path("skills") / skill / "SKILL.md",
        )
        _artifact_preflight(artifact)
    return validated_root, rendered


def render_all(root: Path) -> dict[str, str]:
    _, rendered = _load_validate_render_all(root)
    return {skill: result.content for skill, result in rendered.items()}


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_temp(destination: Path, content: bytes, mode: int) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".failure-memory-",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise
    return temporary


def _atomic_write_all(items: list[tuple[Path, bytes]]) -> None:
    """Replace a batch atomically per file and roll back a partial batch."""

    staged: dict[Path, Path] = {}
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    try:
        for destination, content in items:
            _artifact_preflight(destination)
            try:
                destination_stat = destination.lstat()
            except FileNotFoundError:
                mode = 0o644
                backups[destination] = None
            else:
                mode = stat.S_IMODE(destination_stat.st_mode)
                original = _read_regular_bytes(destination, "artifact backup source")
                backups[destination] = _stage_temp(destination, original, mode)
            staged[destination] = _stage_temp(destination, content, mode)

        for destination, _ in items:
            _artifact_preflight(destination)
            os.replace(staged[destination], destination)
            committed.append(destination)
            _fsync_directory(destination.parent)
    except BaseException:
        for destination in reversed(committed):
            backup = backups[destination]
            if backup is None:
                with contextlib.suppress(FileNotFoundError):
                    destination.unlink()
            else:
                os.replace(backup, destination)
                _fsync_directory(destination.parent)
        raise
    finally:
        for temporary in (*staged.values(), *(item for item in backups.values() if item)):
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def _atomic_write(destination: Path, content: bytes) -> None:
    _atomic_write_all([(destination, content)])


def _synchronize(root: Path, check: bool) -> int:
    validated_root, rendered = _load_validate_render_all(root)
    artifacts = {
        skill: _managed_path(
            validated_root,
            Path("skills") / skill / "SKILL.md",
        )
        for skill in SKILL_NAMES
    }
    if check:
        mismatches: list[Path] = []
        for skill, destination in artifacts.items():
            try:
                actual = _read_regular_bytes(destination, f"{skill} artifact")
            except PathSecurityError as error:
                if "does not exist" not in str(error):
                    raise
                mismatches.append(destination)
                continue
            if actual != rendered[skill].content.encode("utf-8"):
                mismatches.append(destination)
        if mismatches:
            for path in mismatches:
                print(f"out of date: {path.relative_to(validated_root)}", file=sys.stderr)
            print("Run: python tools/render_skills.py", file=sys.stderr)
            return 1
        return 0

    encoded = {skill: rendered[skill].content.encode("utf-8") for skill in SKILL_NAMES}
    _atomic_write_all([(artifacts[skill], encoded[skill]) for skill in SKILL_NAMES])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and render failure-memory skills from contract.json.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Repository or plugin root; defaults to the parent of tools/.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report stale generated skills without writing files.",
    )
    args = parser.parse_args(argv)
    root = args.root if args.root is not None else Path(__file__).resolve().parents[1]
    try:
        return _synchronize(root, args.check)
    except RenderError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
