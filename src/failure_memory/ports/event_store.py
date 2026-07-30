from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from failure_memory.adapters.harness.context import HarnessContext
from failure_memory.domain.capture import (
    CaptureAssessment,
    CaptureDecision,
    FailureCandidate,
)
from failure_memory.domain.learning import (
    ClusterRunResult,
    GeneralizationRecommendation,
    GeneralizationReview,
    LessonCluster,
    LessonTransition,
    RankingExperimentResult,
)
from failure_memory.domain.records import (
    IncidentDraft,
    LessonDraft,
    LessonState,
    LessonVersionRecord,
    RecordingDisposition,
    RecordResult,
)
from failure_memory.domain.retrieval import (
    RecallOutcome,
    RecallTrace,
    RetrievalDocument,
    RetrievalProfile,
)


class EventStorePort(Protocol):
    def append_capture(
        self,
        candidate: FailureCandidate,
        assessment: CaptureAssessment,
        context: HarnessContext,
        *,
        created_at: datetime,
        redaction_state: str,
    ) -> str: ...

    def get_capture_decision(self, capture_attempt_id: str) -> CaptureDecision: ...

    def record_incident_and_lesson(
        self,
        capture_attempt_id: str,
        incident: IncidentDraft,
        lesson: LessonDraft,
        context: HarnessContext,
        *,
        created_at: datetime,
        redaction_state: str,
        generalization_review_id: str | None = None,
        disposition: RecordingDisposition | None = None,
        target_lesson_version_id: str | None = None,
        rationale_code: str | None = None,
    ) -> RecordResult: ...

    def append_generalization_review(
        self,
        capture_attempt_id: str,
        proposed_signature: str,
        recommendation: GeneralizationRecommendation,
        retrieval_profile: str,
        candidate_lesson_version_ids: Sequence[str],
        context: HarnessContext,
        *,
        created_at: datetime,
        redaction_state: str,
    ) -> GeneralizationReview: ...

    def get_generalization_review(self, review_id: str) -> GeneralizationReview: ...

    def find_lesson_by_signature(
        self,
        signature: str,
        workspace_fingerprint: str | None = None,
    ) -> LessonVersionRecord | None: ...

    def list_retrieval_documents(self) -> Sequence[RetrievalDocument]: ...

    def append_recall_trace(self, trace: RecallTrace, context: HarnessContext) -> None: ...

    def append_recall_outcome(
        self,
        outcome: RecallOutcome,
        context: HarnessContext,
        *,
        created_at: datetime,
    ) -> str: ...

    def recall_counts(self) -> Mapping[str, int]: ...

    def counts(self) -> Mapping[str, int]: ...

    def learning_metrics(self) -> Mapping[str, object]: ...

    def transition_lesson(
        self,
        lesson_id: str,
        target_state: LessonState,
        rationale_code: str,
        context: HarnessContext,
        *,
        created_at: datetime,
    ) -> LessonTransition: ...

    def run_shadow_ranking_experiment(
        self,
        *,
        created_at: datetime,
    ) -> RankingExperimentResult: ...

    def append_cluster_run(
        self,
        profile: RetrievalProfile,
        distance_threshold: float,
        lesson_count: int,
        clusters: Sequence[LessonCluster],
        *,
        created_at: datetime,
    ) -> ClusterRunResult: ...

    def integrity_check(self) -> str: ...

    def database_path(self) -> str: ...
