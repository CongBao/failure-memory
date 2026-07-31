from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from failure_memory.domain.records import LessonVersionRecord


class RecallMode(StrEnum):
    AUTO = "auto"
    EXACT = "exact"
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class RecallStatus(StrEnum):
    OK = "ok"
    NO_MATCH = "no_match"
    DEGRADED = "degraded"
    SETUP_REQUIRED = "setup_required"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RecallOutcomeKind(StrEnum):
    USEFUL = "useful"
    NOT_USEFUL = "not_useful"
    FALSE_POSITIVE = "false_positive"
    PREVENTED_RECURRENCE = "prevented_recurrence"
    CONTRADICTED_CURRENT_TASK = "contradicted_current_task"
    STALE = "stale"
    IGNORED = "ignored"
    MISSED_RELEVANT = "missed_relevant"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EmbeddingSpec:
    provider: str
    model: str
    revision: str
    dimensions: int
    distance: str = "cosine"

    @property
    def profile_name(self) -> str:
        safe_model = self.model.replace("/", "--")
        return f"{self.provider}-{safe_model}-{self.revision}-{self.dimensions}"


@dataclass(frozen=True, slots=True)
class RecallQuery:
    mode: RecallMode = RecallMode.AUTO
    text: str | None = None
    expected_invariant: str | None = None
    controllable_cause: str | None = None
    prevention_action: str | None = None
    component: str | None = None
    top_k: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.top_k <= 5:
            raise ValueError("top_k must be between 1 and 5")
        if not any(
            value is not None and value.strip()
            for value in (
                self.text,
                self.expected_invariant,
                self.controllable_cause,
                self.prevention_action,
                self.component,
            )
        ):
            raise ValueError("recall query must contain at least one non-empty field")

    @property
    def has_exact_signature(self) -> bool:
        return all(
            value is not None and value.strip()
            for value in (
                self.expected_invariant,
                self.controllable_cause,
                self.prevention_action,
            )
        )

    @property
    def has_similarity_evidence(self) -> bool:
        has_context = self.text is not None and bool(self.text.strip())
        has_discriminator = any(
            value is not None and value.strip()
            for value in (
                self.expected_invariant,
                self.controllable_cause,
                self.prevention_action,
                self.component,
            )
        )
        return has_context and has_discriminator

    def canonical_text(self) -> str:
        labelled = (
            ("task", self.text),
            ("expected invariant", self.expected_invariant),
            ("controllable cause", self.controllable_cause),
            ("prevention action", self.prevention_action),
            ("component", self.component),
        )
        return "\n".join(
            f"{label}: {value.strip()}" for label, value in labelled if value and value.strip()
        )

    def field_presence(self) -> tuple[str, ...]:
        values = (
            ("text", self.text),
            ("expected_invariant", self.expected_invariant),
            ("controllable_cause", self.controllable_cause),
            ("prevention_action", self.prevention_action),
            ("component", self.component),
        )
        return tuple(name for name, value in values if value is not None and value.strip())


@dataclass(frozen=True, slots=True)
class RetrievalDocument:
    lesson_version: LessonVersionRecord
    # Historical schema name retained as origin provenance. Retrieval is global.
    workspace_fingerprint: str
    expected_invariant: str
    controllable_cause: str
    outcome_summary: str
    material_impact: str
    recurrence_risk: str

    def canonical_text(self) -> str:
        lesson = self.lesson_version.draft
        fields = (
            ("title", lesson.title),
            ("rule", lesson.rule),
            ("expected invariant", self.expected_invariant),
            ("controllable cause", self.controllable_cause),
            ("outcome", self.outcome_summary),
            ("prevention action", lesson.prevention_action),
            ("verification action", lesson.verification_action),
            ("applicability", lesson.applicability),
            ("counterexamples", lesson.counterexamples),
            ("material impact", self.material_impact),
            ("recurrence risk", self.recurrence_risk),
        )
        return "\n".join(f"{label}: {value.strip()}" for label, value in fields)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_text().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RetrievalMatch:
    lesson_version_id: str
    channel: str
    rank: int
    score: float
    distance: float | None = None


@dataclass(frozen=True, slots=True)
class RetrievalIndexStatus:
    state: str
    profile: str
    lexical_available: bool
    semantic_available: bool
    indexed_documents: int
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalProfile:
    name: str
    backend: str
    config_fingerprint: str
    capabilities: tuple[str, ...]
    embedding: EmbeddingSpec | None = None


@dataclass(frozen=True, slots=True)
class RecallCandidate:
    lesson: LessonVersionRecord
    expected_invariant: str
    controllable_cause: str
    outcome_summary: str
    channels: tuple[str, ...]
    score: float
    exact: bool
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    vector_distance: float | None = None
    cluster_review_id: str | None = None
    cluster_key: str | None = None
    cluster_supporting_lesson_version_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecallResult:
    attempt_id: str
    requested_mode: RecallMode
    executed_mode: RecallMode
    status: RecallStatus
    candidates: tuple[RecallCandidate, ...]
    retrieval_profile: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RecallTrace:
    attempt_id: str
    requested_mode: RecallMode
    executed_mode: RecallMode
    status: RecallStatus
    retrieval_profile: RetrievalProfile
    query_fingerprint: str
    query_fields: tuple[str, ...]
    top_k: int
    latency_ms: int
    candidates: tuple[RecallCandidate, ...]
    selected_lesson_version_ids: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class RecallOutcome:
    attempt_id: str
    outcome: RecallOutcomeKind
    lesson_version_id: str | None = None
    detail_code: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if self.outcome is RecallOutcomeKind.MISSED_RELEVANT and self.lesson_version_id is None:
            raise ValueError("missed relevant feedback requires a lesson version")


def retrieval_profile_fingerprint(
    *,
    backend: str,
    embedding: EmbeddingSpec | None,
    lexical: str,
    fusion: str,
) -> str:
    payload = {
        "backend": backend,
        "embedding": None
        if embedding is None
        else {
            "provider": embedding.provider,
            "model": embedding.model,
            "revision": embedding.revision,
            "dimensions": embedding.dimensions,
            "distance": embedding.distance,
        },
        "lexical": lexical,
        "fusion": fusion,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
