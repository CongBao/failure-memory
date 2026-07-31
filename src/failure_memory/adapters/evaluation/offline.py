from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

from failure_memory.adapters.storage_permissions import (
    ensure_private_file,
    ensure_private_tree,
)
from failure_memory.domain.capture import (
    Classification,
    ExpectationSource,
    FailureCandidate,
)
from failure_memory.domain.ids import new_id
from failure_memory.domain.policy import evaluate_candidate
from failure_memory.json_codec import load_json
from failure_memory.mcp.rfc3339 import parse_rfc3339_date_time

_BASELINE_POLICY = "exact-first-rrf-k60"
_CANDIDATE_POLICY = "exact-first-rrf-k60-reviewed-cluster-v1"
_MACHINE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_THRESHOLDS: dict[str, float | int] = {
    "minimum_case_count": 50,
    "minimum_negative_case_count": 20,
    "capture_accuracy": 1.0,
    "requirement_update_false_positive_count": 0,
    "precision_at_1": 0.85,
    "precision_at_3": 0.95,
    "negative_no_injection_accuracy": 0.95,
}


def run_offline_evaluation(
    corpus_path: Path,
    data_root: Path,
    *,
    created_at: datetime,
) -> dict[str, object]:
    corpus = _load_corpus(corpus_path)
    capture_cases = _object_list(corpus, "capture_cases")
    recall_cases = _object_list(corpus, "recall_cases")
    case_ids = [_machine_code(case, "id") for case in (*capture_cases, *recall_cases)]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("evaluation case ids must be unique")
    case_count = len(capture_cases) + len(recall_cases)
    negative_case_count = sum(
        int(_required_bool(case, "negative")) for case in (*capture_cases, *recall_cases)
    )

    capture_correct = 0
    requirement_update_false_positives = 0
    for case in capture_cases:
        candidate = _candidate(_required_object(case, "candidate"))
        actual = evaluate_candidate(candidate).decision.value
        expected = _required_string(case, "expected_decision")
        if expected not in {"accept", "reject", "defer"}:
            raise ValueError("capture expected_decision has an invalid value")
        capture_correct += int(actual == expected)
        if (
            candidate.classification
            in {
                Classification.REQUIREMENT_UPDATE,
                Classification.REQUIREMENT_CLARIFICATION,
            }
            and actual == "accept"
        ):
            requirement_update_false_positives += 1

    positive_recall_cases = 0
    hit_at_1 = 0
    hit_at_3 = 0
    negative_recall_cases = 0
    negative_no_injection = 0
    for case in recall_cases:
        selected = _rank_recall_case(case)
        relevant = set(_string_list(case, "relevant"))
        negative = _required_bool(case, "negative")
        if negative:
            negative_recall_cases += 1
            negative_no_injection += int(not selected)
            continue
        positive_recall_cases += 1
        hit_at_1 += int(bool(relevant.intersection(selected[:1])))
        hit_at_3 += int(bool(relevant.intersection(selected[:3])))

    metrics: dict[str, object] = {
        "capture_accuracy": _ratio(capture_correct, len(capture_cases)),
        "requirement_update_false_positive_count": requirement_update_false_positives,
        "precision_at_1": _ratio(hit_at_1, positive_recall_cases),
        "precision_at_3": _ratio(hit_at_3, positive_recall_cases),
        "negative_no_injection_accuracy": _ratio(
            negative_no_injection,
            negative_recall_cases,
        ),
    }
    threshold_failures = _threshold_failures(
        case_count,
        negative_case_count,
        metrics,
    )
    corpus_fingerprint = hashlib.sha256(
        json.dumps(corpus, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    run_id = new_id("ler")
    report: dict[str, object] = {
        "run_id": run_id,
        "state": "shadow",
        "created_at": created_at.isoformat(),
        "corpus_name": _machine_code(corpus, "name"),
        "corpus_version": _machine_code(corpus, "version"),
        "corpus_fingerprint": corpus_fingerprint,
        "baseline_policy": _BASELINE_POLICY,
        "candidate_policy": _CANDIDATE_POLICY,
        "case_count": case_count,
        "negative_case_count": negative_case_count,
        "metrics": metrics,
        "thresholds": dict(_THRESHOLDS),
        "threshold_failures": threshold_failures,
        "passed": not threshold_failures,
        "production_activated": False,
    }
    report_parent = ensure_private_tree(
        data_root,
        "adapters",
        "evaluation",
        "offline",
        run_id,
    )
    report_path = report_parent / "report.json"
    _write_private_json(report_path, report)
    return report


def _load_corpus(path: Path) -> Mapping[str, object]:
    with path.open(encoding="utf-8") as stream:
        value = load_json(stream)
    if not isinstance(value, Mapping):
        raise ValueError("evaluation corpus must be an object")
    allowed = {
        "schema_version",
        "name",
        "version",
        "capture_cases",
        "recall_cases",
    }
    if set(value) != allowed:
        raise ValueError("evaluation corpus fields do not match schema version 1")
    if value["schema_version"] != 1:
        raise ValueError("evaluation corpus schema version is unsupported")
    _machine_code(value, "name")
    _machine_code(value, "version")
    return cast(Mapping[str, object], value)


def _candidate(value: Mapping[str, object]) -> FailureCandidate:
    allowed = {
        "summary",
        "classification",
        "expectation_source",
        "expectation_established_at",
        "observed_outcome_at",
        "outcome_mismatch",
        "material_impact_or_recurrence_risk",
        "controllable_with_prior_information",
        "durable_lesson",
        "failure_portion_summary",
    }
    required = allowed - {
        "expectation_established_at",
        "failure_portion_summary",
    }
    if not required <= set(value) or set(value) - allowed:
        raise ValueError("evaluation capture candidate fields are invalid")
    try:
        classification = Classification(_required_string(value, "classification"))
        expectation_source = ExpectationSource(_required_string(value, "expectation_source"))
    except ValueError as exc:
        raise ValueError("evaluation capture candidate enum is invalid") from exc
    established = value.get("expectation_established_at")
    return FailureCandidate(
        summary=_required_string(value, "summary"),
        classification=classification,
        expectation_source=expectation_source,
        expectation_established_at=(
            None
            if established is None
            else parse_rfc3339_date_time(_required_string(value, "expectation_established_at"))
        ),
        observed_outcome_at=parse_rfc3339_date_time(_required_string(value, "observed_outcome_at")),
        outcome_mismatch=_required_bool(value, "outcome_mismatch"),
        material_impact_or_recurrence_risk=_required_bool(
            value,
            "material_impact_or_recurrence_risk",
        ),
        controllable_with_prior_information=_required_bool(
            value,
            "controllable_with_prior_information",
        ),
        durable_lesson=_required_bool(value, "durable_lesson"),
        failure_portion_summary=(
            None
            if "failure_portion_summary" not in value
            else _required_string(value, "failure_portion_summary")
        ),
    )


def _rank_recall_case(case: Mapping[str, object]) -> tuple[str, ...]:
    allowed = {
        "id",
        "exact",
        "lexical",
        "semantic",
        "accepted_clusters",
        "relevant",
        "top_k",
        "negative",
    }
    required = allowed - {"exact"}
    if not required <= set(case) or set(case) - allowed:
        raise ValueError("evaluation recall case fields are invalid")
    top_k = case["top_k"]
    if type(top_k) is not int or not 1 <= top_k <= 5:
        raise ValueError("evaluation recall top_k must be between 1 and 5")
    exact = _string_list(case, "exact") if "exact" in case else []
    if exact:
        return tuple(exact[:top_k])
    ranks: dict[str, float] = {}
    for channel in ("lexical", "semantic"):
        for rank, lesson_id in enumerate(_string_list(case, channel), start=1):
            ranks[lesson_id] = ranks.get(lesson_id, 0.0) + 1.0 / (60 + rank)
    ranked = [
        lesson_id
        for lesson_id, _score in sorted(
            ranks.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    for cluster in _object_list(case, "accepted_clusters"):
        if set(cluster) != {"reviewed", "members"}:
            raise ValueError("evaluation accepted cluster fields are invalid")
        if not _required_bool(cluster, "reviewed"):
            continue
        members = _string_list(cluster, "members")
        if not set(members).intersection(ranked):
            continue
        neighbor = next(
            (lesson_id for lesson_id in sorted(members) if lesson_id not in ranked),
            None,
        )
        if neighbor is not None:
            ranked.append(neighbor)
    return tuple(ranked[:top_k])


def _threshold_failures(
    case_count: int,
    negative_case_count: int,
    metrics: Mapping[str, object],
) -> list[str]:
    failures: list[str] = []
    if case_count < int(_THRESHOLDS["minimum_case_count"]):
        failures.append("minimum_case_count")
    if negative_case_count < int(_THRESHOLDS["minimum_negative_case_count"]):
        failures.append("minimum_negative_case_count")
    for metric in (
        "capture_accuracy",
        "precision_at_1",
        "precision_at_3",
        "negative_no_injection_accuracy",
    ):
        value = metrics[metric]
        if not isinstance(value, (int, float)) or value < float(_THRESHOLDS[metric]):
            failures.append(metric)
    false_positives = metrics["requirement_update_false_positive_count"]
    false_positive_threshold = _THRESHOLDS["requirement_update_false_positive_count"]
    if (
        type(false_positives) is not int
        or type(false_positive_threshold) is not int
        or false_positives != false_positive_threshold
    ):
        failures.append("requirement_update_false_positive_count")
    return failures


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    ensure_private_file(path)


def _object_list(value: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    items = value.get(key)
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise ValueError(f"{key} must be an array of objects")
    return [cast(Mapping[str, object], item) for item in items]


def _string_list(value: Mapping[str, object], key: str) -> list[str]:
    items = value.get(key)
    if not isinstance(items, list) or not all(
        isinstance(item, str) and item.strip() for item in items
    ):
        raise ValueError(f"{key} must be an array of non-empty strings")
    if len(items) != len(set(items)):
        raise ValueError(f"{key} must not contain duplicates")
    return cast(list[str], items)


def _required_object(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise ValueError(f"{key} must be an object")
    return cast(Mapping[str, object], nested)


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _machine_code(value: Mapping[str, object], key: str) -> str:
    item = _required_string(value, key)
    if _MACHINE_CODE.fullmatch(item) is None:
        raise ValueError(f"{key} must be a bounded machine-safe identifier")
    return item


def _required_bool(value: Mapping[str, object], key: str) -> bool:
    item = value.get(key)
    if type(item) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return item


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator
