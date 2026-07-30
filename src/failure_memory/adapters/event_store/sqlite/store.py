from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TypeVar

from failure_memory.adapters.event_store.sqlite.errors import (
    SQLITE_BUSY_RETRY_DELAYS_SECONDS,
    is_sqlite_busy_error,
)
from failure_memory.adapters.harness.context import HarnessContext
from failure_memory.application.errors import StorageBusyError
from failure_memory.domain.capture import (
    CaptureAssessment,
    CaptureDecision,
    FailureCandidate,
)
from failure_memory.domain.ids import new_id
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
    IncidentLessonRelation,
    LessonDraft,
    LessonState,
    LessonVersionRecord,
    RecordingDisposition,
    RecordResult,
    lesson_signature,
)
from failure_memory.domain.retrieval import (
    RecallOutcome,
    RecallOutcomeKind,
    RecallTrace,
    RetrievalDocument,
    RetrievalProfile,
)
from failure_memory.ports.event_store import EventStorePort

_PROVENANCE = "local"
_AUTHORITATIVE_TABLES = (
    "capture_attempt",
    "incident",
    "lesson",
    "lesson_version",
    "incident_lesson_relation",
)
_RECALL_TABLES = (
    "retrieval_profile_snapshot",
    "recall_attempt",
    "recall_candidate",
    "recall_selection",
    "recall_outcome_event",
    "recall_miss_event",
)
_SELECTION_FEEDBACK_CTE = """
WITH selection_feedback AS (
    SELECT
        outcome.recall_attempt_id,
        outcome.lesson_version_id,
        MAX(outcome.outcome NOT IN ('ignored', 'unknown')) AS labeled,
        MAX(outcome.outcome IN ('useful', 'prevented_recurrence')) AS positive,
        MAX(outcome.outcome = 'false_positive') AS false_positive
    FROM recall_outcome_event AS outcome
    JOIN recall_selection AS selection
      ON selection.recall_attempt_id = outcome.recall_attempt_id
     AND selection.lesson_version_id = outcome.lesson_version_id
    WHERE outcome.lesson_version_id IS NOT NULL
    GROUP BY outcome.recall_attempt_id, outcome.lesson_version_id
)
"""
_ResultT = TypeVar("_ResultT")


class SQLiteEventStore(EventStorePort):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def append_capture(
        self,
        candidate: FailureCandidate,
        assessment: CaptureAssessment,
        context: HarnessContext,
        *,
        created_at: datetime,
        redaction_state: str,
    ) -> str:
        capture_id = new_id("cap")

        def insert() -> None:
            self.connection.execute(
                """
                INSERT INTO capture_attempt(
                    id, schema_version, created_at, source_harness, workspace_fingerprint,
                    session_fingerprint, provenance, redaction_state, summary, classification,
                    decision, confidence, reason_codes_json, expectation_source,
                    expectation_established_at, observed_outcome_at, failure_portion_summary,
                    policy_version
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture_id,
                    _timestamp(created_at),
                    context.harness,
                    context.workspace_fingerprint,
                    context.session_fingerprint,
                    _PROVENANCE,
                    redaction_state,
                    candidate.summary,
                    candidate.classification.value,
                    assessment.decision.value,
                    assessment.confidence,
                    json.dumps(
                        [code.value for code in assessment.reason_codes],
                        separators=(",", ":"),
                    ),
                    candidate.expectation_source.value,
                    _optional_timestamp(candidate.expectation_established_at),
                    _timestamp(candidate.observed_outcome_at),
                    candidate.failure_portion_summary,
                    assessment.policy_version,
                ),
            )

        self._retry_busy_write(insert)
        return capture_id

    def get_capture_decision(self, capture_attempt_id: str) -> CaptureDecision:
        row = self.connection.execute(
            "SELECT decision FROM capture_attempt WHERE id = ?", (capture_attempt_id,)
        ).fetchone()
        if row is None:
            raise ValueError("capture attempt not found")
        return CaptureDecision(str(row["decision"]))

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
    ) -> RecordResult:
        signature = lesson_signature(
            incident.expected_invariant,
            incident.controllable_cause,
            lesson.prevention_action,
        )

        def record() -> RecordResult:
            if self.get_capture_decision(capture_attempt_id) is not CaptureDecision.ACCEPT:
                raise ValueError("capture attempt is not accepted")
            return self._record_incident_and_lesson_once(
                capture_attempt_id,
                incident,
                lesson,
                context,
                created_at=created_at,
                redaction_state=redaction_state,
                signature=signature,
                generalization_review_id=generalization_review_id,
                disposition=disposition,
                target_lesson_version_id=target_lesson_version_id,
                rationale_code=rationale_code,
            )

        return self._retry_busy_write(record)

    def _record_incident_and_lesson_once(
        self,
        capture_attempt_id: str,
        incident: IncidentDraft,
        lesson: LessonDraft,
        context: HarnessContext,
        *,
        created_at: datetime,
        redaction_state: str,
        signature: str,
        generalization_review_id: str | None,
        disposition: RecordingDisposition | None,
        target_lesson_version_id: str | None,
        rationale_code: str | None,
    ) -> RecordResult:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.find_lesson_by_signature(signature)
            review = (
                None
                if generalization_review_id is None
                else self.get_generalization_review(generalization_review_id)
            )
            if review is not None:
                self._validate_generalization_decision(
                    review,
                    capture_attempt_id,
                    signature,
                    disposition,
                    target_lesson_version_id,
                    rationale_code,
                )
            incident_id = new_id("inc")
            self._insert_incident(
                incident_id,
                capture_attempt_id,
                incident,
                context,
                created_at,
                redaction_state,
            )
            effective_disposition = disposition
            if review is None:
                effective_disposition = (
                    RecordingDisposition.CREATE_DISTINCT
                    if existing is None
                    else RecordingDisposition.REUSE_EXISTING
                )
                target_lesson_version_id = None if existing is None else existing.id
            assert effective_disposition is not None
            if effective_disposition is RecordingDisposition.CREATE_DISTINCT:
                lesson_id = new_id("les")
                version_id = new_id("lv")
                self._insert_lesson(lesson_id, context, created_at, redaction_state)
                self._insert_lesson_version(
                    version_id,
                    lesson_id,
                    signature,
                    lesson,
                    context,
                    created_at,
                    redaction_state,
                )
                self._set_lesson_head(lesson_id, version_id, created_at)
                self._insert_signature_alias(
                    signature,
                    lesson_id,
                    version_id,
                    context,
                    created_at,
                    redaction_state,
                )
                relation = IncidentLessonRelation.NOVEL
                created_new_lesson = True
            else:
                assert target_lesson_version_id is not None
                target = self._current_lesson_for_version(target_lesson_version_id)
                lesson_id = target.lesson_id
                if effective_disposition is RecordingDisposition.GENERALIZE_EXISTING:
                    version_id = new_id("lv")
                    self._insert_lesson_version(
                        version_id,
                        lesson_id,
                        signature,
                        lesson,
                        context,
                        created_at,
                        redaction_state,
                        version_number=target.version_number + 1,
                    )
                    self.connection.execute(
                        """
                        UPDATE lesson_head
                        SET lesson_version_id = ?, updated_at = ?
                        WHERE lesson_id = ?
                        """,
                        (version_id, _timestamp(created_at), lesson_id),
                    )
                    relation = IncidentLessonRelation.REVIEWED_GENERALIZATION
                else:
                    version_id = target.id
                    relation = (
                        IncidentLessonRelation.SAME_CAUSE_SAME_INVARIANT
                        if target.signature == signature
                        else IncidentLessonRelation.REVIEWED_REUSE
                    )
                self._insert_signature_alias(
                    signature,
                    lesson_id,
                    version_id,
                    context,
                    created_at,
                    redaction_state,
                )
                created_new_lesson = False
            self._insert_relation(
                self._relation_id(),
                incident_id,
                lesson_id,
                version_id,
                relation,
                context,
                created_at,
                redaction_state,
            )
            decision_id: str | None = None
            if review is not None:
                decision_id = new_id("fgd")
                self.connection.execute(
                    """
                    INSERT INTO failure_generalization_decision_event(
                        id, schema_version, created_at, source_harness,
                        workspace_fingerprint, session_fingerprint, provenance,
                        redaction_state, review_id, disposition, rationale_code,
                        target_lesson_version_id, resulting_lesson_version_id
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        _timestamp(created_at),
                        context.harness,
                        context.workspace_fingerprint,
                        context.session_fingerprint,
                        _PROVENANCE,
                        redaction_state,
                        review.id,
                        effective_disposition.value,
                        rationale_code.strip() if rationale_code is not None else "",
                        target_lesson_version_id,
                        version_id,
                    ),
                )
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        return RecordResult(
            incident_id=incident_id,
            lesson_id=lesson_id,
            lesson_version_id=version_id,
            relation=relation,
            created_new_lesson=created_new_lesson,
            generalization_decision_id=decision_id,
        )

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
    ) -> GeneralizationReview:
        if self.get_capture_decision(capture_attempt_id) is not CaptureDecision.ACCEPT:
            raise ValueError("capture attempt is not accepted")
        candidate_ids = tuple(dict.fromkeys(candidate_lesson_version_ids))
        if len(candidate_ids) > 3:
            raise ValueError("generalization review accepts at most three candidates")
        if (
            recommendation is GeneralizationRecommendation.REUSE_EXACT
            and len(candidate_ids) != 1
        ):
            raise ValueError("exact reuse review requires exactly one candidate")
        if (
            recommendation is GeneralizationRecommendation.REVIEW_RELATED
            and not candidate_ids
        ):
            raise ValueError("related review requires at least one candidate")
        if (
            recommendation is GeneralizationRecommendation.CREATE_DISTINCT
            and candidate_ids
        ):
            raise ValueError("distinct review cannot include candidates")
        for candidate_id in candidate_ids:
            self._current_lesson_for_version(candidate_id)
        review = GeneralizationReview(
            id=new_id("fgr"),
            capture_attempt_id=capture_attempt_id,
            proposed_signature=proposed_signature,
            recommendation=recommendation,
            retrieval_profile=retrieval_profile,
            candidate_lesson_version_ids=candidate_ids,
        )

        def append() -> None:
            self.connection.execute(
                """
                INSERT INTO failure_generalization_review(
                    id, schema_version, created_at, source_harness,
                    workspace_fingerprint, session_fingerprint, provenance,
                    redaction_state, capture_attempt_id, proposed_signature,
                    recommendation, retrieval_profile,
                    candidate_lesson_version_ids_json, automatic_merge
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    review.id,
                    _timestamp(created_at),
                    context.harness,
                    context.workspace_fingerprint,
                    context.session_fingerprint,
                    _PROVENANCE,
                    redaction_state,
                    capture_attempt_id,
                    proposed_signature,
                    recommendation.value,
                    retrieval_profile,
                    json.dumps(review.candidate_lesson_version_ids, separators=(",", ":")),
                ),
            )

        self._retry_busy_write(append)
        return review

    def get_generalization_review(self, review_id: str) -> GeneralizationReview:
        row = self.connection.execute(
            """
            SELECT id, capture_attempt_id, proposed_signature, recommendation,
                   retrieval_profile, candidate_lesson_version_ids_json
            FROM failure_generalization_review
            WHERE id = ?
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            raise ValueError("generalization review not found")
        candidate_ids = json.loads(str(row["candidate_lesson_version_ids_json"]))
        if not isinstance(candidate_ids, list) or not all(
            isinstance(item, str) for item in candidate_ids
        ):
            raise ValueError("invalid generalization review candidates")
        return GeneralizationReview(
            id=str(row["id"]),
            capture_attempt_id=str(row["capture_attempt_id"]),
            proposed_signature=str(row["proposed_signature"]),
            recommendation=GeneralizationRecommendation(str(row["recommendation"])),
            retrieval_profile=str(row["retrieval_profile"]),
            candidate_lesson_version_ids=tuple(candidate_ids),
        )

    def _retry_busy_write(self, operation: Callable[[], _ResultT]) -> _ResultT:
        for attempt in range(len(SQLITE_BUSY_RETRY_DELAYS_SECONDS) + 1):
            try:
                return operation()
            except sqlite3.Error as error:
                if not is_sqlite_busy_error(error):
                    raise
                if self.connection.in_transaction:
                    self.connection.rollback()
                if attempt == len(SQLITE_BUSY_RETRY_DELAYS_SECONDS):
                    raise StorageBusyError(
                        "Failure-memory storage remained busy after bounded retries."
                    ) from error
                time.sleep(SQLITE_BUSY_RETRY_DELAYS_SECONDS[attempt])
        raise AssertionError("unreachable busy retry state")

    def find_lesson_by_signature(
        self,
        signature: str,
        workspace_fingerprint: str | None = None,
    ) -> LessonVersionRecord | None:
        del workspace_fingerprint  # Origin metadata never limits global reuse.
        row = self.connection.execute(
            """
            SELECT version.id, version.lesson_id, version.version_number, version.created_at,
                   version.lifecycle_state, version.signature, version.title, version.rule,
                   version.prevention_action, version.verification_action, version.applicability,
                   version.counterexamples
            FROM lesson_signature_alias AS alias
            JOIN lesson_head AS head ON head.lesson_id = alias.lesson_id
            JOIN lesson_version AS version ON version.id = head.lesson_version_id
            WHERE alias.signature = ?
            ORDER BY version.created_at, version.id
            LIMIT 1
            """,
            (signature,),
        ).fetchone()
        return None if row is None else _lesson_version_from_row(row)

    def _validate_generalization_decision(
        self,
        review: GeneralizationReview,
        capture_attempt_id: str,
        signature: str,
        disposition: RecordingDisposition | None,
        target_lesson_version_id: str | None,
        rationale_code: str | None,
    ) -> None:
        if review.capture_attempt_id != capture_attempt_id:
            raise ValueError("generalization review belongs to another capture")
        if review.proposed_signature != signature:
            raise ValueError("recording drafts differ from the reviewed failure")
        sanitized_rationale_code = (rationale_code or "").strip()
        if disposition is None or not sanitized_rationale_code:
            raise ValueError("reviewed recording requires disposition and rationale code")
        if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", sanitized_rationale_code) is None:
            raise ValueError("rationale code must be a bounded machine code")
        consumed = self.connection.execute(
            "SELECT 1 FROM failure_generalization_decision_event WHERE review_id = ?",
            (review.id,),
        ).fetchone()
        if consumed is not None:
            raise ValueError("generalization review already has a decision")
        if (
            review.recommendation is GeneralizationRecommendation.REUSE_EXACT
            and disposition is not RecordingDisposition.REUSE_EXISTING
        ):
            raise ValueError("an exact existing lesson must be reused without generalization")
        if disposition is RecordingDisposition.CREATE_DISTINCT:
            if target_lesson_version_id is not None:
                raise ValueError("distinct recording cannot target an existing lesson")
            return
        if target_lesson_version_id is None:
            raise ValueError("reuse and generalization require a target lesson version")
        if target_lesson_version_id not in review.candidate_lesson_version_ids:
            raise ValueError("target lesson was not part of the review")

    def _current_lesson_for_version(self, version_id: str) -> LessonVersionRecord:
        row = self.connection.execute(
            """
            SELECT version.*
            FROM lesson_version AS version
            JOIN lesson_head AS head
              ON head.lesson_id = version.lesson_id
             AND head.lesson_version_id = version.id
            WHERE version.id = ?
            """,
            (version_id,),
        ).fetchone()
        if row is None:
            raise ValueError("target lesson review is stale or invalid")
        return _lesson_version_from_row(row)

    def list_retrieval_documents(self) -> tuple[RetrievalDocument, ...]:
        rows = self.connection.execute(
            """
            SELECT version.id, version.lesson_id, version.version_number, version.created_at,
                   version.lifecycle_state, version.signature, version.title, version.rule,
                   version.prevention_action, version.verification_action, version.applicability,
                   version.counterexamples, version.workspace_fingerprint,
                   incident.expected_invariant, incident.controllable_cause,
                   incident.outcome_summary, incident.material_impact, incident.recurrence_risk
            FROM lesson_head AS head
            JOIN lesson_version AS version ON version.id = head.lesson_version_id
            JOIN incident_lesson_relation AS relation
              ON relation.rowid = (
                  SELECT MIN(candidate.rowid)
                  FROM incident_lesson_relation AS candidate
                  WHERE candidate.lesson_version_id = version.id
              )
            JOIN incident ON incident.id = relation.incident_id
            WHERE version.lifecycle_state NOT IN ('deprecated', 'superseded')
            ORDER BY version.created_at, version.id
            """
        ).fetchall()
        return tuple(_retrieval_document_from_row(row) for row in rows)

    def append_recall_trace(self, trace: RecallTrace, context: HarnessContext) -> None:
        def append() -> None:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                profile = trace.retrieval_profile
                profile_id = f"rp_{profile.config_fingerprint[:26]}"
                embedding = profile.embedding
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO retrieval_profile_snapshot(
                        id, schema_version, created_at, backend, profile_name,
                        config_fingerprint, capabilities_json, embedding_provider,
                        embedding_model, embedding_revision, embedding_dimensions
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_id,
                        trace.created_at,
                        profile.backend,
                        profile.name,
                        profile.config_fingerprint,
                        json.dumps(profile.capabilities, separators=(",", ":")),
                        None if embedding is None else embedding.provider,
                        None if embedding is None else embedding.model,
                        None if embedding is None else embedding.revision,
                        None if embedding is None else embedding.dimensions,
                    ),
                )
                self.connection.execute(
                    """
                    INSERT INTO recall_attempt(
                        id, schema_version, created_at, source_harness,
                        workspace_fingerprint, session_fingerprint, provenance,
                        redaction_state, requested_mode, executed_mode, status,
                        retrieval_profile_id, query_fingerprint, query_fields_json,
                        top_k, latency_ms, candidate_count
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, 'fingerprint_only', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trace.attempt_id,
                        trace.created_at,
                        context.harness,
                        context.workspace_fingerprint,
                        context.session_fingerprint,
                        _PROVENANCE,
                        trace.requested_mode.value,
                        trace.executed_mode.value,
                        trace.status.value,
                        profile_id,
                        trace.query_fingerprint,
                        json.dumps(trace.query_fields, separators=(",", ":")),
                        trace.top_k,
                        trace.latency_ms,
                        len(trace.candidates),
                    ),
                )
                selected_ranks = {
                    lesson_version_id: rank
                    for rank, lesson_version_id in enumerate(
                        trace.selected_lesson_version_ids,
                        start=1,
                    )
                }
                for rank, candidate in enumerate(trace.candidates, start=1):
                    candidate_id = new_id("rc")
                    selected_rank = selected_ranks.get(candidate.lesson.id)
                    self.connection.execute(
                        """
                        INSERT INTO recall_candidate(
                            id, schema_version, created_at, recall_attempt_id,
                            lesson_version_id, candidate_rank, channels_json,
                            exact_match, lexical_rank, semantic_rank, vector_distance,
                            fused_score, eligible, eligibility_reason
                        ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate_id,
                            trace.created_at,
                            trace.attempt_id,
                            candidate.lesson.id,
                            rank,
                            json.dumps(candidate.channels, separators=(",", ":")),
                            int(candidate.exact),
                            candidate.lexical_rank,
                            candidate.semantic_rank,
                            candidate.vector_distance,
                            candidate.score,
                            int(selected_rank is not None),
                            "returned" if selected_rank is not None else "below_top_k",
                        ),
                    )
                    if selected_rank is not None:
                        self.connection.execute(
                            """
                            INSERT INTO recall_selection(
                                id, schema_version, created_at, recall_attempt_id,
                                lesson_version_id, selection_rank, selection_reason
                            ) VALUES (?, 1, ?, ?, ?, ?, 'top_ranked')
                            """,
                            (
                                new_id("rs"),
                                trace.created_at,
                                trace.attempt_id,
                                candidate.lesson.id,
                                selected_rank,
                            ),
                        )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

        self._retry_busy_write(append)

    def append_recall_outcome(
        self,
        outcome: RecallOutcome,
        context: HarnessContext,
        *,
        created_at: datetime,
    ) -> str:
        outcome_id = new_id("ro")

        def append() -> None:
            attempt = self.connection.execute(
                """
                SELECT id
                FROM recall_attempt
                WHERE id = ?
                """,
                (outcome.attempt_id,),
            ).fetchone()
            if attempt is None:
                raise ValueError("recall attempt not found")
            if outcome.lesson_version_id is not None:
                if outcome.outcome is RecallOutcomeKind.MISSED_RELEVANT:
                    relevant = self.connection.execute(
                        "SELECT 1 FROM lesson_version WHERE id = ?",
                        (outcome.lesson_version_id,),
                    ).fetchone()
                    if relevant is None:
                        raise ValueError("relevant lesson version not found")
                else:
                    selected = self.connection.execute(
                        """
                        SELECT 1
                        FROM recall_selection
                        WHERE recall_attempt_id = ? AND lesson_version_id = ?
                        """,
                        (outcome.attempt_id, outcome.lesson_version_id),
                    ).fetchone()
                    if selected is None:
                        raise ValueError("lesson version was not selected by the recall attempt")
            if outcome.outcome is RecallOutcomeKind.MISSED_RELEVANT:
                if outcome.lesson_version_id is None:
                    raise ValueError("missed relevant feedback requires a lesson version")
                self.connection.execute(
                    """
                    INSERT INTO recall_miss_event(
                        id, schema_version, created_at, source_harness,
                        workspace_fingerprint, session_fingerprint, provenance,
                        redaction_state, recall_attempt_id, relevant_lesson_version_id,
                        detail_code, confidence
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, 'structured_only', ?, ?, ?, ?)
                    """,
                    (
                        outcome_id,
                        _timestamp(created_at),
                        context.harness,
                        context.workspace_fingerprint,
                        context.session_fingerprint,
                        _PROVENANCE,
                        outcome.attempt_id,
                        outcome.lesson_version_id,
                        outcome.detail_code,
                        outcome.confidence,
                    ),
                )
            else:
                self.connection.execute(
                    """
                    INSERT INTO recall_outcome_event(
                        id, schema_version, created_at, source_harness,
                        workspace_fingerprint, session_fingerprint, provenance,
                        redaction_state, recall_attempt_id, lesson_version_id,
                        outcome, detail_code, confidence
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, 'structured_only', ?, ?, ?, ?, ?)
                    """,
                    (
                        outcome_id,
                        _timestamp(created_at),
                        context.harness,
                        context.workspace_fingerprint,
                        context.session_fingerprint,
                        _PROVENANCE,
                        outcome.attempt_id,
                        outcome.lesson_version_id,
                        outcome.outcome.value,
                        outcome.detail_code,
                        outcome.confidence,
                    ),
                )

        self._retry_busy_write(append)
        return outcome_id

    def recall_counts(self) -> Mapping[str, int]:
        counts = {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _RECALL_TABLES
        }
        for status in (
            "ok",
            "no_match",
            "degraded",
            "setup_required",
            "insufficient_evidence",
        ):
            counts[f"attempt_status_{status}"] = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM recall_attempt WHERE status = ?",
                    (status,),
                ).fetchone()[0]
            )
        for outcome in (
            "useful",
            "not_useful",
            "false_positive",
            "prevented_recurrence",
            "contradicted_current_task",
            "stale",
            "ignored",
            "unknown",
        ):
            counts[f"outcome_{outcome}"] = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM recall_outcome_event WHERE outcome = ?",
                    (outcome,),
                ).fetchone()[0]
            )
        counts["outcome_missed_relevant"] = counts["recall_miss_event"]
        return counts

    def learning_metrics(self) -> Mapping[str, object]:
        attempt_count = _scalar_count(self.connection, "SELECT COUNT(*) FROM recall_attempt")
        selection_count = _scalar_count(self.connection, "SELECT COUNT(*) FROM recall_selection")
        labeled_selection_count = _scalar_count(
            self.connection,
            _SELECTION_FEEDBACK_CTE
            + """
            SELECT COUNT(*) FROM selection_feedback WHERE labeled
            """,
        )
        positive_selection_count = _scalar_count(
            self.connection,
            _SELECTION_FEEDBACK_CTE
            + """
            SELECT COUNT(*) FROM selection_feedback WHERE positive
            """,
        )
        false_positive_count = _scalar_count(
            self.connection,
            _SELECTION_FEEDBACK_CTE
            + """
            SELECT COUNT(*) FROM selection_feedback WHERE false_positive
            """,
        )
        labeled_attempt_count = _scalar_count(
            self.connection,
            """
            SELECT COUNT(DISTINCT recall_attempt_id)
            FROM (
                SELECT recall_attempt_id FROM recall_outcome_event
                UNION ALL
                SELECT recall_attempt_id FROM recall_miss_event
            )
            """,
        )
        exact_reuse_count = _scalar_count(
            self.connection,
            """
            SELECT COUNT(*) FROM incident_lesson_relation
            WHERE relation_type = 'same_cause_same_invariant'
            """,
        )
        relation_count = _scalar_count(
            self.connection,
            "SELECT COUNT(*) FROM incident_lesson_relation",
        )
        precision_at: dict[str, float | None] = {}
        for cutoff in (1, 3):
            labeled = _scalar_count(
                self.connection,
                _SELECTION_FEEDBACK_CTE
                + """
                SELECT COUNT(*)
                FROM selection_feedback AS outcome
                JOIN recall_selection AS selection
                  ON selection.recall_attempt_id = outcome.recall_attempt_id
                 AND selection.lesson_version_id = outcome.lesson_version_id
                WHERE selection.selection_rank <= ?
                  AND outcome.labeled
                """,
                (cutoff,),
            )
            positive = _scalar_count(
                self.connection,
                _SELECTION_FEEDBACK_CTE
                + """
                SELECT COUNT(*)
                FROM selection_feedback AS outcome
                JOIN recall_selection AS selection
                  ON selection.recall_attempt_id = outcome.recall_attempt_id
                 AND selection.lesson_version_id = outcome.lesson_version_id
                WHERE selection.selection_rank <= ?
                  AND outcome.positive
                """,
                (cutoff,),
            )
            precision_at[str(cutoff)] = _ratio(positive, labeled)
        harness_rows = self.connection.execute(
            """
            SELECT source_harness, COUNT(*) AS attempts
            FROM recall_attempt
            GROUP BY source_harness
            ORDER BY source_harness
            """
        ).fetchall()
        return {
            "scope": "global_personal",
            "attempt_count": attempt_count,
            "selection_count": selection_count,
            "labeled_attempt_count": labeled_attempt_count,
            "labeled_selection_count": labeled_selection_count,
            "positive_selection_count": positive_selection_count,
            "false_positive_count": false_positive_count,
            "missed_relevant_count": _scalar_count(
                self.connection, "SELECT COUNT(*) FROM recall_miss_event"
            ),
            "feedback_coverage": _ratio(labeled_attempt_count, attempt_count),
            "selection_feedback_coverage": _ratio(labeled_selection_count, selection_count),
            "useful_rate": _ratio(positive_selection_count, labeled_selection_count),
            "false_positive_rate": _ratio(false_positive_count, labeled_selection_count),
            "precision_at": precision_at,
            "exact_reuse_count": exact_reuse_count,
            "exact_reuse_rate": _ratio(exact_reuse_count, relation_count),
            "attempts_by_harness": {
                str(row["source_harness"]): int(row["attempts"]) for row in harness_rows
            },
        }

    def transition_lesson(
        self,
        lesson_id: str,
        target_state: LessonState,
        rationale_code: str,
        context: HarnessContext,
        *,
        created_at: datetime,
    ) -> LessonTransition:
        if target_state not in {
            LessonState.VERIFIED,
            LessonState.DEPRECATED,
            LessonState.SUPERSEDED,
        }:
            raise ValueError("target lifecycle state is not reviewable")
        if not rationale_code.strip():
            raise ValueError("lifecycle transition requires a rationale code")
        event_id = new_id("lle")
        new_version_id = new_id("lv")

        def transition() -> LessonTransition:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    """
                    SELECT version.*
                    FROM lesson_head AS head
                    JOIN lesson_version AS version
                      ON version.id = head.lesson_version_id
                    WHERE head.lesson_id = ?
                    """,
                    (lesson_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("lesson not found")
                current = _lesson_version_from_row(row)
                allowed = {
                    LessonState.PROPOSED: {
                        LessonState.VERIFIED,
                        LessonState.DEPRECATED,
                        LessonState.SUPERSEDED,
                    },
                    LessonState.VERIFIED: {
                        LessonState.DEPRECATED,
                        LessonState.SUPERSEDED,
                    },
                }
                if target_state not in allowed.get(current.state, set()):
                    raise ValueError("lesson lifecycle transition is not allowed")
                version_number = current.version_number + 1
                self.connection.execute(
                    """
                    INSERT INTO lesson_version(
                        id, schema_version, created_at, source_harness,
                        workspace_fingerprint, session_fingerprint, provenance,
                        redaction_state, lesson_id, version_number, lifecycle_state,
                        signature, title, rule, prevention_action, verification_action,
                        applicability, counterexamples
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, 'copied_redacted', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_version_id,
                        _timestamp(created_at),
                        context.harness,
                        context.workspace_fingerprint,
                        context.session_fingerprint,
                        _PROVENANCE,
                        lesson_id,
                        version_number,
                        target_state.value,
                        current.signature,
                        current.draft.title,
                        current.draft.rule,
                        current.draft.prevention_action,
                        current.draft.verification_action,
                        current.draft.applicability,
                        current.draft.counterexamples,
                    ),
                )
                self.connection.execute(
                    """
                    UPDATE lesson_head
                    SET lesson_version_id = ?, updated_at = ?
                    WHERE lesson_id = ?
                    """,
                    (new_version_id, _timestamp(created_at), lesson_id),
                )
                self.connection.execute(
                    """
                    INSERT INTO lesson_lifecycle_event(
                        id, schema_version, created_at, source_harness,
                        workspace_fingerprint, session_fingerprint, provenance,
                        lesson_id, prior_version_id, new_version_id, from_state,
                        to_state, rationale_code
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        _timestamp(created_at),
                        context.harness,
                        context.workspace_fingerprint,
                        context.session_fingerprint,
                        _PROVENANCE,
                        lesson_id,
                        current.id,
                        new_version_id,
                        current.state.value,
                        target_state.value,
                        rationale_code.strip(),
                    ),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            new_version = LessonVersionRecord(
                id=new_version_id,
                lesson_id=lesson_id,
                version_number=version_number,
                created_at=created_at,
                state=target_state,
                signature=current.signature,
                draft=current.draft,
            )
            return LessonTransition(
                lesson_id=lesson_id,
                prior_version_id=current.id,
                new_version=new_version,
                from_state=current.state,
                to_state=target_state,
                event_id=event_id,
            )

        return self._retry_busy_write(transition)

    def run_shadow_ranking_experiment(
        self,
        *,
        created_at: datetime,
    ) -> RankingExperimentResult:
        experiment_id = new_id("rxe")
        weights = self._lesson_feedback_weights()
        attempts = self.connection.execute(
            """
            SELECT DISTINCT recall_attempt_id
            FROM recall_candidate
            ORDER BY recall_attempt_id
            """
        ).fetchall()
        changed = 0
        for attempt in attempts:
            rows = self.connection.execute(
                """
                SELECT lesson_version_id, candidate_rank, fused_score
                FROM recall_candidate
                WHERE recall_attempt_id = ?
                ORDER BY candidate_rank
                """,
                (attempt["recall_attempt_id"],),
            ).fetchall()
            if not rows:
                continue
            baseline = str(rows[0]["lesson_version_id"])
            candidate = min(
                rows,
                key=lambda row: (
                    -(
                        float(row["fused_score"])
                        + 0.02 * weights.get(str(row["lesson_version_id"]), 0.0)
                    ),
                    int(row["candidate_rank"]),
                    str(row["lesson_version_id"]),
                ),
            )
            if str(candidate["lesson_version_id"]) != baseline:
                changed += 1
        metrics = dict(self.learning_metrics())
        labeled = _scalar_count(
            self.connection,
            _SELECTION_FEEDBACK_CTE
            + """
            SELECT COUNT(*) FROM selection_feedback WHERE labeled
            """,
        )
        self.connection.execute(
            """
            INSERT INTO ranking_experiment(
                id, schema_version, created_at, state, baseline_policy,
                candidate_policy, attempt_count, labeled_selection_count,
                changed_top_rank_count, metrics_json
            ) VALUES (?, 1, ?, 'shadow', 'exact-first-rrf-k60',
                      'exact-first-rrf-k60-feedback-capped-v1', ?, ?, ?, ?)
            """,
            (
                experiment_id,
                _timestamp(created_at),
                len(attempts),
                labeled,
                changed,
                json.dumps(metrics, sort_keys=True, separators=(",", ":")),
            ),
        )
        return RankingExperimentResult(
            experiment_id=experiment_id,
            attempt_count=len(attempts),
            labeled_selection_count=labeled,
            changed_top_rank_count=changed,
            baseline_policy="exact-first-rrf-k60",
            candidate_policy="exact-first-rrf-k60-feedback-capped-v1",
            metrics=metrics,
        )

    def append_cluster_run(
        self,
        profile: RetrievalProfile,
        distance_threshold: float,
        lesson_count: int,
        clusters: Sequence[LessonCluster],
        *,
        created_at: datetime,
    ) -> ClusterRunResult:
        if not 0 <= distance_threshold <= 2:
            raise ValueError("distance threshold must be between 0 and 2")
        run_id = new_id("lcr")

        def append() -> ClusterRunResult:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                profile_id = f"rp_{profile.config_fingerprint[:26]}"
                embedding = profile.embedding
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO retrieval_profile_snapshot(
                        id, schema_version, created_at, backend, profile_name,
                        config_fingerprint, capabilities_json, embedding_provider,
                        embedding_model, embedding_revision, embedding_dimensions
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_id,
                        _timestamp(created_at),
                        profile.backend,
                        profile.name,
                        profile.config_fingerprint,
                        json.dumps(profile.capabilities, separators=(",", ":")),
                        None if embedding is None else embedding.provider,
                        None if embedding is None else embedding.model,
                        None if embedding is None else embedding.revision,
                        None if embedding is None else embedding.dimensions,
                    ),
                )
                self.connection.execute(
                    """
                    INSERT INTO lesson_cluster_run(
                        id, schema_version, created_at, state, retrieval_profile_id,
                        distance_threshold, lesson_count, cluster_count
                    ) VALUES (?, 1, ?, 'proposed', ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        _timestamp(created_at),
                        profile_id,
                        distance_threshold,
                        lesson_count,
                        len(clusters),
                    ),
                )
                for cluster in clusters:
                    for lesson_version_id in cluster.lesson_version_ids:
                        self.connection.execute(
                            """
                            INSERT INTO lesson_cluster_member(
                                id, schema_version, created_at, cluster_run_id,
                                cluster_key, lesson_version_id
                            ) VALUES (?, 1, ?, ?, ?, ?)
                            """,
                            (
                                new_id("lcm"),
                                _timestamp(created_at),
                                run_id,
                                cluster.key,
                                lesson_version_id,
                            ),
                        )
                    self.connection.execute(
                        """
                        INSERT INTO lesson_generalization_proposal(
                            id, schema_version, created_at, cluster_run_id,
                            cluster_key, status, supporting_lesson_version_ids_json,
                            counterexample_lesson_version_ids_json
                        ) VALUES (?, 1, ?, ?, ?, 'proposed', ?, '[]')
                        """,
                        (
                            new_id("lgp"),
                            _timestamp(created_at),
                            run_id,
                            cluster.key,
                            json.dumps(
                                cluster.lesson_version_ids,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            return ClusterRunResult(
                run_id=run_id,
                retrieval_profile=profile.name,
                distance_threshold=distance_threshold,
                lesson_count=lesson_count,
                clusters=tuple(clusters),
            )

        return self._retry_busy_write(append)

    def _lesson_feedback_weights(self) -> dict[str, float]:
        rows = self.connection.execute(
            """
            SELECT lesson_version_id,
                   SUM(CASE WHEN outcome IN ('useful', 'prevented_recurrence')
                            THEN 1 ELSE 0 END) AS positive,
                   SUM(CASE WHEN outcome IN (
                       'not_useful', 'false_positive',
                       'contradicted_current_task', 'stale'
                   ) THEN 1 ELSE 0 END) AS negative
            FROM recall_outcome_event
            WHERE lesson_version_id IS NOT NULL
            GROUP BY lesson_version_id
            """
        ).fetchall()
        return {
            str(row["lesson_version_id"]): (
                (int(row["positive"]) - int(row["negative"]))
                / (int(row["positive"]) + int(row["negative"]) + 2)
            )
            for row in rows
        }

    def counts(self) -> Mapping[str, int]:
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _AUTHORITATIVE_TABLES
        }

    def integrity_check(self) -> str:
        return str(self.connection.execute("PRAGMA integrity_check").fetchone()[0])

    def database_path(self) -> str:
        row = self.connection.execute("PRAGMA database_list").fetchone()
        if row is None:
            raise RuntimeError("SQLite main database is unavailable")
        return str(row["file"])

    def _insert_incident(
        self,
        incident_id: str,
        capture_attempt_id: str,
        incident: IncidentDraft,
        context: HarnessContext,
        created_at: datetime,
        redaction_state: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO incident(
                id, schema_version, created_at, source_harness, workspace_fingerprint,
                session_fingerprint, provenance, redaction_state, capture_attempt_id,
                outcome_summary, expected_invariant, controllable_cause, material_impact,
                recurrence_risk
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident_id,
                _timestamp(created_at),
                context.harness,
                context.workspace_fingerprint,
                context.session_fingerprint,
                _PROVENANCE,
                redaction_state,
                capture_attempt_id,
                incident.outcome_summary,
                incident.expected_invariant,
                incident.controllable_cause,
                incident.material_impact,
                incident.recurrence_risk,
            ),
        )

    def _insert_lesson(
        self,
        lesson_id: str,
        context: HarnessContext,
        created_at: datetime,
        redaction_state: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO lesson(
                id, schema_version, created_at, source_harness, workspace_fingerprint,
                session_fingerprint, provenance, redaction_state
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                lesson_id,
                _timestamp(created_at),
                context.harness,
                context.workspace_fingerprint,
                context.session_fingerprint,
                _PROVENANCE,
                redaction_state,
            ),
        )

    def _insert_lesson_version(
        self,
        version_id: str,
        lesson_id: str,
        signature: str,
        lesson: LessonDraft,
        context: HarnessContext,
        created_at: datetime,
        redaction_state: str,
        *,
        version_number: int = 1,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO lesson_version(
                id, schema_version, created_at, source_harness, workspace_fingerprint,
                session_fingerprint, provenance, redaction_state, lesson_id, version_number,
                lifecycle_state, signature, title, rule, prevention_action, verification_action,
                applicability, counterexamples
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                _timestamp(created_at),
                context.harness,
                context.workspace_fingerprint,
                context.session_fingerprint,
                _PROVENANCE,
                redaction_state,
                lesson_id,
                version_number,
                LessonState.PROPOSED.value,
                signature,
                lesson.title,
                lesson.rule,
                lesson.prevention_action,
                lesson.verification_action,
                lesson.applicability,
                lesson.counterexamples,
            ),
        )

    def _insert_signature_alias(
        self,
        signature: str,
        lesson_id: str,
        version_id: str,
        context: HarnessContext,
        created_at: datetime,
        redaction_state: str,
    ) -> None:
        existing = self.connection.execute(
            "SELECT lesson_id FROM lesson_signature_alias WHERE signature = ?",
            (signature,),
        ).fetchone()
        if existing is not None:
            if str(existing["lesson_id"]) != lesson_id:
                raise ValueError("lesson signature already belongs to another lesson")
            return
        self.connection.execute(
            """
            INSERT INTO lesson_signature_alias(
                id, schema_version, created_at, source_harness,
                workspace_fingerprint, session_fingerprint, provenance,
                redaction_state, signature, lesson_id, source_lesson_version_id
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("lsa"),
                _timestamp(created_at),
                context.harness,
                context.workspace_fingerprint,
                context.session_fingerprint,
                _PROVENANCE,
                redaction_state,
                signature,
                lesson_id,
                version_id,
            ),
        )

    def _set_lesson_head(self, lesson_id: str, version_id: str, created_at: datetime) -> None:
        self.connection.execute(
            "INSERT INTO lesson_head(lesson_id, lesson_version_id, updated_at) VALUES (?, ?, ?)",
            (lesson_id, version_id, _timestamp(created_at)),
        )

    def _insert_relation(
        self,
        relation_id: str,
        incident_id: str,
        lesson_id: str,
        version_id: str,
        relation: IncidentLessonRelation,
        context: HarnessContext,
        created_at: datetime,
        redaction_state: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO incident_lesson_relation(
                id, schema_version, created_at, source_harness, workspace_fingerprint,
                session_fingerprint, provenance, redaction_state, incident_id, lesson_id,
                lesson_version_id, relation_type, confidence
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relation_id,
                _timestamp(created_at),
                context.harness,
                context.workspace_fingerprint,
                context.session_fingerprint,
                _PROVENANCE,
                redaction_state,
                incident_id,
                lesson_id,
                version_id,
                relation.value,
                1.0,
            ),
        )

    def _relation_id(self) -> str:
        return new_id("rel")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _optional_timestamp(value: datetime | None) -> str | None:
    return None if value is None else _timestamp(value)


def _scalar_count(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...] = (),
) -> int:
    row = connection.execute(statement, parameters).fetchone()
    return 0 if row is None else int(row[0])


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _lesson_version_from_row(row: sqlite3.Row) -> LessonVersionRecord:
    return LessonVersionRecord(
        id=str(row["id"]),
        lesson_id=str(row["lesson_id"]),
        version_number=int(row["version_number"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        state=LessonState(str(row["lifecycle_state"])),
        signature=str(row["signature"]),
        draft=LessonDraft(
            title=str(row["title"]),
            rule=str(row["rule"]),
            prevention_action=str(row["prevention_action"]),
            verification_action=str(row["verification_action"]),
            applicability=str(row["applicability"]),
            counterexamples=str(row["counterexamples"]),
        ),
    )


def _retrieval_document_from_row(row: sqlite3.Row) -> RetrievalDocument:
    return RetrievalDocument(
        lesson_version=_lesson_version_from_row(row),
        workspace_fingerprint=str(row["workspace_fingerprint"]),
        expected_invariant=str(row["expected_invariant"]),
        controllable_cause=str(row["controllable_cause"]),
        outcome_summary=str(row["outcome_summary"]),
        material_impact=str(row["material_impact"]),
        recurrence_risk=str(row["recurrence_risk"]),
    )
