package service

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

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
	paths  config.Paths
	store  *storesqlite.Store
	index  ports.RetrievalIndex
	cancel context.CancelFunc
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
	if transport == "mcp" {
		var warmContext context.Context
		warmContext, cancel = context.WithCancel(context.Background())
		go func() {
			_ = index.Warm(warmContext)
		}()
	}
	return &Service{paths: paths, store: store, index: index, cancel: cancel}, nil
}

func (s *Service) Close() error {
	var errorsSeen []error
	if s.cancel != nil {
		s.cancel()
	}
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
			if indexErr := s.index.Upsert(ctx, lesson); indexErr != nil {
				semanticStatus = "index_pending"
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
	if recorded.Deduplication == "related_pending_generalization" {
		result.GeneralizationHint = "Related lessons were retained separately and queued for generalization review."
	}
	return result, nil
}

func (s *Service) Recall(ctx context.Context, input model.RecallInput) (model.RecallResult, error) {
	started := time.Now()
	input = redact.Recall(input)
	if strings.TrimSpace(input.Text) == "" {
		return model.RecallResult{}, errors.New("text is required")
	}
	if strings.TrimSpace(input.ExpectedInvariant) == "" &&
		strings.TrimSpace(input.ControllableCause) == "" &&
		strings.TrimSpace(input.PreventionAction) == "" &&
		strings.TrimSpace(input.Component) == "" {
		return model.RecallResult{}, errors.New("one concrete discriminator is required")
	}
	if input.TopK < 0 || input.TopK > 3 {
		return model.RecallResult{}, errors.New("top_k must be between 1 and 3")
	}
	operationID := identity.New("recallop")
	searched, err := s.index.Search(ctx, input)
	if err != nil {
		return model.RecallResult{}, fmt.Errorf("search failure memory: %w", err)
	}
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
			Score:            candidate.Score,
			RetrievalReasons: candidate.Reasons,
		})
		traceCandidates = append(traceCandidates, storesqlite.RecallCandidate{
			LessonVersionID: candidate.LessonVersionID,
			Rank:            rank + 1,
			Score:           candidate.Score,
			Reasons:         candidate.Reasons,
			Selected:        true,
		})
	}
	attemptID, err := s.store.AppendRecall(
		ctx,
		operationID,
		input,
		searched.Mode,
		searched.SemanticStatus,
		traceCandidates,
	)
	if err != nil {
		return model.RecallResult{}, fmt.Errorf("append recall trace: %w", err)
	}
	return model.RecallResult{
		AttemptID:      attemptID,
		Mode:           searched.Mode,
		SemanticStatus: searched.SemanticStatus,
		Lessons:        lessons,
		TotalLatencyMS: time.Since(started).Milliseconds(),
		CandidateCount: len(traceCandidates),
		StoreScope:     "global_personal",
	}, nil
}

func (s *Service) RebuildIndex(ctx context.Context) (map[string]any, error) {
	started := time.Now()
	lessons, err := s.store.ListLessons(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.index.Rebuild(ctx, lessons); err != nil {
		return nil, err
	}
	indexStatus, err := s.index.Status(ctx)
	if err != nil {
		return nil, err
	}
	expected := int64(len(lessons))
	if indexStatus.Documents != expected ||
		indexStatus.Lexical != expected ||
		indexStatus.Vectors != expected {
		return nil, fmt.Errorf(
			"retrieval rebuild incomplete: expected %d lessons, got documents=%d lexical=%d vectors=%d",
			expected,
			indexStatus.Documents,
			indexStatus.Lexical,
			indexStatus.Vectors,
		)
	}
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
	indexStatus, err := s.index.Status(ctx)
	if err != nil {
		return nil, err
	}
	result["retrieval_index"] = indexStatus
	counts, _ := result["counts"].(map[string]int64)
	expected := counts["lessons"]
	indexComplete := indexStatus.Documents == expected &&
		indexStatus.Lexical == expected &&
		indexStatus.Vectors == expected
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
	return map[string]any{
		"counts":         counts,
		"event_counts":   eventCounts,
		"outcome_counts": outcomeCounts,
		"store_id":       s.store.StoreID(),
	}, nil
}

func (s *Service) RecordRecallOutcome(
	ctx context.Context,
	input model.RecallOutcomeInput,
) (model.OutcomeResult, error) {
	input.DetailCode = redact.Text(input.DetailCode)
	eventID, err := s.store.AppendRecallOutcome(ctx, input)
	if err != nil {
		return model.OutcomeResult{}, err
	}
	return model.OutcomeResult{EventID: eventID, Status: "recorded"}, nil
}

func (s *Service) RecordRepairOutcome(
	ctx context.Context,
	input model.RepairOutcomeInput,
) (model.OutcomeResult, error) {
	input.DetailCode = redact.Text(input.DetailCode)
	input.Evidence = redact.Text(input.Evidence)
	eventID, err := s.store.AppendRepairOutcome(ctx, input)
	if err != nil {
		return model.OutcomeResult{}, err
	}
	return model.OutcomeResult{EventID: eventID, Status: "recorded"}, nil
}

func (s *Service) ProposeClusters(
	ctx context.Context,
	threshold float64,
) (model.ClusterRunResult, error) {
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
	eventID, err := s.store.AppendGeneralizationReview(ctx, input)
	if err != nil {
		return model.OutcomeResult{}, err
	}
	return model.OutcomeResult{EventID: eventID, Status: "recorded"}, nil
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

func (s *Service) semanticStatus() string {
	if s.index.Semantic() {
		return "ready"
	}
	return "vector_fallback_nonsemantic"
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
		cause.Layer,
		cause.FailureMode,
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
		CauseLayer:      cause.Layer,
		FailureMode:     cause.FailureMode,
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
