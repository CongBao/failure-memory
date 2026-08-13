package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/gofrs/flock"

	embeddingadapter "github.com/CongBao/failure-memory/internal/adapters/embedding"
	"github.com/CongBao/failure-memory/internal/config"
	"github.com/CongBao/failure-memory/internal/identity"
	"github.com/CongBao/failure-memory/internal/migrate"
	"github.com/CongBao/failure-memory/internal/model"
	"github.com/CongBao/failure-memory/internal/policy"
	"github.com/CongBao/failure-memory/internal/ports"
	"github.com/CongBao/failure-memory/internal/redact"
	"github.com/CongBao/failure-memory/internal/retrieval"
	storesqlite "github.com/CongBao/failure-memory/internal/store/sqlite"
)

type Service struct {
	paths      config.Paths
	store      *storesqlite.Store
	index      ports.RetrievalIndex
	cancel     context.CancelFunc
	syncMu     sync.Mutex
	syncState  string
	syncError  string
	background sync.WaitGroup
}

func Open(transport string) (*Service, error) {
	paths, err := config.ResolvePaths()
	if err != nil {
		return nil, err
	}
	harness, workspace, session, err := config.RuntimeContext(paths, transport)
	if err != nil {
		return nil, err
	}
	runtimeContext := model.Context{
		Harness:              harness,
		WorkspaceFingerprint: workspace,
		SessionFingerprint:   session,
		Transport:            transport,
	}
	store, err := storesqlite.Open(paths.EventStore, runtimeContext)
	if err != nil {
		return nil, err
	}
	embedder, _ := embeddingadapter.Resolve(paths.EmbeddingModel)
	index, err := retrieval.Open(paths.RetrievalIndex, embedder)
	if err != nil {
		_ = store.Close()
		return nil, err
	}
	var cancel context.CancelFunc
	service := &Service{
		paths: paths, store: store, index: index, syncState: "unchecked",
	}
	if transport == "mcp" {
		var warmContext context.Context
		warmContext, cancel = context.WithCancel(context.Background())
		service.background.Add(1)
		go func() {
			defer service.background.Done()
			_ = index.Warm(warmContext)
			_, _ = service.ensureIndex(warmContext, false)
		}()
	}
	service.cancel = cancel
	return service, nil
}

func (s *Service) Close() error {
	var errorsSeen []error
	if s.cancel != nil {
		s.cancel()
	}
	s.background.Wait()
	if s.index != nil {
		if err := s.index.Close(); err != nil {
			errorsSeen = append(errorsSeen, err)
		}
	}
	if s.store != nil {
		if err := s.store.Close(); err != nil {
			errorsSeen = append(errorsSeen, err)
		}
	}
	return errors.Join(errorsSeen...)
}

func (s *Service) Remember(ctx context.Context, input model.RememberInput) (model.RememberResult, error) {
	started := time.Now()
	input = redact.Remember(input)
	if strings.TrimSpace(input.Summary) == "" {
		return model.RememberResult{}, errors.New("summary is required")
	}
	assessment := policy.Qualify(input)
	operationID := identity.New("record")
	signature := storesqlite.CanonicalSignature(input)
	var document *model.LessonDocument
	if assessment.Decision == model.Accept {
		document = buildDocument(input, signature)
	}
	relatedVersions := s.relatedLessons(ctx, input, document)
	recorded, err := s.store.Record(
		ctx, operationID, input, assessment, signature, document, relatedVersions,
	)
	if err != nil {
		return model.RememberResult{}, fmt.Errorf("record failure memory: %w", err)
	}
	semanticStatus := s.semanticStatus()
	if recorded.LessonVersionID != "" {
		lesson, found, lookupErr := s.store.LessonByVersion(ctx, recorded.LessonVersionID)
		if lookupErr != nil {
			semanticStatus = "index_pending"
		} else if found {
			if indexErr := s.index.Upsert(ctx, lesson, recorded.LessonRevision); indexErr != nil {
				semanticStatus = "index_pending"
				s.setSyncState("repair_needed", indexErr)
			} else if synced, syncErr := s.quickIndexSync(ctx); syncErr != nil || !synced {
				semanticStatus = "index_pending"
				s.setSyncState("repair_needed", syncErr)
			}
		}
	}
	result := model.RememberResult{
		OperationID:     operationID,
		Status:          assessment.Status,
		Decision:        assessment.Decision,
		ReasonCodes:     assessment.ReasonCodes,
		Deduplication:   recorded.Deduplication,
		SemanticStatus:  semanticStatus,
		TotalLatencyMS:  time.Since(started).Milliseconds(),
		CaptureEventID:  recorded.CaptureEventID,
		IncidentID:      recorded.IncidentID,
		LessonID:        recorded.LessonID,
		LessonVersionID: recorded.LessonVersionID,
		RepairID:        recorded.RepairID,
	}
	result.Correction = rememberCorrection(
		input.CorrectionOf,
		recorded.CaptureEventID,
		assessment.ReasonCodes,
	)
	result.Retryable = result.Correction != nil
	if recorded.Deduplication == "related_pending_generalization" {
		result.GeneralizationHint = "Related lessons were retained separately and queued for generalization review."
	}
	return result, nil
}

func (s *Service) Recall(ctx context.Context, input model.RecallInput) (model.RecallResult, error) {
	started := time.Now()
	input = redact.Recall(input)
	requestData, _ := json.Marshal(input)
	if strings.TrimSpace(input.Text) == "" {
		return model.RecallResult{}, errors.New("text is required")
	}
	if input.TopK < 0 || input.TopK > 3 {
		return model.RecallResult{}, errors.New("top_k must be between 1 and 3")
	}
	if input.MinRelevance < 0 || input.MinRelevance > 1 {
		return model.RecallResult{}, errors.New("min_relevance must be between 0 and 1")
	}
	indexStarted := time.Now()
	if _, err := s.ensureIndex(ctx, false); err != nil {
		return model.RecallResult{}, fmt.Errorf("repair retrieval index: %w", err)
	}
	indexLatency := time.Since(indexStarted).Milliseconds()
	representatives, err := s.store.RetrievalRepresentatives(ctx)
	if err != nil {
		return model.RecallResult{}, err
	}
	input.Representatives = representatives
	operationID := identity.New("recallop")
	searchStarted := time.Now()
	searched, err := s.index.Search(ctx, input)
	if err != nil {
		return model.RecallResult{}, fmt.Errorf("search failure memory: %w", err)
	}
	searchLatency := time.Since(searchStarted).Milliseconds()
	hydrationStarted := time.Now()
	lessons := make([]model.Lesson, 0, len(searched.Candidates))
	traceCandidates := make([]storesqlite.RecallCandidate, 0, len(searched.Candidates))
	for rank, candidate := range searched.Candidates {
		document, found, err := s.store.LessonByVersion(ctx, candidate.LessonVersionID)
		if err != nil {
			return model.RecallResult{}, err
		}
		if !found {
			continue
		}
		lessons = append(lessons, model.Lesson{
			LessonID:         document.LessonID,
			LessonVersionID:  document.LessonVersionID,
			Title:            document.Title,
			Rule:             document.Rule,
			Prevention:       document.Prevention,
			Verification:     document.Verification,
			Applicability:    document.Applicability,
			Counterexamples:  document.Counterexamples,
			CauseLayer:       document.CauseLayer,
			FailureMode:      document.FailureMode,
			Component:        document.Component,
			RelevanceScore:   candidate.RelevanceScore,
			RetrievalReasons: candidate.Reasons,
		})
		traceCandidates = append(traceCandidates, storesqlite.RecallCandidate{
			LessonVersionID: candidate.LessonVersionID,
			Rank:            rank + 1,
			Score:           candidate.Score,
			RelevanceScore:  candidate.RelevanceScore,
			Reasons:         candidate.Reasons,
			Selected:        true,
		})
	}
	hydrationLatency := time.Since(hydrationStarted).Milliseconds()
	responseData, _ := json.Marshal(lessons)
	attemptID, err := s.store.AppendRecall(
		ctx,
		operationID,
		input,
		searched.Mode,
		searched.SemanticStatus,
		traceCandidates,
		storesqlite.RecallTelemetry{
			RetrievedCount:         searched.RetrievedCount,
			FilteredBelowThreshold: searched.FilteredBelowThreshold,
			CollapsedByCluster:     searched.CollapsedByCluster,
			TrimmedByAdaptiveLimit: searched.TrimmedByAdaptiveLimit,
			AppliedTopK:            searched.AppliedTopK,
			AppliedMinRelevance:    searched.AppliedMinRelevance,
			AbstentionReason:       searched.AbstentionReason,
			IndexSyncLatencyMS:     indexLatency,
			SearchLatencyMS:        searchLatency,
			HydrationLatencyMS:     hydrationLatency,
			RequestBytes:           len(requestData),
			ResponseBytes:          len(responseData),
		},
	)
	if err != nil {
		return model.RecallResult{}, fmt.Errorf("append recall trace: %w", err)
	}
	return model.RecallResult{
		AttemptID:              attemptID,
		Mode:                   searched.Mode,
		SemanticStatus:         searched.SemanticStatus,
		Lessons:                lessons,
		TotalLatencyMS:         time.Since(started).Milliseconds(),
		CandidateCount:         len(traceCandidates),
		RetrievedCount:         searched.RetrievedCount,
		FilteredBelowThreshold: searched.FilteredBelowThreshold,
		CollapsedByCluster:     searched.CollapsedByCluster,
		TrimmedByAdaptiveLimit: searched.TrimmedByAdaptiveLimit,
		AppliedTopK:            searched.AppliedTopK,
		AppliedMinRelevance:    searched.AppliedMinRelevance,
		AbstentionReason:       searched.AbstentionReason,
		StoreScope:             "global_personal",
	}, nil
}

func (s *Service) RebuildIndex(ctx context.Context) (map[string]any, error) {
	started := time.Now()
	lessons, revision, manifest, err := s.store.LessonSnapshot(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.index.Rebuild(ctx, lessons, revision); err != nil {
		return nil, err
	}
	indexStatus, err := s.index.Status(ctx)
	if err != nil {
		return nil, err
	}
	expected := int64(len(lessons))
	if indexStatus.Documents != expected ||
		indexStatus.Lexical != expected ||
		indexStatus.Vectors != expected ||
		indexStatus.SourceRevision != revision {
		return nil, fmt.Errorf(
			"retrieval rebuild incomplete: expected %d lessons, got documents=%d lexical=%d vectors=%d",
			expected,
			indexStatus.Documents,
			indexStatus.Lexical,
			indexStatus.Vectors,
		)
	}
	indexManifest, err := s.index.Manifest(ctx)
	if err != nil {
		return nil, err
	}
	if indexManifest != manifest {
		return nil, errors.New("retrieval rebuild manifest does not match the event store")
	}
	indexStatus.ManifestSHA256 = indexManifest
	s.setSyncState("repaired", nil)
	return map[string]any{
		"indexed_lessons": len(lessons),
		"index_status":    indexStatus,
		"profile":         s.index.Profile(),
		"semantic":        s.index.Semantic(),
		"latency_ms":      time.Since(started).Milliseconds(),
	}, nil
}

func (s *Service) Doctor(ctx context.Context) (map[string]any, error) {
	result, err := s.store.Doctor(ctx)
	if err != nil {
		return nil, err
	}
	result["event_store_path"] = s.paths.EventStore
	result["retrieval_index_path"] = s.paths.RetrievalIndex
	result["retrieval_profile"] = s.index.Profile()
	syncState, syncErr := s.ensureIndex(ctx, true)
	result["retrieval_sync_state"] = syncState
	if syncErr != nil {
		result["retrieval_sync_error"] = syncErr.Error()
	}
	indexStatus, err := s.index.Status(ctx)
	if err != nil {
		return nil, err
	}
	result["retrieval_index"] = indexStatus
	expected, _ := result["manifest_lesson_count"].(int)
	revision, _ := result["retrieval_revision"].(int64)
	indexComplete := indexStatus.Documents == int64(expected) &&
		indexStatus.Lexical == int64(expected) &&
		indexStatus.Vectors == int64(expected) &&
		indexStatus.SourceRevision == revision
	indexManifest, manifestErr := s.index.Manifest(ctx)
	if manifestErr == nil {
		indexStatus.ManifestSHA256 = indexManifest
		result["retrieval_index"] = indexStatus
		if storeManifest, ok := result["lesson_manifest_sha256"].(string); ok {
			indexComplete = indexComplete && indexManifest == storeManifest
		}
	}
	result["retrieval_index_complete"] = indexComplete
	semanticStatus := s.semanticStatus()
	if !indexComplete {
		semanticStatus = "index_incomplete"
	}
	if err := s.index.Warm(ctx); err != nil {
		semanticStatus = "unavailable"
		result["semantic_error"] = err.Error()
	}
	result["semantic_status"] = semanticStatus
	result["embedding_adapter"] = embeddingadapter.StatusAt(s.paths.EmbeddingModel)
	result["user_managed_language_runtime_required"] = false
	return result, nil
}

func (s *Service) Metrics(ctx context.Context) (map[string]any, error) {
	counts, err := s.store.Counts(ctx)
	if err != nil {
		return nil, err
	}
	eventCounts, err := s.store.EventCounts(ctx)
	if err != nil {
		return nil, err
	}
	outcomeCounts, err := s.store.OutcomeCounts(ctx)
	if err != nil {
		return nil, err
	}
	recallPerformance, err := s.store.RecallPerformance(ctx)
	if err != nil {
		return nil, err
	}
	lifecycle, err := s.store.LessonLifecycleCounts(ctx)
	if err != nil {
		return nil, err
	}
	coverage, err := s.store.OutcomeCoverage(ctx)
	if err != nil {
		return nil, err
	}
	harnessUsage, err := s.store.HarnessUsage(ctx)
	if err != nil {
		return nil, err
	}
	generalizationBacklog, err := s.store.GeneralizationBacklog(ctx)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"counts":                 counts,
		"event_counts":           eventCounts,
		"outcome_counts":         outcomeCounts,
		"outcome_coverage":       coverage,
		"recall_performance":     recallPerformance,
		"lesson_lifecycle":       lifecycle,
		"generalization_backlog": generalizationBacklog,
		"harness_usage":          harnessUsage,
		"store_id":               s.store.StoreID(),
	}, nil
}

func (s *Service) ReportOutcome(
	ctx context.Context,
	input model.MemoryOutcomeInput,
) (model.OutcomeResult, error) {
	input.EvidenceCode = redact.Text(input.EvidenceCode)
	result, err := s.store.AppendMemoryOutcome(ctx, input)
	if err != nil {
		return model.OutcomeResult{}, err
	}
	retrievalStatus := ""
	if result.RetrievalChanged && !result.Duplicate {
		retrievalStatus = "ready"
		if _, err := s.ensureIndex(ctx, false); err != nil {
			retrievalStatus = "repair_pending"
		}
	}
	return model.OutcomeResult{
		EventID: result.EventID, Status: "recorded", Duplicate: result.Duplicate,
		LessonID: result.LessonID, LessonVersionID: result.LessonVersionID,
		RetrievalStatus: retrievalStatus,
	}, nil
}

func (s *Service) ProposeClusters(
	ctx context.Context,
	threshold float64,
) (model.ClusterRunResult, error) {
	if _, err := s.ensureIndex(ctx, false); err != nil {
		return model.ClusterRunResult{}, fmt.Errorf("repair retrieval index: %w", err)
	}
	if threshold == 0 {
		threshold = 0.18
	}
	vectorClusters, lessonCount, err := s.index.Clusters(ctx, threshold)
	if err != nil {
		return model.ClusterRunResult{}, err
	}
	clusters := make([]model.Cluster, 0, len(vectorClusters))
	for _, cluster := range vectorClusters {
		clusters = append(clusters, model.Cluster{
			Key:              cluster.Key,
			LessonVersionIDs: cluster.LessonVersionIDs,
		})
	}
	runID, err := s.store.AppendClusterRun(
		ctx, s.index.Profile(), s.index.Semantic(), threshold, lessonCount, clusters,
	)
	if err != nil {
		return model.ClusterRunResult{}, err
	}
	return model.ClusterRunResult{
		RunID:             runID,
		Profile:           s.index.Profile(),
		Semantic:          s.index.Semantic(),
		DistanceThreshold: threshold,
		LessonCount:       lessonCount,
		Clusters:          clusters,
		ProposalCount:     len(clusters),
	}, nil
}

func (s *Service) ReviewGeneralization(
	ctx context.Context,
	input model.GeneralizationReviewInput,
) (model.OutcomeResult, error) {
	input.RationaleCode = redact.Text(input.RationaleCode)
	var document *model.LessonDocument
	if input.Decision == "accept" {
		if input.GeneralizedLesson == nil {
			return model.OutcomeResult{}, errors.New("accepted generalization requires generalized_lesson")
		}
		generalized := input.GeneralizedLesson
		if !containsValue(model.CauseLayerValues(), string(generalized.CauseLayer)) ||
			!containsValue(model.FailureModeValues(), string(generalized.FailureMode)) {
			return model.OutcomeResult{}, errors.New("generalized lesson cause taxonomy is invalid")
		}
		if strings.TrimSpace(generalized.Rule) == "" ||
			strings.TrimSpace(generalized.Prevention) == "" ||
			strings.TrimSpace(generalized.Verification) == "" ||
			strings.TrimSpace(generalized.Component) == "" {
			return model.OutcomeResult{}, errors.New("generalized lesson is missing durable fields")
		}
		synthetic := model.RememberInput{
			Expectation: &model.ExpectationEvidence{Invariant: generalized.Rule},
			Observed: &model.ObservedEvidence{
				Outcome: "Related incidents shared one controllable failure pattern.",
				Impact:  "Separate lessons duplicated prevention guidance.",
			},
			Cause: &model.CauseEvidence{
				Layer: generalized.CauseLayer, FailureMode: generalized.FailureMode,
				Component: generalized.Component, Evidence: input.RationaleCode,
				RecommendedChange: generalized.Prevention,
				Verification:      generalized.Verification,
			},
			Lesson: &model.LessonEvidence{
				Title: generalized.Title, Rule: generalized.Rule,
				Prevention: generalized.Prevention, Verification: generalized.Verification,
				Applicability:   generalized.Applicability,
				Counterexamples: generalized.Counterexamples,
			},
		}
		document = buildDocument(synthetic, storesqlite.CanonicalSignature(synthetic))
	}
	stored, err := s.store.AppendGeneralizationReview(ctx, input, document)
	if err != nil {
		return model.OutcomeResult{}, err
	}
	if input.Decision == "accept" {
		retrievalStatus := "ready"
		if _, err := s.ensureIndex(ctx, false); err != nil {
			retrievalStatus = "repair_pending"
		}
		return model.OutcomeResult{
			EventID: stored.EventID, Status: "recorded", LessonID: stored.LessonID,
			LessonVersionID: stored.LessonVersionID, RetrievalStatus: retrievalStatus,
		}, nil
	}
	return model.OutcomeResult{
		EventID: stored.EventID, Status: "recorded", LessonID: stored.LessonID,
		LessonVersionID: stored.LessonVersionID,
	}, nil
}

func containsValue(values []string, value string) bool {
	for _, candidate := range values {
		if candidate == value {
			return true
		}
	}
	return false
}

func (s *Service) MigrateV07(
	ctx context.Context,
	source string,
) (model.LegacyImportResult, error) {
	legacy, err := migrate.ReadV07(ctx, source)
	if err != nil {
		return model.LegacyImportResult{}, err
	}
	result, err := s.store.ImportLegacy(ctx, legacy)
	if err != nil {
		return model.LegacyImportResult{}, err
	}
	if result.Status == "imported" {
		if _, err := s.RebuildIndex(ctx); err != nil {
			return result, fmt.Errorf("legacy data imported but retrieval rebuild failed: %w", err)
		}
	}
	return result, nil
}

func (s *Service) StoreStatus() map[string]any {
	return map[string]any{
		"scope":                "global_personal",
		"store_id":             s.store.StoreID(),
		"event_store_path":     s.paths.EventStore,
		"retrieval_index_path": s.paths.RetrievalIndex,
	}
}

func (s *Service) CreateBackup(
	ctx context.Context,
	destination string,
) (storesqlite.BackupResult, error) {
	return s.store.CreateBackup(ctx, destination)
}

func (s *Service) semanticStatus() string {
	if s.index.Semantic() {
		return "ready"
	}
	return "vector_fallback_nonsemantic"
}

func (s *Service) quickIndexSync(ctx context.Context) (bool, error) {
	revision, err := s.store.RetrievalRevision(ctx)
	if err != nil {
		return false, err
	}
	status, err := s.index.Status(ctx)
	if err != nil {
		return false, err
	}
	return indexStatusMatches(status, revision), nil
}

func (s *Service) ensureIndex(ctx context.Context, deep bool) (string, error) {
	s.syncMu.Lock()
	defer s.syncMu.Unlock()

	revision, err := s.store.RetrievalRevision(ctx)
	if err != nil {
		s.setSyncStateLocked("repair_failed", err)
		return "repair_failed", err
	}
	status, err := s.index.Status(ctx)
	if err != nil {
		s.setSyncStateLocked("repair_failed", err)
		return "repair_failed", err
	}
	if indexStatusMatches(status, revision) {
		if !deep {
			s.setSyncStateLocked("in_sync", nil)
			return "in_sync", nil
		}
		_, _, storeManifest, snapshotErr := s.store.LessonSnapshot(ctx)
		indexManifest, indexErr := s.index.Manifest(ctx)
		if snapshotErr == nil && indexErr == nil && storeManifest == indexManifest {
			s.setSyncStateLocked("in_sync", nil)
			return "in_sync", nil
		}
	}

	reconcileLock := flock.New(s.paths.RetrievalIndex + ".reconcile.lock")
	locked, err := reconcileLock.TryLockContext(ctx, 25*time.Millisecond)
	if err != nil {
		s.setSyncStateLocked("repair_failed", err)
		return "repair_failed", err
	}
	if !locked {
		err := errors.New("retrieval reconciliation is busy")
		s.setSyncStateLocked("repair_failed", err)
		return "repair_failed", err
	}
	defer func() {
		_ = reconcileLock.Unlock()
		_ = reconcileLock.Close()
	}()

	s.setSyncStateLocked("repairing", nil)
	for attempt := 0; attempt < 3; attempt++ {
		lessons, snapshotRevision, manifest, err := s.store.LessonSnapshot(ctx)
		if err != nil {
			s.setSyncStateLocked("repair_failed", err)
			return "repair_failed", err
		}
		status, err := s.index.Status(ctx)
		if err == nil && indexStatusMatches(status, snapshotRevision) {
			indexManifest, manifestErr := s.index.Manifest(ctx)
			if manifestErr == nil && indexManifest == manifest {
				s.setSyncStateLocked("in_sync", nil)
				return "in_sync", nil
			}
		}
		if err := s.index.Rebuild(ctx, lessons, snapshotRevision); err != nil {
			s.setSyncStateLocked("repair_failed", err)
			return "repair_failed", err
		}
		liveRevision, err := s.store.RetrievalRevision(ctx)
		if err != nil {
			s.setSyncStateLocked("repair_failed", err)
			return "repair_failed", err
		}
		if liveRevision != snapshotRevision {
			continue
		}
		status, err = s.index.Status(ctx)
		if err != nil || !indexStatusMatches(status, snapshotRevision) {
			if err == nil {
				err = errors.New("retrieval index counts do not match the event store")
			}
			s.setSyncStateLocked("repair_failed", err)
			return "repair_failed", err
		}
		indexManifest, err := s.index.Manifest(ctx)
		if err != nil || indexManifest != manifest {
			if err == nil {
				err = errors.New("retrieval index manifest does not match the event store")
			}
			s.setSyncStateLocked("repair_failed", err)
			return "repair_failed", err
		}
		s.setSyncStateLocked("repaired", nil)
		return "repaired", nil
	}
	err = errors.New("event store changed repeatedly during retrieval reconciliation")
	s.setSyncStateLocked("repair_failed", err)
	return "repair_failed", err
}

func indexStatusMatches(status ports.RetrievalStatus, revision int64) bool {
	return status.Documents == status.Lexical &&
		status.Lexical == status.Vectors &&
		status.SourceRevision == revision
}

func (s *Service) setSyncState(state string, err error) {
	s.syncMu.Lock()
	defer s.syncMu.Unlock()
	s.setSyncStateLocked(state, err)
}

func (s *Service) setSyncStateLocked(state string, err error) {
	s.syncState = state
	s.syncError = ""
	if err != nil {
		s.syncError = err.Error()
	}
}

func (s *Service) relatedLessons(
	ctx context.Context,
	input model.RememberInput,
	document *model.LessonDocument,
) []string {
	if document == nil || input.Cause == nil || input.Expectation == nil || input.Lesson == nil {
		return nil
	}
	searched, err := s.index.Search(ctx, model.RecallInput{
		Text:              document.Document,
		ExpectedInvariant: input.Expectation.Invariant,
		ControllableCause: input.Cause.Evidence,
		PreventionAction:  input.Lesson.Prevention,
		Component:         input.Cause.Component,
		Mode:              "hybrid",
		TopK:              3,
	})
	if err != nil {
		return nil
	}
	var related []string
	for _, candidate := range searched.Candidates {
		existing, found, err := s.store.LessonByVersion(ctx, candidate.LessonVersionID)
		if err != nil || !found || existing.Signature == document.Signature {
			continue
		}
		sameComponent := normalized(existing.Component) == normalized(document.Component)
		sameCause := normalized(existing.CauseLayer) == normalized(document.CauseLayer) &&
			normalized(existing.FailureMode) == normalized(document.FailureMode)
		if sameComponent || sameCause {
			related = append(related, candidate.LessonVersionID)
		}
	}
	return related
}

func normalized(value string) string {
	return strings.Join(strings.Fields(strings.ToLower(value)), " ")
}

func rememberCorrection(
	correctionOf string,
	captureEventID string,
	reasonCodes []string,
) *model.RememberCorrection {
	if strings.TrimSpace(correctionOf) != "" || captureEventID == "" || len(reasonCodes) == 0 {
		return nil
	}
	allowed := map[string][]string{}
	for _, reason := range reasonCodes {
		switch reason {
		case "invalid_classification":
			allowed["classification"] = model.ClassificationValues()
		case "cause_layer_invalid":
			allowed["cause.layer"] = model.CauseLayerValues()
		case "failure_mode_invalid":
			allowed["cause.failure_mode"] = model.FailureModeValues()
		case "cause_confidence_invalid":
			allowed["cause.confidence"] = model.ConfidenceValues()
		default:
			return nil
		}
	}
	return &model.RememberCorrection{
		CorrectionOfCaptureEventID: captureEventID,
		AllowedValues:              allowed,
	}
}

func buildDocument(input model.RememberInput, signature string) *model.LessonDocument {
	cause := input.Cause
	lesson := input.Lesson
	expectation := input.Expectation
	observed := input.Observed
	if cause == nil || lesson == nil || expectation == nil || observed == nil {
		return nil
	}
	title := strings.TrimSpace(lesson.Title)
	if title == "" {
		title = firstSentence(lesson.Rule, 96)
	}
	document := strings.Join([]string{
		title,
		expectation.Invariant,
		observed.Outcome,
		observed.Impact,
		string(cause.Layer),
		string(cause.FailureMode),
		cause.Component,
		cause.Evidence,
		cause.RecommendedChange,
		lesson.Rule,
		lesson.Prevention,
		lesson.Verification,
		lesson.Applicability,
		lesson.Counterexamples,
	}, "\n")
	return &model.LessonDocument{
		Signature:       signature,
		Title:           title,
		Rule:            lesson.Rule,
		Prevention:      lesson.Prevention,
		Verification:    lesson.Verification,
		Applicability:   lesson.Applicability,
		Counterexamples: lesson.Counterexamples,
		CauseLayer:      string(cause.Layer),
		FailureMode:     string(cause.FailureMode),
		Component:       cause.Component,
		Document:        document,
		CreatedAt:       time.Now().UTC(),
	}
}

func firstSentence(value string, maximum int) string {
	value = strings.TrimSpace(value)
	for _, separator := range []string{". ", "。", "\n"} {
		if index := strings.Index(value, separator); index >= 0 {
			value = value[:index]
		}
	}
	runes := []rune(value)
	if len(runes) > maximum {
		value = string(runes[:maximum])
	}
	return value
}
