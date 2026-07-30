from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from failure_memory.adapters.dependency_runtime.manager import AdapterRuntimeManager
from failure_memory.adapters.event_store.sqlite.connection import (
    connect_sqlite,
    secure_sqlite_files,
)
from failure_memory.adapters.event_store.sqlite.errors import is_sqlite_busy_error
from failure_memory.adapters.event_store.sqlite.importer import (
    GlobalStoreImporter,
    discover_legacy_databases,
)
from failure_memory.adapters.event_store.sqlite.migrate import apply_migrations
from failure_memory.adapters.event_store.sqlite.store import SQLiteEventStore
from failure_memory.adapters.harness.context import HarnessContext, resolve_data_root
from failure_memory.adapters.retrieval.sqlite import SQLiteRetrievalIndex
from failure_memory.adapters.storage_permissions import ensure_private_tree
from failure_memory.application.errors import SemanticSetupRequiredError, StorageBusyError
from failure_memory.application.redaction import RedactionResult, redact_text
from failure_memory.domain.capture import CaptureAssessment, FailureCandidate
from failure_memory.domain.learning import LessonCluster
from failure_memory.domain.policy import evaluate_candidate
from failure_memory.domain.records import (
    IncidentDraft,
    LessonDraft,
    LessonState,
    LessonVersionRecord,
    RecordResult,
    lesson_signature,
)
from failure_memory.domain.retrieval import (
    RecallCandidate,
    RecallMode,
    RecallOutcome,
    RecallQuery,
    RecallResult,
    RecallStatus,
    RecallTrace,
    RetrievalDocument,
    RetrievalMatch,
    RetrievalProfile,
    retrieval_profile_fingerprint,
)
from failure_memory.domain.store import StoreImportPlan, StoreImportResult
from failure_memory.ports.event_store import EventStorePort
from failure_memory.ports.retrieval import RetrievalIndexPort


@dataclass(frozen=True)
class EvaluatedCapture:
    capture_attempt_id: str
    assessment: CaptureAssessment


class FailureMemoryService:
    def __init__(
        self,
        store: EventStorePort,
        context: HarnessContext,
        clock: Callable[[], datetime] | None = None,
        closer: Callable[[], None] | None = None,
        retrieval_index: RetrievalIndexPort | None = None,
        runtime_manager: AdapterRuntimeManager | None = None,
        store_importer: GlobalStoreImporter | None = None,
    ) -> None:
        self.store = store
        self.context = context
        self.clock = clock or (lambda: datetime.now(UTC))
        self._closer = closer
        self.retrieval_index = retrieval_index
        self.runtime_manager = runtime_manager
        self.store_importer = store_importer
        self._closed = False

    def evaluate_failure_candidate(self, candidate: FailureCandidate) -> EvaluatedCapture:
        summary = redact_text(candidate.summary)
        failure_portion = (
            None
            if candidate.failure_portion_summary is None
            else redact_text(candidate.failure_portion_summary)
        )
        safe_candidate = replace(
            candidate,
            summary=summary.text,
            failure_portion_summary=None if failure_portion is None else failure_portion.text,
        )
        assessment = evaluate_candidate(safe_candidate)
        capture_id = self.store.append_capture(
            safe_candidate,
            assessment,
            self.context,
            created_at=self.clock(),
            redaction_state=_redaction_state(summary, failure_portion),
        )
        return EvaluatedCapture(capture_id, assessment)

    def record_failure_incident(
        self,
        capture_attempt_id: str,
        incident: IncidentDraft,
        lesson: LessonDraft,
    ) -> RecordResult:
        safe_incident, incident_results = _redact_incident(incident)
        safe_lesson, lesson_results = _redact_lesson(lesson)
        result = self.store.record_incident_and_lesson(
            capture_attempt_id,
            safe_incident,
            safe_lesson,
            self.context,
            created_at=self.clock(),
            redaction_state=_redaction_state(*incident_results, *lesson_results),
        )
        with suppress(Exception):
            self.build_index()
        return result

    def find_related_failures(
        self,
        expected_invariant: str,
        controllable_cause: str,
        prevention_action: str,
    ) -> LessonVersionRecord | None:
        return self.store.find_lesson_by_signature(
            lesson_signature(
                redact_text(expected_invariant).text,
                redact_text(controllable_cause).text,
                redact_text(prevention_action).text,
            ),
        )

    def recall_failure_lessons(self, query: RecallQuery) -> RecallResult:
        safe_query = _redact_recall_query(query)
        started = time.monotonic()
        attempt_id = _recall_id()
        documents = self._retrieval_documents()
        document_by_id = {document.lesson_version.id: document for document in documents}
        exact = self._exact_recall(safe_query, document_by_id)
        if exact is not None:
            result = RecallResult(
                attempt_id=attempt_id,
                requested_mode=safe_query.mode,
                executed_mode=RecallMode.EXACT,
                status=RecallStatus.OK,
                candidates=(exact,),
                retrieval_profile="exact-signature",
            )
            self._record_recall(result, safe_query, _exact_profile(), started)
            return result
        if safe_query.mode is RecallMode.EXACT:
            status = (
                RecallStatus.NO_MATCH
                if safe_query.has_exact_signature
                else RecallStatus.INSUFFICIENT_EVIDENCE
            )
            result = RecallResult(
                attempt_id=attempt_id,
                requested_mode=safe_query.mode,
                executed_mode=RecallMode.EXACT,
                status=status,
                candidates=(),
                retrieval_profile="exact-signature",
                detail=(
                    None
                    if safe_query.has_exact_signature
                    else "exact recall requires invariant, cause, and prevention action"
                ),
            )
            self._record_recall(result, safe_query, _exact_profile(), started)
            return result
        if not safe_query.has_similarity_evidence:
            profile = (
                _exact_profile() if self.retrieval_index is None else self.retrieval_index.profile
            )
            result = RecallResult(
                attempt_id=attempt_id,
                requested_mode=safe_query.mode,
                executed_mode=safe_query.mode,
                status=RecallStatus.INSUFFICIENT_EVIDENCE,
                candidates=(),
                retrieval_profile=profile.name,
                detail=(
                    "similarity recall requires task context plus an explicit "
                    "invariant, cause, prevention action, or component"
                ),
            )
            self._record_recall(result, safe_query, profile, started)
            return result
        retrieval = self.retrieval_index
        if retrieval is None:
            result = RecallResult(
                attempt_id=attempt_id,
                requested_mode=safe_query.mode,
                executed_mode=safe_query.mode,
                status=RecallStatus.SETUP_REQUIRED,
                candidates=(),
                retrieval_profile="exact-signature",
                detail="retrieval index is unavailable",
            )
            self._record_recall(result, safe_query, _exact_profile(), started)
            return result
        retrieval.sync(documents)
        requested = safe_query.mode
        resolved = RecallMode.HYBRID if requested is RecallMode.AUTO else requested
        search_limit = min(20, max(10, safe_query.top_k * 4))
        lexical: tuple[RetrievalMatch, ...] = ()
        semantic: tuple[RetrievalMatch, ...] = ()
        degraded = False
        detail: str | None = None
        if resolved in {RecallMode.LEXICAL, RecallMode.HYBRID}:
            lexical = tuple(
                retrieval.search_lexical(
                    safe_query,
                    limit=search_limit,
                )
            )
        if resolved in {RecallMode.SEMANTIC, RecallMode.HYBRID}:
            try:
                semantic = tuple(
                    retrieval.search_semantic(
                        safe_query,
                        limit=search_limit,
                    )
                )
            except SemanticSetupRequiredError as exc:
                detail = str(exc)
                if resolved is RecallMode.SEMANTIC:
                    result = RecallResult(
                        attempt_id=attempt_id,
                        requested_mode=requested,
                        executed_mode=RecallMode.SEMANTIC,
                        status=RecallStatus.SETUP_REQUIRED,
                        candidates=(),
                        retrieval_profile=retrieval.profile.name,
                        detail=detail,
                    )
                    self._record_recall(result, safe_query, retrieval.profile, started)
                    return result
                degraded = True
                resolved = RecallMode.LEXICAL
        ranked_candidates = _fuse_matches(
            lexical,
            semantic,
            document_by_id,
            limit=search_limit,
        )
        candidates = ranked_candidates[: safe_query.top_k]
        status = (
            RecallStatus.DEGRADED
            if degraded
            else RecallStatus.OK
            if candidates
            else RecallStatus.NO_MATCH
        )
        result = RecallResult(
            attempt_id=attempt_id,
            requested_mode=requested,
            executed_mode=resolved,
            status=status,
            candidates=candidates,
            retrieval_profile=retrieval.profile.name,
            detail=detail,
        )
        self._record_recall(
            result,
            safe_query,
            retrieval.profile,
            started,
            considered_candidates=ranked_candidates,
        )
        return result

    def record_recall_outcome(self, outcome: RecallOutcome) -> str:
        return self.store.append_recall_outcome(
            outcome,
            self.context,
            created_at=self.clock(),
        )

    def recall_metrics(self) -> Mapping[str, int]:
        return self.store.recall_counts()

    def learning_metrics(self) -> Mapping[str, object]:
        return self.store.learning_metrics()

    def transition_lesson(
        self,
        lesson_id: str,
        target_state: LessonState,
        rationale_code: str,
    ) -> Mapping[str, object]:
        result = self.store.transition_lesson(
            lesson_id,
            target_state,
            rationale_code,
            self.context,
            created_at=self.clock(),
        )
        with suppress(Exception):
            self.build_index()
        return {
            "event_id": result.event_id,
            "lesson_id": result.lesson_id,
            "prior_version_id": result.prior_version_id,
            "new_version_id": result.new_version.id,
            "version_number": result.new_version.version_number,
            "from_state": result.from_state.value,
            "to_state": result.to_state.value,
        }

    def run_shadow_ranking_experiment(self) -> Mapping[str, object]:
        result = self.store.run_shadow_ranking_experiment(created_at=self.clock())
        return {
            "experiment_id": result.experiment_id,
            "state": "shadow",
            "attempt_count": result.attempt_count,
            "labeled_selection_count": result.labeled_selection_count,
            "changed_top_rank_count": result.changed_top_rank_count,
            "baseline_policy": result.baseline_policy,
            "candidate_policy": result.candidate_policy,
            "metrics": dict(result.metrics),
        }

    def propose_lesson_clusters(
        self,
        *,
        distance_threshold: float = 0.2,
    ) -> Mapping[str, object]:
        retrieval = self.retrieval_index
        if retrieval is None or not retrieval.status().semantic_available:
            raise SemanticSetupRequiredError(
                "proposal clustering requires the explicit semantic adapter"
            )
        documents = self._retrieval_documents()
        pairs = retrieval.similar_pairs(
            documents,
            distance_threshold=distance_threshold,
        )
        parents = {document.lesson_version.id: document.lesson_version.id for document in documents}

        def find(value: str) -> str:
            while parents[value] != value:
                parents[value] = parents[parents[value]]
                value = parents[value]
            return value

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[max(left_root, right_root)] = min(left_root, right_root)

        for pair in pairs:
            union(pair.left_lesson_version_id, pair.right_lesson_version_id)
        grouped: dict[str, list[str]] = {}
        for lesson_version_id in sorted(parents):
            grouped.setdefault(find(lesson_version_id), []).append(lesson_version_id)
        clusters = tuple(
            LessonCluster(
                key=hashlib.sha256("\x1f".join(members).encode()).hexdigest()[:20],
                lesson_version_ids=tuple(members),
            )
            for members in grouped.values()
            if len(members) >= 2
        )
        result = self.store.append_cluster_run(
            retrieval.profile,
            distance_threshold,
            len(documents),
            clusters,
            created_at=self.clock(),
        )
        return {
            "run_id": result.run_id,
            "state": "proposed",
            "retrieval_profile": result.retrieval_profile,
            "distance_threshold": result.distance_threshold,
            "lesson_count": result.lesson_count,
            "cluster_count": len(result.clusters),
            "clusters": [
                {
                    "cluster_key": cluster.key,
                    "lesson_version_ids": list(cluster.lesson_version_ids),
                }
                for cluster in result.clusters
            ],
            "automatic_merge": False,
        }

    def store_status(self) -> Mapping[str, object]:
        if self.store_importer is None:
            return {
                "scope": "global_personal",
                "state": "unavailable",
                "target_store_id": "unavailable",
                "import_count": 0,
                "imports": [],
            }
        return {"state": "ready", **self.store_importer.status()}

    def discover_source_stores(self) -> tuple[Mapping[str, object], ...]:
        importer = self._require_store_importer()
        paths = discover_legacy_databases(
            importer.target_database,
            env_plugin_data=os.environ.get("PLUGIN_DATA"),
            env_claude_plugin_data=os.environ.get("CLAUDE_PLUGIN_DATA"),
        )
        return tuple(self._store_plan_payload(importer.plan(path)) for path in paths)

    def plan_store_import(self, source_database: Path) -> Mapping[str, object]:
        return self._store_plan_payload(self._require_store_importer().plan(source_database))

    def import_source_store(self, source_database: Path) -> Mapping[str, object]:
        importer = self._require_store_importer()
        result = importer.apply(source_database)
        with suppress(Exception):
            self.build_index()
        return self._store_result_payload(result)

    def verify_source_store(self, source_database: Path) -> Mapping[str, object]:
        plan = self._require_store_importer().plan(source_database)
        return {
            **self._store_plan_payload(plan),
            "verified": plan.already_imported and not plan.conflicts,
        }

    def build_index(self) -> Mapping[str, object]:
        if self.retrieval_index is None:
            return {
                **self.retrieval_status(),
                "changed_documents": 0,
            }
        changed = self.retrieval_index.sync(self._retrieval_documents())
        status = self.retrieval_index.status()
        return {
            "state": status.state,
            "profile": status.profile,
            "lexical_available": status.lexical_available,
            "semantic_available": status.semantic_available,
            "indexed_documents": status.indexed_documents,
            "changed_documents": changed,
            "detail": status.detail,
        }

    def retrieval_status(self) -> Mapping[str, object]:
        if self.retrieval_index is None:
            return {
                "state": "unavailable",
                "profile": "exact-signature",
                "lexical_available": False,
                "semantic_available": False,
                "indexed_documents": 0,
                "detail": "retrieval index is unavailable",
            }
        return _retrieval_status_payload(self.retrieval_index)

    def metrics(self) -> Mapping[str, int]:
        return self.store.counts()

    def setup_status(self) -> Mapping[str, object]:
        retrieval_status = None if self.retrieval_index is None else self.retrieval_index.status()
        available = [
            "failure_qualification",
            "incident_recording",
            "exact_signature_lookup",
            "global_cross_harness_recall",
            "copy_only_store_import",
            "recall_telemetry",
            "learning_metrics",
        ]
        unavailable = ["prompt_hook", "production_feedback_ranking"]
        if retrieval_status is not None and retrieval_status.lexical_available:
            available.append("fts5_recall")
        else:
            unavailable.append("fts5_recall")
        if retrieval_status is not None and retrieval_status.semantic_available:
            available.extend(["semantic_recall", "hybrid_recall"])
        else:
            unavailable.extend(["semantic_recall", "hybrid_recall"])
        return {
            "scope": "global_personal",
            "state": (
                "hybrid_ready"
                if retrieval_status is not None and retrieval_status.semantic_available
                else "lexical_ready"
                if retrieval_status is not None
                else "bootstrap_ready"
            ),
            "profile": (
                "bootstrap-sqlite" if retrieval_status is None else retrieval_status.profile
            ),
            "available_capabilities": available,
            "unavailable_capabilities": unavailable,
        }

    def doctor(self) -> Mapping[str, object]:
        status = dict(self.setup_status())
        status.update(
            {
                "integrity_check": self.store.integrity_check(),
                "counts": dict(self.store.counts()),
                "recall_counts": dict(self.store.recall_counts()),
                "learning_metrics": dict(self.store.learning_metrics()),
                "store": dict(self.store_status()),
                "retrieval": (
                    {"state": "unavailable"}
                    if self.retrieval_index is None
                    else _retrieval_status_payload(self.retrieval_index)
                ),
            }
        )
        return status

    def close(self) -> None:
        """Release resources owned by this service, at most once."""
        if self._closed:
            return
        self._closed = True
        if self._closer is not None:
            self._closer()

    def _retrieval_documents(self) -> tuple[RetrievalDocument, ...]:
        return tuple(self.store.list_retrieval_documents())

    def _require_store_importer(self) -> GlobalStoreImporter:
        if self.store_importer is None:
            raise ValueError("store import is unavailable for this adapter")
        return self.store_importer

    @staticmethod
    def _store_plan_payload(plan: StoreImportPlan) -> Mapping[str, object]:
        return {
            "source_store_id": plan.source.source_store_id,
            "source_schema_version": plan.source.schema_version,
            "content_fingerprint": plan.source.content_fingerprint,
            "source_counts": dict(plan.source.counts),
            "target_store_id": plan.target_store_id,
            "already_imported": plan.already_imported,
            "can_apply": plan.can_apply,
            "importable_counts": dict(plan.importable_counts),
            "skipped_counts": dict(plan.skipped_counts),
            "conflicts": list(plan.conflicts),
        }

    @staticmethod
    def _store_result_payload(result: StoreImportResult) -> Mapping[str, object]:
        return {
            "import_id": result.import_id,
            "source_store_id": result.source_store_id,
            "content_fingerprint": result.content_fingerprint,
            "imported_counts": dict(result.imported_counts),
            "skipped_counts": dict(result.skipped_counts),
            "already_imported": result.already_imported,
        }

    def _exact_recall(
        self,
        query: RecallQuery,
        documents: Mapping[str, RetrievalDocument],
    ) -> RecallCandidate | None:
        if not query.has_exact_signature:
            return None
        assert query.expected_invariant is not None
        assert query.controllable_cause is not None
        assert query.prevention_action is not None
        lesson = self.store.find_lesson_by_signature(
            lesson_signature(
                query.expected_invariant,
                query.controllable_cause,
                query.prevention_action,
            ),
        )
        if lesson is None:
            return None
        document = documents.get(lesson.id)
        return RecallCandidate(
            lesson=lesson,
            expected_invariant=(
                query.expected_invariant if document is None else document.expected_invariant
            ),
            controllable_cause=(
                query.controllable_cause if document is None else document.controllable_cause
            ),
            outcome_summary="" if document is None else document.outcome_summary,
            channels=("exact",),
            score=1.0,
            exact=True,
        )

    def _record_recall(
        self,
        result: RecallResult,
        query: RecallQuery,
        profile: RetrievalProfile,
        started: float,
        considered_candidates: tuple[RecallCandidate, ...] | None = None,
    ) -> None:
        created_at = self.clock().astimezone(UTC).isoformat()
        self.store.append_recall_trace(
            RecallTrace(
                attempt_id=result.attempt_id,
                requested_mode=result.requested_mode,
                executed_mode=result.executed_mode,
                status=result.status,
                retrieval_profile=profile,
                query_fingerprint=self.context.fingerprint(query.canonical_text()),
                query_fields=query.field_presence(),
                top_k=query.top_k,
                latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                candidates=(
                    result.candidates if considered_candidates is None else considered_candidates
                ),
                selected_lesson_version_ids=tuple(
                    candidate.lesson.id for candidate in result.candidates
                ),
                created_at=created_at,
            ),
            self.context,
        )


def create_local_service(
    *,
    data_root: Path | None = None,
    cwd: Path | None = None,
    harness: str | None = None,
    session_id: str | None = None,
) -> FailureMemoryService:
    """Create a local SQLite service shared by command-line and MCP adapters."""
    root = resolve_data_root() if data_root is None else data_root
    context = HarnessContext.create(
        root,
        Path.cwd() if cwd is None else cwd,
        harness or os.environ.get("FAILURE_MEMORY_HARNESS", "local"),
        session_id or os.environ.get("FAILURE_MEMORY_SESSION_ID"),
    )
    database_parent = ensure_private_tree(
        context.data_root,
        "adapters",
        "event-store",
        "sqlite",
        "primary",
    )
    database = database_parent / "failure-memory.sqlite3"
    connection: sqlite3.Connection | None = None
    retrieval: SQLiteRetrievalIndex | None = None
    try:
        connection = connect_sqlite(database)
        apply_migrations(connection)
        secure_sqlite_files(database)
        store = SQLiteEventStore(connection)
        store_importer = GlobalStoreImporter(connection, database, context.data_root)
        runtime_manager = AdapterRuntimeManager(context.data_root)
        embedding_provider = None
        profile_component = "fts5-only"
        if runtime_manager.activate():
            from failure_memory.adapters.embedding.fastembed import FastEmbedProvider

            embedding_provider = FastEmbedProvider(runtime_manager.model_root)
            profile_component = embedding_provider.spec.profile_name
        retrieval_parent = ensure_private_tree(
            context.data_root,
            "adapters",
            "retrieval",
            "sqlite-vec",
            "global-v2",
            profile_component,
        )
        retrieval = SQLiteRetrievalIndex(
            retrieval_parent / "index.sqlite3",
            embedding_provider=embedding_provider,
        )

        def close_all() -> None:
            try:
                retrieval.close()
            finally:
                connection.close()

        return FailureMemoryService(
            store,
            context,
            closer=close_all,
            retrieval_index=retrieval,
            runtime_manager=runtime_manager,
            store_importer=store_importer,
        )
    except BaseException as error:
        if connection is not None:
            with suppress(BaseException):
                connection.close()
        if retrieval is not None:
            with suppress(BaseException):
                retrieval.close()
        if isinstance(error, sqlite3.Error) and is_sqlite_busy_error(error):
            raise StorageBusyError(
                "Failure-memory storage remained busy during service startup."
            ) from error
        raise


def _redact_incident(incident: IncidentDraft) -> tuple[IncidentDraft, tuple[RedactionResult, ...]]:
    return _redact_draft(incident)


def _redact_lesson(lesson: LessonDraft) -> tuple[LessonDraft, tuple[RedactionResult, ...]]:
    return _redact_draft(lesson)


def _redact_draft[DraftT: (IncidentDraft, LessonDraft)](
    draft: DraftT,
) -> tuple[DraftT, tuple[RedactionResult, ...]]:
    replacements: dict[str, object] = {}
    results: list[RedactionResult] = []
    for field in fields(draft):
        value = getattr(draft, field.name)
        if value is None:
            replacements[field.name] = None
            continue
        if not isinstance(value, str):
            raise TypeError(f"{type(draft).__name__}.{field.name} must be text or null")
        result = redact_text(value)
        replacements[field.name] = result.text
        results.append(result)
    return replace(draft, **cast(Any, replacements)), tuple(results)


def _redaction_state(*results: RedactionResult | None) -> str:
    contains_redaction = any(
        result is not None and result.state == "redacted" for result in results
    )
    return "redacted" if contains_redaction else "clean"


def _redact_recall_query(query: RecallQuery) -> RecallQuery:
    replacements: dict[str, object] = {}
    for name in (
        "text",
        "expected_invariant",
        "controllable_cause",
        "prevention_action",
        "component",
    ):
        value = getattr(query, name)
        replacements[name] = None if value is None else redact_text(value).text
    return replace(query, **cast(Any, replacements))


def _exact_profile() -> RetrievalProfile:
    return RetrievalProfile(
        name="exact-signature",
        backend="event-store-sqlite",
        config_fingerprint=retrieval_profile_fingerprint(
            backend="event-store-sqlite",
            embedding=None,
            lexical="none",
            fusion="none",
        ),
        capabilities=("exact",),
    )


def _fuse_matches(
    lexical: tuple[RetrievalMatch, ...],
    semantic: tuple[RetrievalMatch, ...],
    documents: Mapping[str, RetrievalDocument],
    *,
    limit: int,
) -> tuple[RecallCandidate, ...]:
    ranks: dict[str, dict[str, RetrievalMatch]] = {}
    for match in (*lexical, *semantic):
        ranks.setdefault(match.lesson_version_id, {})[match.channel] = match
    ordered = sorted(
        ranks.items(),
        key=lambda item: (
            -sum(1.0 / (60 + match.rank) for match in item[1].values()),
            item[0],
        ),
    )
    candidates: list[RecallCandidate] = []
    for lesson_version_id, channels in ordered:
        document = documents.get(lesson_version_id)
        if document is None:
            continue
        lexical_match = channels.get("lexical")
        semantic_match = channels.get("semantic")
        candidates.append(
            RecallCandidate(
                lesson=document.lesson_version,
                expected_invariant=document.expected_invariant,
                controllable_cause=document.controllable_cause,
                outcome_summary=document.outcome_summary,
                channels=tuple(sorted(channels)),
                score=sum(1.0 / (60 + match.rank) for match in channels.values()),
                exact=False,
                lexical_rank=None if lexical_match is None else lexical_match.rank,
                semantic_rank=None if semantic_match is None else semantic_match.rank,
                vector_distance=(None if semantic_match is None else semantic_match.distance),
            )
        )
        if len(candidates) == limit:
            break
    return tuple(candidates)


def _retrieval_status_payload(index: RetrievalIndexPort) -> dict[str, object]:
    status = index.status()
    return {
        "state": status.state,
        "profile": status.profile,
        "lexical_available": status.lexical_available,
        "semantic_available": status.semantic_available,
        "indexed_documents": status.indexed_documents,
        "detail": status.detail,
    }


def _recall_id() -> str:
    from failure_memory.domain.ids import new_id

    return new_id("ra")
