package sqlite

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/gofrs/flock"
	_ "modernc.org/sqlite"
	_ "modernc.org/sqlite/vec"

	"github.com/CongBao/failure-memory/internal/config"
	"github.com/CongBao/failure-memory/internal/identity"
	"github.com/CongBao/failure-memory/internal/lessonmanifest"
	"github.com/CongBao/failure-memory/internal/model"
	"github.com/CongBao/failure-memory/internal/policy"
)

const schemaV1 = `
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS store_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT NOT NULL,
    transport TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS event_log_type_sequence_idx
ON event_log(event_type, sequence);

CREATE INDEX IF NOT EXISTS event_log_operation_idx
ON event_log(operation_id, sequence);

CREATE TRIGGER IF NOT EXISTS event_log_no_update
BEFORE UPDATE ON event_log BEGIN
    SELECT RAISE(ABORT, 'event_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS event_log_no_delete
BEFORE DELETE ON event_log BEGIN
    SELECT RAISE(ABORT, 'event_log is append-only');
END;

CREATE TABLE IF NOT EXISTS lesson_projection (
    lesson_id TEXT PRIMARY KEY,
    lesson_version_id TEXT NOT NULL UNIQUE,
    signature TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    rule TEXT NOT NULL,
    prevention TEXT NOT NULL,
    verification TEXT NOT NULL,
    applicability TEXT NOT NULL,
    counterexamples TEXT NOT NULL,
    cause_layer TEXT NOT NULL,
    failure_mode TEXT NOT NULL,
    component TEXT NOT NULL,
    document TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_event_id TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS lesson_projection_cause_idx
ON lesson_projection(cause_layer, failure_mode, component);

CREATE TRIGGER IF NOT EXISTS lesson_projection_no_update
BEFORE UPDATE ON lesson_projection BEGIN
    SELECT RAISE(ABORT, 'lesson_projection is append-only');
END;

CREATE TRIGGER IF NOT EXISTS lesson_projection_no_delete
BEFORE DELETE ON lesson_projection BEGIN
    SELECT RAISE(ABORT, 'lesson_projection is append-only');
END;

CREATE TABLE IF NOT EXISTS incident_projection (
    incident_id TEXT PRIMARY KEY,
    capture_event_id TEXT NOT NULL,
    lesson_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    expected_invariant TEXT NOT NULL,
    observed_outcome TEXT NOT NULL,
    material_impact TEXT NOT NULL,
    cause_evidence TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_event_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(lesson_id) REFERENCES lesson_projection(lesson_id)
);

CREATE INDEX IF NOT EXISTS incident_projection_lesson_idx
ON incident_projection(lesson_id, created_at);

CREATE TRIGGER IF NOT EXISTS incident_projection_no_update
BEFORE UPDATE ON incident_projection BEGIN
    SELECT RAISE(ABORT, 'incident_projection is append-only');
END;

CREATE TRIGGER IF NOT EXISTS incident_projection_no_delete
BEFORE DELETE ON incident_projection BEGIN
    SELECT RAISE(ABORT, 'incident_projection is append-only');
END;

CREATE TABLE IF NOT EXISTS import_receipt (
    import_id TEXT PRIMARY KEY,
    source_identity TEXT NOT NULL UNIQUE,
    source_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    lesson_count INTEGER NOT NULL
);

CREATE TRIGGER IF NOT EXISTS import_receipt_no_update
BEFORE UPDATE ON import_receipt BEGIN
    SELECT RAISE(ABORT, 'import_receipt is append-only');
END;

CREATE TRIGGER IF NOT EXISTS import_receipt_no_delete
BEFORE DELETE ON import_receipt BEGIN
    SELECT RAISE(ABORT, 'import_receipt is append-only');
END;
`

type Store struct {
	db        *sql.DB
	path      string
	context   model.Context
	storeID   string
	usageLock *flock.Flock
}

type RecordResult struct {
	CaptureEventID  string
	IncidentID      string
	LessonID        string
	LessonVersionID string
	RepairID        string
	Deduplication   string
	RelatedVersions []string
	LessonRevision  int64
}

type RecallCandidate struct {
	LessonVersionID string   `json:"lesson_version_id"`
	Rank            int      `json:"rank"`
	Score           float64  `json:"score"`
	Reasons         []string `json:"reasons"`
	Selected        bool     `json:"selected"`
}

func Open(path string, runtimeContext model.Context) (*Store, error) {
	if err := config.EnsurePrivateDir(filepath.Dir(path)); err != nil {
		return nil, fmt.Errorf("create event-store directory: %w", err)
	}
	if err := recoverInterruptedRestore(path); err != nil {
		return nil, err
	}
	openContext, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	usageLock := flock.New(path + ".usage.lock")
	locked, err := usageLock.TryRLockContext(openContext, 25*time.Millisecond)
	if err != nil {
		return nil, fmt.Errorf("lock event store for use: %w", err)
	}
	if !locked {
		return nil, errors.New("event store is exclusively locked for maintenance")
	}
	releaseUsageLock := func() {
		_ = usageLock.Unlock()
		_ = usageLock.Close()
	}
	dsn := fmt.Sprintf(
		"file:%s?_txlock=immediate&_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)&_pragma=synchronous(NORMAL)",
		filepath.ToSlash(path),
	)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		releaseUsageLock()
		return nil, fmt.Errorf("open event store: %w", err)
	}
	db.SetMaxOpenConns(4)
	db.SetMaxIdleConns(2)
	if err := db.PingContext(openContext); err != nil {
		_ = db.Close()
		releaseUsageLock()
		return nil, fmt.Errorf("ping event store: %w", err)
	}
	if err := makePrivate(path); err != nil {
		_ = db.Close()
		releaseUsageLock()
		return nil, err
	}
	storeID, err := initializeAndMigrate(openContext, db, path)
	if err != nil {
		_ = db.Close()
		releaseUsageLock()
		return nil, err
	}
	return &Store{
		db: db, path: path, context: runtimeContext, storeID: storeID, usageLock: usageLock,
	}, nil
}

func makePrivate(path string) error {
	if err := os.Chmod(path, 0o600); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("protect event store: %w", err)
	}
	return nil
}

func (s *Store) Close() error {
	dbErr := s.db.Close()
	var lockErr error
	if s.usageLock != nil {
		lockErr = errors.Join(s.usageLock.Unlock(), s.usageLock.Close())
	}
	return errors.Join(dbErr, lockErr)
}

func (s *Store) StoreID() string {
	return s.storeID
}

func (s *Store) Path() string {
	return s.path
}

func (s *Store) Record(
	ctx context.Context,
	operationID string,
	input model.RememberInput,
	assessment policy.Assessment,
	signature string,
	document *model.LessonDocument,
	relatedVersions []string,
) (RecordResult, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return RecordResult{}, err
	}
	defer func() { _ = tx.Rollback() }()

	capturePayload := map[string]any{
		"summary":         input.Summary,
		"classification":  input.Classification,
		"failure_portion": input.FailurePortion,
		"decision":        assessment.Decision,
		"reason_codes":    assessment.ReasonCodes,
		"expectation":     input.Expectation,
		"observed":        input.Observed,
		"cause":           input.Cause,
		"proposed_lesson": input.Lesson,
		"prior_recall_id": input.PriorRecallID,
		"redaction_state": "applied",
		"qualification_v": 1,
	}
	captureEventID, err := s.appendEvent(tx, "capture_evaluated", operationID, capturePayload)
	if err != nil {
		return RecordResult{}, err
	}
	result := RecordResult{
		CaptureEventID: captureEventID,
		Deduplication:  "not_run",
	}
	if assessment.Decision != model.Accept {
		if _, err := s.appendEvent(tx, "recording_completed", operationID, map[string]any{
			"capture_event_id": captureEventID,
			"status":           assessment.Status,
			"decision":         assessment.Decision,
			"deduplication":    result.Deduplication,
		}); err != nil {
			return RecordResult{}, err
		}
		if err := tx.Commit(); err != nil {
			return RecordResult{}, err
		}
		return result, nil
	}
	if document == nil || input.Expectation == nil || input.Observed == nil || input.Cause == nil {
		return RecordResult{}, errors.New("accepted failure is missing durable evidence")
	}
	result.RepairID = identity.New("repair")
	if _, err := s.appendEvent(tx, "repair_recommended", operationID, map[string]any{
		"recommendation_id":  result.RepairID,
		"capture_event_id":   captureEventID,
		"target_layer":       input.Cause.Layer,
		"target_reference":   input.Cause.Component,
		"recommended_change": input.Cause.RecommendedChange,
		"verification":       input.Cause.Verification,
		"evidence":           input.Cause.Evidence,
		"confidence":         input.Cause.Confidence,
	}); err != nil {
		return RecordResult{}, err
	}

	existing, found, err := lessonBySignatureTx(tx, signature)
	if err != nil {
		return RecordResult{}, err
	}
	if found {
		result.LessonID = existing.LessonID
		result.LessonVersionID = existing.LessonVersionID
		result.Deduplication = "exact_reuse"
		if _, err := s.appendEvent(tx, "lesson_reused", operationID, map[string]any{
			"capture_event_id":  captureEventID,
			"lesson_id":         existing.LessonID,
			"lesson_version_id": existing.LessonVersionID,
			"signature":         signature,
		}); err != nil {
			return RecordResult{}, err
		}
	} else {
		result.LessonID = identity.New("lesson")
		result.LessonVersionID = identity.New("lessonv")
		result.Deduplication = "distinct"
		lessonEventID, err := s.appendEvent(tx, "lesson_proposed", operationID, map[string]any{
			"capture_event_id":  captureEventID,
			"lesson_id":         result.LessonID,
			"lesson_version_id": result.LessonVersionID,
			"signature":         signature,
			"title":             document.Title,
			"rule":              document.Rule,
			"prevention":        document.Prevention,
			"verification":      document.Verification,
			"applicability":     document.Applicability,
			"counterexamples":   document.Counterexamples,
			"cause_layer":       document.CauseLayer,
			"failure_mode":      document.FailureMode,
			"component":         document.Component,
			"state":             "proposed",
		})
		if err != nil {
			return RecordResult{}, err
		}
		now := time.Now().UTC().Format(time.RFC3339Nano)
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO lesson_projection(
				lesson_id, lesson_version_id, signature, title, rule, prevention,
				verification, applicability, counterexamples, cause_layer,
				failure_mode, component, document, state, created_at, source_event_id
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)`,
			result.LessonID,
			result.LessonVersionID,
			signature,
			document.Title,
			document.Rule,
			document.Prevention,
			document.Verification,
			document.Applicability,
			document.Counterexamples,
			document.CauseLayer,
			document.FailureMode,
			document.Component,
			document.Document,
			now,
			lessonEventID,
		); err != nil {
			return RecordResult{}, err
		}
		if len(relatedVersions) > 0 {
			result.Deduplication = "related_pending_generalization"
			result.RelatedVersions = append([]string(nil), relatedVersions...)
			if _, err := s.appendEvent(tx, "generalization_review_proposed", operationID, map[string]any{
				"capture_event_id":           captureEventID,
				"new_lesson_version_id":      result.LessonVersionID,
				"related_lesson_version_ids": relatedVersions,
				"recommendation":             "review_related",
				"state":                      "proposed",
			}); err != nil {
				return RecordResult{}, err
			}
		}
	}
	if result.LessonRevision, err = lessonRevisionTx(ctx, tx); err != nil {
		return RecordResult{}, err
	}

	result.IncidentID = identity.New("incident")
	incidentEventID, err := s.appendEvent(tx, "incident_recorded", operationID, map[string]any{
		"incident_id":        result.IncidentID,
		"capture_event_id":   captureEventID,
		"lesson_id":          result.LessonID,
		"lesson_version_id":  result.LessonVersionID,
		"summary":            input.Summary,
		"expected_invariant": input.Expectation.Invariant,
		"observed_outcome":   input.Observed.Outcome,
		"material_impact":    input.Observed.Impact,
		"cause_evidence":     input.Cause.Evidence,
	})
	if err != nil {
		return RecordResult{}, err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO incident_projection(
			incident_id, capture_event_id, lesson_id, summary, expected_invariant,
			observed_outcome, material_impact, cause_evidence, created_at, source_event_id
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		result.IncidentID,
		captureEventID,
		result.LessonID,
		input.Summary,
		input.Expectation.Invariant,
		input.Observed.Outcome,
		input.Observed.Impact,
		input.Cause.Evidence,
		time.Now().UTC().Format(time.RFC3339Nano),
		incidentEventID,
	); err != nil {
		return RecordResult{}, err
	}
	if input.PriorRecallID != "" {
		if _, err := s.appendEvent(tx, "recall_failed_to_prevent", operationID, map[string]any{
			"recall_attempt_id": input.PriorRecallID,
			"incident_id":       result.IncidentID,
			"lesson_id":         result.LessonID,
		}); err != nil {
			return RecordResult{}, err
		}
	}
	if _, err := s.appendEvent(tx, "recording_completed", operationID, map[string]any{
		"capture_event_id":  captureEventID,
		"incident_id":       result.IncidentID,
		"lesson_id":         result.LessonID,
		"lesson_version_id": result.LessonVersionID,
		"repair_id":         result.RepairID,
		"status":            assessment.Status,
		"decision":          assessment.Decision,
		"deduplication":     result.Deduplication,
	}); err != nil {
		return RecordResult{}, err
	}
	if err := tx.Commit(); err != nil {
		return RecordResult{}, err
	}
	return result, nil
}

func (s *Store) AppendRecallOutcome(ctx context.Context, input model.RecallOutcomeInput) (string, error) {
	valid := map[string]bool{
		"useful": true, "not_useful": true, "false_positive": true,
		"prevented_recurrence": true, "contradicted_current_task": true,
		"stale": true, "ignored": true, "unknown": true,
	}
	if !valid[input.Outcome] {
		return "", fmt.Errorf("unsupported recall outcome %q", input.Outcome)
	}
	if input.Confidence < 0 || input.Confidence > 1 {
		return "", errors.New("confidence must be between 0 and 1")
	}
	found, err := s.eventPayloadIDExists(ctx, "recall_attempted", "attempt_id", input.RecallAttemptID)
	if err != nil {
		return "", err
	}
	if !found {
		return "", errors.New("recall attempt does not exist")
	}
	if input.LessonVersionID != "" {
		var count int
		if err := s.db.QueryRowContext(ctx, `
			SELECT COUNT(*) FROM event_log
			WHERE event_type = 'recall_candidate_observed'
			  AND json_extract(payload_json, '$.attempt_id') = ?
			  AND json_extract(payload_json, '$.lesson_version_id') = ?`,
			input.RecallAttemptID,
			input.LessonVersionID,
		).Scan(&count); err != nil {
			return "", err
		}
		if count == 0 {
			return "", errors.New("lesson was not a candidate in this recall")
		}
	}
	return s.appendStandaloneEvent(ctx, "recall_outcome_observed", identity.New("feedback"), input)
}

func (s *Store) AppendRepairOutcome(ctx context.Context, input model.RepairOutcomeInput) (string, error) {
	valid := map[string]bool{
		"applied": true, "rejected": true, "partially_applied": true,
		"verified_effective": true, "verified_ineffective": true,
		"recurrence_observed": true, "superseded": true,
	}
	if !valid[input.Outcome] {
		return "", fmt.Errorf("unsupported repair outcome %q", input.Outcome)
	}
	found, err := s.eventPayloadIDExists(
		ctx, "repair_recommended", "recommendation_id", input.RepairRecommendationID,
	)
	if err != nil {
		return "", err
	}
	if !found {
		return "", errors.New("repair recommendation does not exist")
	}
	return s.appendStandaloneEvent(ctx, "repair_outcome_observed", identity.New("repairfb"), input)
}

func (s *Store) AppendClusterRun(
	ctx context.Context,
	profile string,
	semantic bool,
	threshold float64,
	lessonCount int,
	clusters []model.Cluster,
) (string, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return "", err
	}
	defer func() { _ = tx.Rollback() }()
	operationID := identity.New("clusterop")
	runID := identity.New("cluster")
	if _, err := s.appendEvent(tx, "lesson_cluster_proposed", operationID, map[string]any{
		"run_id":             runID,
		"retrieval_profile":  profile,
		"semantic":           semantic,
		"distance_threshold": threshold,
		"lesson_count":       lessonCount,
		"cluster_count":      len(clusters),
		"state":              "proposed",
	}); err != nil {
		return "", err
	}
	for _, cluster := range clusters {
		if _, err := s.appendEvent(tx, "generalization_proposal_created", operationID, map[string]any{
			"run_id":                     runID,
			"cluster_key":                cluster.Key,
			"supporting_lesson_versions": cluster.LessonVersionIDs,
			"state":                      "proposed",
		}); err != nil {
			return "", err
		}
	}
	if err := tx.Commit(); err != nil {
		return "", err
	}
	return runID, nil
}

func (s *Store) AppendGeneralizationReview(
	ctx context.Context,
	input model.GeneralizationReviewInput,
) (string, error) {
	if input.Decision != "accept" && input.Decision != "reject" && input.Decision != "defer" {
		return "", errors.New("decision must be accept, reject, or defer")
	}
	if strings.TrimSpace(input.RationaleCode) == "" || len(input.SupportingLessonVersions) < 2 {
		return "", errors.New("rationale and at least two supporting lessons are required")
	}
	found, err := s.eventPayloadIDExists(ctx, "lesson_cluster_proposed", "run_id", input.RunID)
	if err != nil {
		return "", err
	}
	if !found {
		return "", errors.New("cluster run does not exist")
	}
	payload := map[string]any{
		"run_id":                         input.RunID,
		"cluster_key":                    input.ClusterKey,
		"decision":                       input.Decision,
		"rationale_code":                 input.RationaleCode,
		"supporting_lesson_version_ids":  input.SupportingLessonVersions,
		"counterexample_lesson_versions": input.CounterexampleVersions,
	}
	return s.appendStandaloneEvent(
		ctx, "generalization_proposal_reviewed", identity.New("clusterreview"), payload,
	)
}

func (s *Store) ImportLegacy(
	ctx context.Context,
	legacy model.LegacyImport,
) (model.LegacyImportResult, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return model.LegacyImportResult{}, err
	}
	defer func() { _ = tx.Rollback() }()
	var existingID string
	err = tx.QueryRowContext(ctx, `
		SELECT import_id FROM import_receipt WHERE source_identity = ?`,
		legacy.SourceIdentity,
	).Scan(&existingID)
	if err == nil {
		return model.LegacyImportResult{
			ImportID:     existingID,
			Status:       "already_imported",
			CaptureCount: len(legacy.Captures),
		}, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return model.LegacyImportResult{}, err
	}
	operationID := identity.New("importop")
	importID := identity.New("import")
	result := model.LegacyImportResult{
		ImportID:     importID,
		Status:       "imported",
		CaptureCount: len(legacy.Captures),
	}
	equivalent, err := legacyAlreadyProjected(ctx, tx, legacy.Captures)
	if err != nil {
		return model.LegacyImportResult{}, err
	}
	if equivalent && len(legacy.Captures) > 0 {
		eventID, err := s.appendEvent(tx, "legacy_store_import_alias_registered", operationID, map[string]any{
			"import_id":       importID,
			"source_identity": legacy.SourceIdentity,
			"source_sha256":   legacy.SourceSHA256,
			"captures":        len(legacy.Captures),
		})
		if err != nil {
			return model.LegacyImportResult{}, err
		}
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO import_receipt(
				import_id, source_identity, source_sha256, imported_at, event_count, lesson_count
			) VALUES (?, ?, ?, ?, 1, 0)`,
			importID,
			legacy.SourceIdentity,
			legacy.SourceSHA256,
			time.Now().UTC().Format(time.RFC3339Nano),
		); err != nil {
			return model.LegacyImportResult{}, err
		}
		if err := tx.Commit(); err != nil {
			return model.LegacyImportResult{}, err
		}
		_ = eventID
		result.Status = "already_imported_equivalent"
		result.IncidentCount = 0
		result.LessonCount = 0
		result.RepairCount = 0
		return result, nil
	}
	importedLessons := map[string]bool{}
	for _, capture := range legacy.Captures {
		captureEventID, err := s.appendEvent(tx, "capture_evaluated", operationID, map[string]any{
			"legacy_capture_id":    capture.CaptureID,
			"legacy_created_at":    capture.CreatedAt,
			"legacy_harness":       capture.SourceHarness,
			"summary":              capture.Summary,
			"classification":       capture.Classification,
			"decision":             capture.Decision,
			"reason_codes_json":    capture.ReasonCodesJSON,
			"expectation_source":   capture.ExpectationSource,
			"expectation_evidence": capture.ExpectationEvidence,
			"failure_portion":      capture.FailurePortion,
			"migration_source":     legacy.SourceIdentity,
			"qualification_v":      0,
		})
		if err != nil {
			return model.LegacyImportResult{}, err
		}
		if capture.LessonVersionID != "" && !importedLessons[capture.LessonVersionID] {
			causeLayer := fallback(capture.CauseLayer, "unknown")
			failureMode := fallback(capture.FailureMode, "unknown")
			component := fallback(capture.Component, capture.ControllableCause)
			document := strings.Join([]string{
				capture.LessonTitle,
				capture.ExpectedInvariant,
				capture.Outcome,
				capture.ControllableCause,
				capture.MaterialImpact,
				causeLayer,
				failureMode,
				component,
				capture.LessonRule,
				capture.LessonPrevention,
				capture.LessonVerification,
				capture.LessonApplicability,
				capture.LessonCounterexamples,
			}, "\n")
			lessonEventID, err := s.appendEvent(tx, "lesson_imported", operationID, map[string]any{
				"lesson_id":         capture.LessonID,
				"lesson_version_id": capture.LessonVersionID,
				"legacy_signature":  capture.LessonSignature,
				"migration_source":  legacy.SourceIdentity,
				"state":             fallback(capture.LessonState, "proposed"),
			})
			if err != nil {
				return model.LegacyImportResult{}, err
			}
			_, err = tx.ExecContext(ctx, `
				INSERT INTO lesson_projection(
					lesson_id, lesson_version_id, signature, title, rule, prevention,
					verification, applicability, counterexamples, cause_layer,
					failure_mode, component, document, state, created_at, source_event_id
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
				capture.LessonID,
				capture.LessonVersionID,
				capture.LessonSignature,
				capture.LessonTitle,
				capture.LessonRule,
				capture.LessonPrevention,
				capture.LessonVerification,
				capture.LessonApplicability,
				capture.LessonCounterexamples,
				causeLayer,
				failureMode,
				component,
				document,
				fallback(capture.LessonState, "proposed"),
				capture.CreatedAt,
				lessonEventID,
			)
			if err != nil {
				return model.LegacyImportResult{}, err
			}
			importedLessons[capture.LessonVersionID] = true
			result.LessonCount++
		}
		if capture.IncidentID != "" {
			incidentEventID, err := s.appendEvent(tx, "incident_imported", operationID, map[string]any{
				"incident_id":       capture.IncidentID,
				"legacy_capture_id": capture.CaptureID,
				"lesson_id":         capture.LessonID,
				"lesson_version_id": capture.LessonVersionID,
				"migration_source":  legacy.SourceIdentity,
			})
			if err != nil {
				return model.LegacyImportResult{}, err
			}
			_, err = tx.ExecContext(ctx, `
				INSERT INTO incident_projection(
					incident_id, capture_event_id, lesson_id, summary, expected_invariant,
					observed_outcome, material_impact, cause_evidence, created_at, source_event_id
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
				capture.IncidentID,
				captureEventID,
				capture.LessonID,
				capture.Summary,
				capture.ExpectedInvariant,
				capture.Outcome,
				capture.MaterialImpact,
				fallback(capture.CauseEvidence, capture.ControllableCause),
				capture.CreatedAt,
				incidentEventID,
			)
			if err != nil {
				return model.LegacyImportResult{}, err
			}
			result.IncidentCount++
		}
		if capture.RepairID != "" {
			if _, err := s.appendEvent(tx, "repair_recommended", operationID, map[string]any{
				"recommendation_id":  capture.RepairID,
				"legacy_capture_id":  capture.CaptureID,
				"target_layer":       capture.RepairTargetLayer,
				"target_reference":   capture.RepairTarget,
				"recommended_change": capture.RecommendedChange,
				"verification":       capture.RepairVerification,
				"evidence":           capture.RepairRationale,
				"confidence":         capture.RepairConfidence,
				"migration_source":   legacy.SourceIdentity,
			}); err != nil {
				return model.LegacyImportResult{}, err
			}
			result.RepairCount++
		}
	}
	if _, err := s.appendEvent(tx, "legacy_store_imported", operationID, map[string]any{
		"import_id":       importID,
		"source_identity": legacy.SourceIdentity,
		"source_sha256":   legacy.SourceSHA256,
		"captures":        result.CaptureCount,
		"incidents":       result.IncidentCount,
		"lessons":         result.LessonCount,
		"repairs":         result.RepairCount,
	}); err != nil {
		return model.LegacyImportResult{}, err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO import_receipt(
			import_id, source_identity, source_sha256, imported_at, event_count, lesson_count
		) VALUES (?, ?, ?, ?, ?, ?)`,
		importID,
		legacy.SourceIdentity,
		legacy.SourceSHA256,
		time.Now().UTC().Format(time.RFC3339Nano),
		result.CaptureCount+result.IncidentCount+result.LessonCount+result.RepairCount+1,
		result.LessonCount,
	); err != nil {
		return model.LegacyImportResult{}, err
	}
	if _, err := syncLessonRevisionTx(ctx, tx); err != nil {
		return model.LegacyImportResult{}, err
	}
	if err := tx.Commit(); err != nil {
		return model.LegacyImportResult{}, err
	}
	return result, nil
}

func (s *Store) AppendRecall(
	ctx context.Context,
	operationID string,
	input model.RecallInput,
	mode string,
	semanticStatus string,
	candidates []RecallCandidate,
) (string, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return "", err
	}
	defer func() { _ = tx.Rollback() }()
	attemptID := identity.New("recall")
	_, err = s.appendEvent(tx, "recall_attempted", operationID, map[string]any{
		"attempt_id":      attemptID,
		"query":           input,
		"mode":            mode,
		"semantic_status": semanticStatus,
		"candidate_count": len(candidates),
	})
	if err != nil {
		return "", err
	}
	for _, candidate := range candidates {
		if _, err := s.appendEvent(tx, "recall_candidate_observed", operationID, map[string]any{
			"attempt_id":        attemptID,
			"lesson_version_id": candidate.LessonVersionID,
			"rank":              candidate.Rank,
			"score":             candidate.Score,
			"reasons":           candidate.Reasons,
			"selected":          candidate.Selected,
		}); err != nil {
			return "", err
		}
	}
	if err := tx.Commit(); err != nil {
		return "", err
	}
	return attemptID, nil
}

func (s *Store) ListLessons(ctx context.Context) ([]model.LessonDocument, error) {
	return listLessons(ctx, s.db)
}

type lessonQueryer interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func listLessons(ctx context.Context, queryer lessonQueryer) ([]model.LessonDocument, error) {
	rows, err := queryer.QueryContext(ctx, `
		SELECT lesson_id, lesson_version_id, signature, title, rule, prevention,
		       verification, applicability, counterexamples, cause_layer,
		       failure_mode, component, document, created_at
		FROM lesson_projection
		WHERE state IN ('proposed', 'active')
		ORDER BY created_at, lesson_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var lessons []model.LessonDocument
	for rows.Next() {
		var lesson model.LessonDocument
		var created string
		if err := rows.Scan(
			&lesson.LessonID,
			&lesson.LessonVersionID,
			&lesson.Signature,
			&lesson.Title,
			&lesson.Rule,
			&lesson.Prevention,
			&lesson.Verification,
			&lesson.Applicability,
			&lesson.Counterexamples,
			&lesson.CauseLayer,
			&lesson.FailureMode,
			&lesson.Component,
			&lesson.Document,
			&created,
		); err != nil {
			return nil, err
		}
		lesson.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
		lessons = append(lessons, lesson)
	}
	return lessons, rows.Err()
}

func (s *Store) LessonSnapshot(
	ctx context.Context,
) ([]model.LessonDocument, int64, string, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, 0, "", err
	}
	defer func() { _ = tx.Rollback() }()
	revision, err := lessonRevisionTx(ctx, tx)
	if err != nil {
		return nil, 0, "", err
	}
	lessons, err := listLessons(ctx, tx)
	if err != nil {
		return nil, 0, "", err
	}
	if err := tx.Commit(); err != nil {
		return nil, 0, "", err
	}
	return lessons, revision, lessonmanifest.Digest(lessons), nil
}

func (s *Store) LessonByVersion(ctx context.Context, versionID string) (model.LessonDocument, bool, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT lesson_id, lesson_version_id, signature, title, rule, prevention,
		       verification, applicability, counterexamples, cause_layer,
		       failure_mode, component, document, created_at
		FROM lesson_projection WHERE lesson_version_id = ?`, versionID)
	var lesson model.LessonDocument
	var created string
	err := row.Scan(
		&lesson.LessonID,
		&lesson.LessonVersionID,
		&lesson.Signature,
		&lesson.Title,
		&lesson.Rule,
		&lesson.Prevention,
		&lesson.Verification,
		&lesson.Applicability,
		&lesson.Counterexamples,
		&lesson.CauseLayer,
		&lesson.FailureMode,
		&lesson.Component,
		&lesson.Document,
		&created,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return model.LessonDocument{}, false, nil
	}
	if err != nil {
		return model.LessonDocument{}, false, err
	}
	lesson.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	return lesson, true, nil
}

func (s *Store) Counts(ctx context.Context) (map[string]int64, error) {
	result := map[string]int64{}
	for name, query := range map[string]string{
		"events":    "SELECT COUNT(*) FROM event_log",
		"incidents": "SELECT COUNT(*) FROM incident_projection",
		"lessons":   "SELECT COUNT(*) FROM lesson_projection",
		"captures":  "SELECT COUNT(*) FROM event_log WHERE event_type = 'capture_evaluated'",
		"recalls":   "SELECT COUNT(*) FROM event_log WHERE event_type = 'recall_attempted'",
	} {
		var count int64
		if err := s.db.QueryRowContext(ctx, query).Scan(&count); err != nil {
			return nil, err
		}
		result[name] = count
	}
	return result, nil
}

func (s *Store) EventCounts(ctx context.Context) (map[string]int64, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT event_type, COUNT(*) FROM event_log GROUP BY event_type ORDER BY event_type`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]int64{}
	for rows.Next() {
		var eventType string
		var count int64
		if err := rows.Scan(&eventType, &count); err != nil {
			return nil, err
		}
		result[eventType] = count
	}
	return result, rows.Err()
}

func (s *Store) OutcomeCounts(ctx context.Context) (map[string]map[string]int64, error) {
	result := map[string]map[string]int64{
		"recall": {},
		"repair": {},
	}
	for category, eventType := range map[string]string{
		"recall": "recall_outcome_observed",
		"repair": "repair_outcome_observed",
	} {
		rows, err := s.db.QueryContext(ctx, `
			SELECT json_extract(payload_json, '$.outcome'), COUNT(*)
			FROM event_log
			WHERE event_type = ?
			GROUP BY json_extract(payload_json, '$.outcome')
			ORDER BY json_extract(payload_json, '$.outcome')`,
			eventType,
		)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var outcome string
			var count int64
			if err := rows.Scan(&outcome, &count); err != nil {
				_ = rows.Close()
				return nil, err
			}
			result[category][outcome] = count
		}
		if err := rows.Close(); err != nil {
			return nil, err
		}
	}
	return result, nil
}

func (s *Store) Doctor(ctx context.Context) (map[string]any, error) {
	var integrity string
	if err := s.db.QueryRowContext(ctx, "PRAGMA quick_check").Scan(&integrity); err != nil {
		return nil, err
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT event_id, payload_json, payload_sha256 FROM event_log ORDER BY sequence`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var bad []string
	for rows.Next() {
		var eventID, payload, expected string
		if err := rows.Scan(&eventID, &payload, &expected); err != nil {
			return nil, err
		}
		sum := sha256.Sum256([]byte(payload))
		if hex.EncodeToString(sum[:]) != expected {
			bad = append(bad, eventID)
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	counts, err := s.Counts(ctx)
	if err != nil {
		return nil, err
	}
	lessons, revision, manifest, err := s.LessonSnapshot(ctx)
	if err != nil {
		return nil, err
	}
	version, err := s.SchemaVersion(ctx)
	if err != nil {
		return nil, err
	}
	sort.Strings(bad)
	return map[string]any{
		"store_id":               s.storeID,
		"schema_version":         version,
		"integrity_check":        integrity,
		"event_hash_errors":      bad,
		"counts":                 counts,
		"lesson_revision":        revision,
		"lesson_manifest_sha256": manifest,
		"manifest_lesson_count":  len(lessons),
	}, nil
}

func (s *Store) appendEvent(
	tx *sql.Tx,
	eventType string,
	operationID string,
	payload any,
) (string, error) {
	data, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	eventID := identity.New("event")
	_, err = tx.Exec(`
		INSERT INTO event_log(
			event_id, event_type, schema_version, occurred_at, source_harness,
			workspace_fingerprint, session_fingerprint, transport, operation_id,
			payload_json, payload_sha256
		) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)`,
		eventID,
		eventType,
		time.Now().UTC().Format(time.RFC3339Nano),
		s.context.Harness,
		s.context.WorkspaceFingerprint,
		s.context.SessionFingerprint,
		s.context.Transport,
		operationID,
		string(data),
		hex.EncodeToString(sum[:]),
	)
	if err != nil {
		return "", err
	}
	return eventID, nil
}

func (s *Store) appendStandaloneEvent(
	ctx context.Context,
	eventType string,
	operationID string,
	payload any,
) (string, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return "", err
	}
	defer func() { _ = tx.Rollback() }()
	eventID, err := s.appendEvent(tx, eventType, operationID, payload)
	if err != nil {
		return "", err
	}
	if err := tx.Commit(); err != nil {
		return "", err
	}
	return eventID, nil
}

func (s *Store) eventPayloadIDExists(
	ctx context.Context,
	eventType string,
	field string,
	value string,
) (bool, error) {
	if strings.TrimSpace(value) == "" {
		return false, nil
	}
	switch field {
	case "attempt_id", "recommendation_id", "run_id":
	default:
		return false, errors.New("unsupported event identifier field")
	}
	query := fmt.Sprintf(`
		SELECT COUNT(*) FROM event_log
		WHERE event_type = ? AND json_extract(payload_json, '$.%s') = ?`, field)
	var count int
	if err := s.db.QueryRowContext(ctx, query, eventType, value).Scan(&count); err != nil {
		return false, err
	}
	return count > 0, nil
}

func lessonBySignatureTx(tx *sql.Tx, signature string) (model.LessonDocument, bool, error) {
	row := tx.QueryRow(`
		SELECT lesson_id, lesson_version_id, signature, title, rule, prevention,
		       verification, applicability, counterexamples, cause_layer,
		       failure_mode, component, document, created_at
		FROM lesson_projection WHERE signature = ?`, signature)
	var lesson model.LessonDocument
	var created string
	err := row.Scan(
		&lesson.LessonID,
		&lesson.LessonVersionID,
		&lesson.Signature,
		&lesson.Title,
		&lesson.Rule,
		&lesson.Prevention,
		&lesson.Verification,
		&lesson.Applicability,
		&lesson.Counterexamples,
		&lesson.CauseLayer,
		&lesson.FailureMode,
		&lesson.Component,
		&lesson.Document,
		&created,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return model.LessonDocument{}, false, nil
	}
	if err != nil {
		return model.LessonDocument{}, false, err
	}
	lesson.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	return lesson, true, nil
}

func CanonicalSignature(input model.RememberInput) string {
	if input.Expectation == nil || input.Cause == nil || input.Lesson == nil {
		return ""
	}
	parts := []string{
		input.Expectation.Invariant,
		input.Cause.Layer,
		input.Cause.FailureMode,
		input.Cause.Component,
		input.Lesson.Rule,
	}
	for index := range parts {
		parts[index] = normalize(parts[index])
	}
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x1f")))
	return hex.EncodeToString(sum[:])
}

func normalize(value string) string {
	return strings.Join(strings.Fields(strings.ToLower(strings.TrimSpace(value))), " ")
}

func fallback(value string, alternative string) string {
	if strings.TrimSpace(value) == "" {
		return alternative
	}
	return value
}

func legacyAlreadyProjected(
	ctx context.Context,
	tx *sql.Tx,
	captures []model.LegacyCapture,
) (bool, error) {
	for _, capture := range captures {
		var captureCount int
		if err := tx.QueryRowContext(ctx, `
			SELECT COUNT(*) FROM event_log
			WHERE event_type = 'capture_evaluated'
			  AND json_extract(payload_json, '$.legacy_capture_id') = ?`,
			capture.CaptureID,
		).Scan(&captureCount); err != nil {
			return false, err
		}
		if captureCount == 0 {
			return false, nil
		}
		if capture.LessonVersionID != "" {
			var lessonCount int
			if err := tx.QueryRowContext(ctx, `
				SELECT COUNT(*) FROM lesson_projection
				WHERE lesson_version_id = ? AND lesson_id = ?`,
				capture.LessonVersionID,
				capture.LessonID,
			).Scan(&lessonCount); err != nil {
				return false, err
			}
			if lessonCount == 0 {
				return false, nil
			}
		}
	}
	return true, nil
}
