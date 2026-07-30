from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class LessonState(StrEnum):
    PROPOSED = "proposed"
    VERIFIED = "verified"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"


class IncidentLessonRelation(StrEnum):
    SAME_CAUSE_SAME_INVARIANT = "same_cause_same_invariant"
    NOVEL = "novel"


@dataclass(frozen=True)
class IncidentDraft:
    outcome_summary: str
    expected_invariant: str
    controllable_cause: str
    material_impact: str
    recurrence_risk: str


@dataclass(frozen=True)
class LessonDraft:
    title: str
    rule: str
    prevention_action: str
    verification_action: str
    applicability: str
    counterexamples: str


@dataclass(frozen=True)
class IncidentRecord:
    id: str
    capture_attempt_id: str
    created_at: datetime
    draft: IncidentDraft


@dataclass(frozen=True)
class LessonRecord:
    id: str
    created_at: datetime


@dataclass(frozen=True)
class LessonVersionRecord:
    id: str
    lesson_id: str
    version_number: int
    created_at: datetime
    state: LessonState
    signature: str
    draft: LessonDraft


@dataclass(frozen=True)
class RecordResult:
    incident_id: str
    lesson_id: str
    lesson_version_id: str
    relation: IncidentLessonRelation
    created_new_lesson: bool


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def lesson_signature(
    expected_invariant: str,
    controllable_cause: str,
    prevention_action: str,
) -> str:
    payload = "\x1f".join(
        _normalize(value) for value in (expected_invariant, controllable_cause, prevention_action)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
