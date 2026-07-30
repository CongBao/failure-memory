from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from failure_memory.domain.records import LessonState, LessonVersionRecord


class GeneralizationRecommendation(StrEnum):
    REUSE_EXACT = "reuse_exact"
    REVIEW_RELATED = "review_related"
    CREATE_DISTINCT = "create_distinct"


@dataclass(frozen=True, slots=True)
class GeneralizationReview:
    id: str
    capture_attempt_id: str
    proposed_signature: str
    recommendation: GeneralizationRecommendation
    retrieval_profile: str
    candidate_lesson_version_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LessonTransition:
    lesson_id: str
    prior_version_id: str
    new_version: LessonVersionRecord
    from_state: LessonState
    to_state: LessonState
    event_id: str


@dataclass(frozen=True, slots=True)
class RankingExperimentResult:
    experiment_id: str
    attempt_count: int
    labeled_selection_count: int
    changed_top_rank_count: int
    baseline_policy: str
    candidate_policy: str
    metrics: dict[str, object]


@dataclass(frozen=True, slots=True)
class LessonCluster:
    key: str
    lesson_version_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SimilarityPair:
    left_lesson_version_id: str
    right_lesson_version_id: str
    distance: float


@dataclass(frozen=True, slots=True)
class ClusterRunResult:
    run_id: str
    retrieval_profile: str
    distance_threshold: float
    lesson_count: int
    clusters: tuple[LessonCluster, ...]
