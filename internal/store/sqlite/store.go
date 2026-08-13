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
	"strconv"
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
	RelevanceScore  float64  `json:"relevance_score"`
	Reasons         []string `json:"reasons"`
	Selected        bool     `json:"selected"`
}

type RecallTelemetry struct {
	RetrievedCount         int
	FilteredBelowThreshold int
	CollapsedByCluster     int
	TrimmedByAdaptiveLimit int
	AppliedTopK            int
	AppliedMinRelevance    float64
	AbstentionReason       string
	IndexSyncLatencyMS     int64
	SearchLatencyMS        int64
	HydrationLatencyMS     int64
	RequestBytes           int
	ResponseBytes          int
}

type MemoryOutcomeResult struct {
	EventID           string
	Duplicate         bool
	LessonID          string
	LessonVersionID   string
	RetrievalChanged  bool
	RetrievalRevision int64
}

type GeneralizationResult struct {
	EventID           string
	LessonID          string
	LessonVersionID   string
	RetrievalRevision int64
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
		"summary":                        input.Summary,
		"classification":                 input.Classification,
		"failure_portion":                input.FailurePortion,
		"decision":                       assessment.Decision,
		"reason_codes":                   assessment.ReasonCodes,
		"expectation":                    input.Expectation,
		"observed":                       input.Observed,
		"cause":                          input.Cause,
		"proposed_lesson":                input.Lesson,
		"prior_recall_id":                input.PriorRecallID,
		"correction_of_capture_event_id": input.CorrectionOf,
		"redaction_state":                "applied",
		"qualification_v":                1,
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
			var representative string
			if err := tx.QueryRow(`
				SELECT representative_lesson_version_id
				FROM lesson_lifecycle_projection WHERE lesson_version_id = ?`,
				relatedVersions[0],
			).Scan(&representative); err != nil {
				return RecordResult{}, err
			}
			if _, err := tx.Exec(`
				UPDATE lesson_lifecycle_projection
				SET representative_lesson_version_id = ?
				WHERE lesson_version_id = ?`,
				representative, result.LessonVersionID,
			); err != nil {
				return RecordResult{}, err
			}
		}
	}
	if result.LessonRevision, err = retrievalRevisionTx(ctx, tx); err != nil {
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

func (s *Store) AppendMemoryOutcome(
	ctx context.Context,
	input model.MemoryOutcomeInput,
) (MemoryOutcomeResult, error) {
	input.TargetType = model.MemoryTargetType(strings.ToLower(strings.TrimSpace(string(input.TargetType))))
	input.Outcome = model.MemoryOutcome(strings.ToLower(strings.TrimSpace(string(input.Outcome))))
	input.LessonVersionIDs = uniqueSortedStrings(input.LessonVersionIDs)
	if input.Confidence < 0 || input.Confidence > 1 {
		return MemoryOutcomeResult{}, errors.New("confidence must be between 0 and 1")
	}
	if strings.TrimSpace(input.EvidenceCode) == "" {
		return MemoryOutcomeResult{}, errors.New("evidence_code is required")
	}
	allowed := map[string]map[string]bool{
		"recall": {
			"applied": true, "not_applicable": true, "already_known": true,
			"contradicted": true, "prevented_recurrence": true,
			"failed_to_prevent": true, "ignored": true, "unknown": true,
		},
		"repair": {
			"applied": true, "partially_applied": true, "rejected": true,
			"verified_effective": true, "verified_ineffective": true,
			"recurrence_observed": true, "superseded": true,
		},
		"lesson": {
			"confirmed": true, "false_positive": true, "stale": true,
			"superseded": true, "needs_generalization": true,
		},
	}
	if !allowed[string(input.TargetType)][string(input.Outcome)] {
		return MemoryOutcomeResult{}, fmt.Errorf(
			"unsupported %s outcome %q", input.TargetType, input.Outcome,
		)
	}
	if strings.TrimSpace(input.TargetID) == "" {
		return MemoryOutcomeResult{}, errors.New("target_id is required")
	}
	if strings.TrimSpace(input.IdempotencyKey) == "" {
		digest := sha256.Sum256([]byte(strings.Join([]string{
			string(input.TargetType), input.TargetID, string(input.Outcome),
			strings.Join(input.LessonVersionIDs, ","), input.EvidenceCode,
			strconv.FormatFloat(input.Confidence, 'g', -1, 64),
		}, "\x00")))
		input.IdempotencyKey = hex.EncodeToString(digest[:])
	}
	requestData, err := json.Marshal(map[string]any{
		"target_type": input.TargetType, "target_id": input.TargetID,
		"outcome": input.Outcome, "lesson_version_ids": input.LessonVersionIDs,
		"evidence_code": input.EvidenceCode, "confidence": input.Confidence,
	})
	if err != nil {
		return MemoryOutcomeResult{}, err
	}
	requestDigest := sha256.Sum256(requestData)
	requestSHA256 := hex.EncodeToString(requestDigest[:])
	var existing, existingRequestSHA256 string
	err = s.db.QueryRowContext(ctx, `
		SELECT event_id, request_sha256 FROM memory_outcome_receipt
		WHERE idempotency_key = ?`, input.IdempotencyKey).Scan(
		&existing, &existingRequestSHA256,
	)
	if err == nil {
		if existingRequestSHA256 != requestSHA256 {
			return MemoryOutcomeResult{}, errors.New("idempotency_key was already used for a different outcome")
		}
		revision, revisionErr := s.RetrievalRevision(ctx)
		return MemoryOutcomeResult{
			EventID: existing, Duplicate: true, RetrievalRevision: revision,
		}, revisionErr
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return MemoryOutcomeResult{}, err
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return MemoryOutcomeResult{}, err
	}
	defer func() { _ = tx.Rollback() }()
	eventID := identity.New("event")
	receipt, err := tx.ExecContext(ctx, `
		INSERT OR IGNORE INTO memory_outcome_receipt(
			idempotency_key, event_id, request_sha256, created_at
		) VALUES (?, ?, ?, ?)`, input.IdempotencyKey, eventID, requestSHA256,
		time.Now().UTC().Format(time.RFC3339Nano))
	if err != nil {
		return MemoryOutcomeResult{}, err
	}
	claimed, err := receipt.RowsAffected()
	if err != nil {
		return MemoryOutcomeResult{}, err
	}
	if claimed == 0 {
		if err := tx.QueryRowContext(ctx, `
			SELECT event_id, request_sha256 FROM memory_outcome_receipt WHERE idempotency_key = ?`,
			input.IdempotencyKey,
		).Scan(&existing, &existingRequestSHA256); err != nil {
			return MemoryOutcomeResult{}, err
		}
		if existingRequestSHA256 != requestSHA256 {
			return MemoryOutcomeResult{}, errors.New("idempotency_key was already used for a different outcome")
		}
		revision, err := retrievalRevisionTx(ctx, tx)
		if err != nil {
			return MemoryOutcomeResult{}, err
		}
		if err := tx.Commit(); err != nil {
			return MemoryOutcomeResult{}, err
		}
		return MemoryOutcomeResult{
			EventID: existing, Duplicate: true, RetrievalRevision: revision,
		}, nil
	}
	retrievalChanged := false
	switch string(input.TargetType) {
	case "recall":
		if found, err := eventPayloadIDExistsTx(
			tx, "recall_attempted", "attempt_id", input.TargetID,
		); err != nil || !found {
			if err != nil {
				return MemoryOutcomeResult{}, err
			}
			return MemoryOutcomeResult{}, errors.New("recall attempt does not exist")
		}
		for _, versionID := range input.LessonVersionIDs {
			var count int
			if err := tx.QueryRowContext(ctx, `
				SELECT COUNT(*) FROM event_log
				WHERE event_type = 'recall_candidate_observed'
				  AND json_extract(payload_json, '$.attempt_id') = ?
				  AND json_extract(payload_json, '$.lesson_version_id') = ?
				  AND json_extract(payload_json, '$.selected') = 1`,
				input.TargetID, versionID,
			).Scan(&count); err != nil {
				return MemoryOutcomeResult{}, err
			}
			if count == 0 {
				return MemoryOutcomeResult{}, fmt.Errorf(
					"lesson %s was not returned by recall attempt", versionID,
				)
			}
		}
	case "repair":
		if found, err := eventPayloadIDExistsTx(
			tx, "repair_recommended", "recommendation_id", input.TargetID,
		); err != nil || !found {
			if err != nil {
				return MemoryOutcomeResult{}, err
			}
			return MemoryOutcomeResult{}, errors.New("repair recommendation does not exist")
		}
	case "lesson":
		if err := tx.QueryRow(`
			SELECT lesson_id FROM lesson_projection WHERE lesson_version_id = ?`,
			input.TargetID,
		).Scan(&existing); errors.Is(err, sql.ErrNoRows) {
			return MemoryOutcomeResult{}, errors.New("lesson version does not exist")
		} else if err != nil {
			return MemoryOutcomeResult{}, err
		}
	}
	eventID, err = s.appendEventWithID(
		tx, eventID, "memory_outcome_observed", identity.New("outcome"), input,
	)
	if err != nil {
		return MemoryOutcomeResult{}, err
	}
	if input.TargetType == "lesson" {
		var previousState string
		if err := tx.QueryRowContext(ctx, `
			SELECT state FROM lesson_lifecycle_projection WHERE lesson_version_id = ?`,
			input.TargetID,
		).Scan(&previousState); err != nil {
			return MemoryOutcomeResult{}, err
		}
		state := "active"
		switch string(input.Outcome) {
		case "false_positive", "stale", "superseded":
			state = string(input.Outcome)
		case "needs_generalization":
			state = "proposed"
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE lesson_lifecycle_projection
			SET state = ?, last_outcome = ?, updated_at = ?, source_event_id = ?
			WHERE lesson_version_id = ?`,
			state, string(input.Outcome), time.Now().UTC().Format(time.RFC3339Nano),
			eventID, input.TargetID,
		); err != nil {
			return MemoryOutcomeResult{}, err
		}
		retrievalChanged = searchableLifecycleState(previousState) != searchableLifecycleState(state)
		if retrievalChanged {
			if _, err := tx.ExecContext(ctx, `
				UPDATE store_metadata
				SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
				WHERE key = 'retrieval_revision'`); err != nil {
				return MemoryOutcomeResult{}, err
			}
		}
	}
	revision, err := retrievalRevisionTx(ctx, tx)
	if err != nil {
		return MemoryOutcomeResult{}, err
	}
	if err := tx.Commit(); err != nil {
		return MemoryOutcomeResult{}, err
	}
	result := MemoryOutcomeResult{EventID: eventID, RetrievalRevision: revision}
	if input.TargetType == "lesson" {
		result.LessonID = existing
		result.LessonVersionID = input.TargetID
		result.RetrievalChanged = retrievalChanged
	}
	return result, nil
}

func searchableLifecycleState(state string) bool {
	return state != "false_positive" && state != "stale" && state != "superseded"
}

func eventPayloadIDExistsTx(
	tx *sql.Tx,
	eventType string,
	field string,
	value string,
) (bool, error) {
	if field != "attempt_id" && field != "recommendation_id" {
		return false, errors.New("unsupported event identifier field")
	}
	query := fmt.Sprintf(`
		SELECT COUNT(*) FROM event_log
		WHERE event_type = ? AND json_extract(payload_json, '$.%s') = ?`, field)
	var count int
	if err := tx.QueryRow(query, eventType, value).Scan(&count); err != nil {
		return false, err
	}
	return count > 0, nil
}

func uniqueSortedStrings(values []string) []string {
	seen := map[string]bool{}
	result := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" || seen[value] {
			continue
		}
		seen[value] = true
		result = append(result, value)
	}
	sort.Strings(result)
	return result
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
	document *model.LessonDocument,
) (GeneralizationResult, error) {
	if input.Decision != "accept" && input.Decision != "reject" && input.Decision != "defer" {
		return GeneralizationResult{}, errors.New("decision must be accept, reject, or defer")
	}
	if strings.TrimSpace(input.RationaleCode) == "" || len(input.SupportingLessonVersions) < 2 {
		return GeneralizationResult{}, errors.New("rationale and at least two supporting lessons are required")
	}
	found, err := s.eventPayloadIDExists(ctx, "lesson_cluster_proposed", "run_id", input.RunID)
	if err != nil {
		return GeneralizationResult{}, err
	}
	if !found {
		return GeneralizationResult{}, errors.New("cluster run does not exist")
	}
	var proposalJSON string
	err = s.db.QueryRowContext(ctx, `
		SELECT payload_json FROM event_log
		WHERE event_type = 'generalization_proposal_created'
		  AND json_extract(payload_json, '$.run_id') = ?
		  AND json_extract(payload_json, '$.cluster_key') = ?
		ORDER BY sequence DESC LIMIT 1`, input.RunID, input.ClusterKey).Scan(&proposalJSON)
	if errors.Is(err, sql.ErrNoRows) {
		return GeneralizationResult{}, errors.New("cluster was not proposed by this run")
	}
	if err != nil {
		return GeneralizationResult{}, err
	}
	var proposal struct {
		SupportingLessonVersions []string `json:"supporting_lesson_versions"`
	}
	if err := json.Unmarshal([]byte(proposalJSON), &proposal); err != nil {
		return GeneralizationResult{}, fmt.Errorf("decode cluster proposal: %w", err)
	}
	if !sameStringSet(input.SupportingLessonVersions, proposal.SupportingLessonVersions) {
		return GeneralizationResult{}, errors.New("supporting lessons do not match the proposed cluster")
	}
	if input.Decision == "accept" && document == nil {
		return GeneralizationResult{}, errors.New("accepted generalization requires generalized_lesson")
	}
	payload := map[string]any{
		"run_id":                         input.RunID,
		"cluster_key":                    input.ClusterKey,
		"decision":                       input.Decision,
		"rationale_code":                 input.RationaleCode,
		"supporting_lesson_version_ids":  input.SupportingLessonVersions,
		"counterexample_lesson_versions": input.CounterexampleVersions,
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return GeneralizationResult{}, err
	}
	defer func() { _ = tx.Rollback() }()
	var finalized int
	if err := tx.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM event_log
		WHERE event_type = 'generalization_proposal_reviewed'
		  AND json_extract(payload_json, '$.run_id') = ?
		  AND json_extract(payload_json, '$.cluster_key') = ?
		  AND json_extract(payload_json, '$.decision') IN ('accept', 'reject')`,
		input.RunID, input.ClusterKey,
	).Scan(&finalized); err != nil {
		return GeneralizationResult{}, err
	}
	if finalized > 0 {
		return GeneralizationResult{}, errors.New("cluster proposal already has a final review")
	}
	operationID := identity.New("clusterreview")
	eventID, err := s.appendEvent(tx, "generalization_proposal_reviewed", operationID, payload)
	if err != nil {
		return GeneralizationResult{}, err
	}
	result := GeneralizationResult{EventID: eventID}
	if input.Decision == "accept" {
		for _, versionID := range input.SupportingLessonVersions {
			var count int
			if err := tx.QueryRow(`
				SELECT COUNT(*) FROM lesson_projection WHERE lesson_version_id = ?`,
				versionID,
			).Scan(&count); err != nil {
				return GeneralizationResult{}, err
			}
			if count == 0 {
				return GeneralizationResult{}, fmt.Errorf("supporting lesson %s does not exist", versionID)
			}
		}
		result.LessonID = identity.New("lesson")
		result.LessonVersionID = identity.New("lessonv")
		lessonEventID, err := s.appendEvent(tx, "lesson_generalized", operationID, map[string]any{
			"lesson_id":                  result.LessonID,
			"lesson_version_id":          result.LessonVersionID,
			"signature":                  document.Signature,
			"title":                      document.Title,
			"rule":                       document.Rule,
			"prevention":                 document.Prevention,
			"verification":               document.Verification,
			"applicability":              document.Applicability,
			"counterexamples":            document.Counterexamples,
			"cause_layer":                document.CauseLayer,
			"failure_mode":               document.FailureMode,
			"component":                  document.Component,
			"supporting_lesson_versions": input.SupportingLessonVersions,
			"state":                      "active",
		})
		if err != nil {
			return GeneralizationResult{}, err
		}
		now := time.Now().UTC().Format(time.RFC3339Nano)
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO lesson_projection(
				lesson_id, lesson_version_id, signature, title, rule, prevention,
				verification, applicability, counterexamples, cause_layer,
				failure_mode, component, document, state, created_at, source_event_id
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)`,
			result.LessonID, result.LessonVersionID, document.Signature,
			document.Title, document.Rule, document.Prevention, document.Verification,
			document.Applicability, document.Counterexamples, document.CauseLayer,
			document.FailureMode, document.Component, document.Document, now, lessonEventID,
		); err != nil {
			return GeneralizationResult{}, err
		}
		for _, versionID := range input.SupportingLessonVersions {
			supersededEventID, err := s.appendEvent(tx, "lesson_superseded", operationID, map[string]any{
				"lesson_version_id":                versionID,
				"representative_lesson_version_id": result.LessonVersionID,
				"reason":                           "accepted_generalization",
			})
			if err != nil {
				return GeneralizationResult{}, err
			}
			if _, err := tx.ExecContext(ctx, `
				UPDATE lesson_lifecycle_projection
				SET state = 'superseded', representative_lesson_version_id = ?,
				    last_outcome = 'superseded', updated_at = ?, source_event_id = ?
				WHERE lesson_version_id = ?`,
				result.LessonVersionID, now, supersededEventID, versionID,
			); err != nil {
				return GeneralizationResult{}, err
			}
		}
	}
	result.RetrievalRevision, err = retrievalRevisionTx(ctx, tx)
	if err != nil {
		return GeneralizationResult{}, err
	}
	if err := tx.Commit(); err != nil {
		return GeneralizationResult{}, err
	}
	return result, nil
}

func sameStringSet(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	counts := map[string]int{}
	for _, value := range left {
		counts[value]++
	}
	for _, value := range right {
		counts[value]--
	}
	for _, count := range counts {
		if count != 0 {
			return false
		}
	}
	return true
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
	telemetry RecallTelemetry,
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
			"relevance_score":   candidate.RelevanceScore,
			"reasons":           candidate.Reasons,
			"selected":          candidate.Selected,
		}); err != nil {
			return "", err
		}
	}
	if _, err := s.appendEvent(tx, "recall_completed", operationID, map[string]any{
		"attempt_id":                attemptID,
		"retrieved_count":           telemetry.RetrievedCount,
		"returned_count":            len(candidates),
		"filtered_below_threshold":  telemetry.FilteredBelowThreshold,
		"collapsed_by_cluster":      telemetry.CollapsedByCluster,
		"trimmed_by_adaptive_limit": telemetry.TrimmedByAdaptiveLimit,
		"applied_top_k":             telemetry.AppliedTopK,
		"applied_min_relevance":     telemetry.AppliedMinRelevance,
		"abstention_reason":         telemetry.AbstentionReason,
		"index_sync_latency_ms":     telemetry.IndexSyncLatencyMS,
		"search_latency_ms":         telemetry.SearchLatencyMS,
		"hydration_latency_ms":      telemetry.HydrationLatencyMS,
		"request_bytes":             telemetry.RequestBytes,
		"response_bytes":            telemetry.ResponseBytes,
	}); err != nil {
		return "", err
	}
	if err := tx.Commit(); err != nil {
		return "", err
	}
	return attemptID, nil
}

func (s *Store) RecallPerformance(ctx context.Context) (map[string]any, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT
			COALESCE(CAST(json_extract(payload_json, '$.retrieved_count') AS INTEGER), 0),
			COALESCE(CAST(json_extract(payload_json, '$.returned_count') AS INTEGER), 0),
			COALESCE(CAST(json_extract(payload_json, '$.filtered_below_threshold') AS INTEGER), 0),
			COALESCE(CAST(json_extract(payload_json, '$.collapsed_by_cluster') AS INTEGER), 0),
			COALESCE(CAST(json_extract(payload_json, '$.trimmed_by_adaptive_limit') AS INTEGER), 0),
			COALESCE(CAST(json_extract(payload_json, '$.applied_min_relevance') AS REAL), 0),
			COALESCE(CAST(json_extract(payload_json, '$.index_sync_latency_ms') AS INTEGER), 0),
			COALESCE(CAST(json_extract(payload_json, '$.search_latency_ms') AS INTEGER), 0),
			COALESCE(CAST(json_extract(payload_json, '$.hydration_latency_ms') AS INTEGER), 0),
			COALESCE(CAST(json_extract(payload_json, '$.request_bytes') AS INTEGER), 0),
			COALESCE(CAST(json_extract(payload_json, '$.response_bytes') AS INTEGER), 0)
		FROM event_log WHERE event_type = 'recall_completed' ORDER BY sequence`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var attempts, abstentions, requestBytes, responseBytes int64
	var retrieved, returned, filtered, collapsed, adaptiveTrimmed int64
	var thresholdTotal float64
	latencies := make([]int64, 0)
	indexLatencies := make([]int64, 0)
	searchLatencies := make([]int64, 0)
	hydrationLatencies := make([]int64, 0)
	for rows.Next() {
		var rowRetrieved, rowReturned, rowFiltered, rowCollapsed, rowAdaptiveTrimmed int64
		var threshold float64
		var indexLatency, searchLatency, hydrationLatency, requestSize, responseSize int64
		if err := rows.Scan(
			&rowRetrieved, &rowReturned, &rowFiltered, &rowCollapsed, &rowAdaptiveTrimmed,
			&threshold,
			&indexLatency, &searchLatency, &hydrationLatency, &requestSize, &responseSize,
		); err != nil {
			return nil, err
		}
		attempts++
		if rowReturned == 0 {
			abstentions++
		}
		retrieved += rowRetrieved
		returned += rowReturned
		filtered += rowFiltered
		collapsed += rowCollapsed
		adaptiveTrimmed += rowAdaptiveTrimmed
		thresholdTotal += threshold
		requestBytes += requestSize
		responseBytes += responseSize
		latencies = append(latencies, indexLatency+searchLatency+hydrationLatency)
		indexLatencies = append(indexLatencies, indexLatency)
		searchLatencies = append(searchLatencies, searchLatency)
		hydrationLatencies = append(hydrationLatencies, hydrationLatency)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	average := func(total int64) float64 {
		if attempts == 0 {
			return 0
		}
		return float64(total) / float64(attempts)
	}
	averageThreshold := 0.0
	zeroResultRate := 0.0
	if attempts > 0 {
		averageThreshold = thresholdTotal / float64(attempts)
		zeroResultRate = float64(abstentions) / float64(attempts)
	}
	return map[string]any{
		"attempts":                          attempts,
		"abstentions":                       abstentions,
		"zero_result_rate":                  zeroResultRate,
		"average_retrieved":                 average(retrieved),
		"average_returned":                  average(returned),
		"average_filtered_below_threshold":  average(filtered),
		"average_collapsed_by_cluster":      average(collapsed),
		"average_trimmed_by_adaptive_limit": average(adaptiveTrimmed),
		"average_min_relevance":             averageThreshold,
		"request_bytes":                     requestBytes,
		"response_bytes":                    responseBytes,
		"total_io_bytes":                    requestBytes + responseBytes,
		"latency_ms":                        latencyDistribution(latencies),
		"phase_latency_ms": map[string]map[string]int64{
			"index_sync": latencyDistribution(indexLatencies),
			"search":     latencyDistribution(searchLatencies),
			"hydration":  latencyDistribution(hydrationLatencies),
		},
	}, nil
}

func latencyDistribution(values []int64) map[string]int64 {
	if len(values) == 0 {
		return map[string]int64{"p50": 0, "p95": 0, "max": 0}
	}
	sort.Slice(values, func(i, j int) bool { return values[i] < values[j] })
	percentile := func(numerator int) int64 {
		index := (len(values)*numerator + 99) / 100
		if index < 1 {
			index = 1
		}
		return values[index-1]
	}
	return map[string]int64{
		"p50": percentile(50),
		"p95": percentile(95),
		"max": values[len(values)-1],
	}
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
	return scanLessons(rows)
}

func scanLessons(rows *sql.Rows) ([]model.LessonDocument, error) {
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
	revision, err := retrievalRevisionTx(ctx, tx)
	if err != nil {
		return nil, 0, "", err
	}
	lessons, err := listSearchableLessons(ctx, tx)
	if err != nil {
		return nil, 0, "", err
	}
	if err := tx.Commit(); err != nil {
		return nil, 0, "", err
	}
	return lessons, revision, lessonmanifest.Digest(lessons), nil
}

func listSearchableLessons(
	ctx context.Context,
	queryer lessonQueryer,
) ([]model.LessonDocument, error) {
	rows, err := queryer.QueryContext(ctx, `
		SELECT l.lesson_id, l.lesson_version_id, l.signature, l.title, l.rule,
		       l.prevention, l.verification, l.applicability, l.counterexamples,
		       l.cause_layer, l.failure_mode, l.component, l.document, l.created_at
		FROM lesson_projection l
		JOIN lesson_lifecycle_projection c USING(lesson_version_id)
		WHERE c.state NOT IN ('false_positive', 'superseded', 'stale')
		ORDER BY l.created_at, l.lesson_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanLessons(rows)
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

func (s *Store) RetrievalRepresentatives(ctx context.Context) (map[string]string, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT lesson_version_id, representative_lesson_version_id
		FROM lesson_lifecycle_projection
		WHERE state NOT IN ('false_positive', 'superseded', 'stale')`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]string{}
	for rows.Next() {
		var versionID, representative string
		if err := rows.Scan(&versionID, &representative); err != nil {
			return nil, err
		}
		result[versionID] = representative
	}
	return result, rows.Err()
}

func (s *Store) Counts(ctx context.Context) (map[string]int64, error) {
	result := map[string]int64{}
	for name, query := range map[string]string{
		"events":      "SELECT COUNT(*) FROM event_log",
		"incidents":   "SELECT COUNT(*) FROM incident_projection",
		"lessons":     "SELECT COUNT(*) FROM lesson_projection",
		"captures":    "SELECT COUNT(*) FROM event_log WHERE event_type = 'capture_evaluated'",
		"corrections": "SELECT COUNT(*) FROM event_log WHERE event_type = 'capture_evaluated' AND COALESCE(json_extract(payload_json, '$.correction_of_capture_event_id'), '') <> ''",
		"recalls":     "SELECT COUNT(*) FROM event_log WHERE event_type = 'recall_attempted'",
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
		"lesson": {},
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
	rows, err := s.db.QueryContext(ctx, `
		SELECT json_extract(payload_json, '$.target_type'),
		       json_extract(payload_json, '$.outcome'), COUNT(*)
		FROM event_log
		WHERE event_type = 'memory_outcome_observed'
		GROUP BY json_extract(payload_json, '$.target_type'),
		         json_extract(payload_json, '$.outcome')
		ORDER BY 1, 2`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var category, outcome string
		var count int64
		if err := rows.Scan(&category, &outcome, &count); err != nil {
			return nil, err
		}
		if result[category] == nil {
			result[category] = map[string]int64{}
		}
		result[category][outcome] += count
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return result, nil
}

func (s *Store) LessonLifecycleCounts(ctx context.Context) (map[string]int64, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT state, COUNT(*)
		FROM lesson_lifecycle_projection
		GROUP BY state ORDER BY state`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]int64{
		"active": 0, "proposed": 0, "false_positive": 0,
		"stale": 0, "superseded": 0,
	}
	for rows.Next() {
		var state string
		var count int64
		if err := rows.Scan(&state, &count); err != nil {
			return nil, err
		}
		result[state] = count
	}
	return result, rows.Err()
}

func (s *Store) OutcomeCoverage(ctx context.Context) (map[string]map[string]any, error) {
	queries := map[string][2]string{
		"recall": {
			"SELECT COUNT(*) FROM event_log WHERE event_type = 'recall_attempted'",
			`SELECT COUNT(DISTINCT target_id) FROM (
				SELECT json_extract(payload_json, '$.target_id') AS target_id
				FROM event_log WHERE event_type = 'memory_outcome_observed'
				  AND json_extract(payload_json, '$.target_type') = 'recall'
				UNION
				SELECT json_extract(payload_json, '$.recall_attempt_id')
				FROM event_log WHERE event_type = 'recall_outcome_observed'
			) WHERE target_id IS NOT NULL`,
		},
		"repair": {
			"SELECT COUNT(*) FROM event_log WHERE event_type = 'repair_recommended'",
			`SELECT COUNT(DISTINCT target_id) FROM (
				SELECT json_extract(payload_json, '$.target_id') AS target_id
				FROM event_log WHERE event_type = 'memory_outcome_observed'
				  AND json_extract(payload_json, '$.target_type') = 'repair'
				UNION
				SELECT json_extract(payload_json, '$.repair_recommendation_id')
				FROM event_log WHERE event_type = 'repair_outcome_observed'
			) WHERE target_id IS NOT NULL`,
		},
		"lesson": {
			"SELECT COUNT(*) FROM lesson_projection",
			`SELECT COUNT(DISTINCT json_extract(payload_json, '$.target_id'))
			 FROM event_log WHERE event_type = 'memory_outcome_observed'
			   AND json_extract(payload_json, '$.target_type') = 'lesson'`,
		},
	}
	result := map[string]map[string]any{}
	for category, pair := range queries {
		var eligible, observed int64
		if err := s.db.QueryRowContext(ctx, pair[0]).Scan(&eligible); err != nil {
			return nil, err
		}
		if err := s.db.QueryRowContext(ctx, pair[1]).Scan(&observed); err != nil {
			return nil, err
		}
		rate := 0.0
		if eligible > 0 {
			rate = float64(observed) / float64(eligible)
		}
		result[category] = map[string]any{
			"eligible": eligible, "observed": observed, "coverage_rate": rate,
		}
	}
	return result, nil
}

func (s *Store) HarnessUsage(ctx context.Context) (map[string]map[string]any, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT COALESCE(NULLIF(source_harness, ''), 'unknown'),
		       MIN(occurred_at), MAX(occurred_at), COUNT(*),
		       SUM(CASE WHEN event_type = 'capture_evaluated' THEN 1 ELSE 0 END),
		       SUM(CASE WHEN event_type = 'recall_attempted' THEN 1 ELSE 0 END),
		       SUM(CASE WHEN event_type IN (
		           'memory_outcome_observed', 'recall_outcome_observed',
		           'repair_outcome_observed') THEN 1 ELSE 0 END)
		FROM event_log
		GROUP BY COALESCE(NULLIF(source_harness, ''), 'unknown')
		ORDER BY 1`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]map[string]any{}
	for rows.Next() {
		var harness, firstUsed, lastUsed string
		var events, captures, recalls, outcomes int64
		if err := rows.Scan(
			&harness, &firstUsed, &lastUsed, &events, &captures, &recalls, &outcomes,
		); err != nil {
			return nil, err
		}
		result[harness] = map[string]any{
			"first_used_at": firstUsed, "last_used_at": lastUsed, "events": events,
			"captures": captures, "recalls": recalls, "outcomes": outcomes,
		}
	}
	return result, rows.Err()
}

func (s *Store) GeneralizationBacklog(ctx context.Context) (map[string]any, error) {
	var clusterPending, onlineSuggestions int64
	var oldestCluster, oldestOnline sql.NullString
	if err := s.db.QueryRowContext(ctx, `
		SELECT COUNT(*), MIN(proposal.occurred_at)
		FROM event_log proposal
		WHERE proposal.event_type = 'generalization_proposal_created'
		  AND NOT EXISTS (
		      SELECT 1 FROM event_log review
		      WHERE review.event_type = 'generalization_proposal_reviewed'
		        AND json_extract(review.payload_json, '$.run_id') = json_extract(proposal.payload_json, '$.run_id')
		        AND json_extract(review.payload_json, '$.cluster_key') = json_extract(proposal.payload_json, '$.cluster_key')
		        AND json_extract(review.payload_json, '$.decision') IN ('accept', 'reject')
		  )`).Scan(&clusterPending, &oldestCluster); err != nil {
		return nil, err
	}
	if err := s.db.QueryRowContext(ctx, `
		SELECT COUNT(*), MIN(event.occurred_at)
		FROM event_log event
		JOIN lesson_lifecycle_projection lifecycle
		  ON lifecycle.lesson_version_id = json_extract(event.payload_json, '$.new_lesson_version_id')
		WHERE event.event_type = 'generalization_review_proposed'
		  AND lifecycle.state NOT IN ('false_positive', 'stale', 'superseded')`).Scan(
		&onlineSuggestions, &oldestOnline,
	); err != nil {
		return nil, err
	}
	oldestAt := ""
	for _, candidate := range []sql.NullString{oldestCluster, oldestOnline} {
		if candidate.Valid && (oldestAt == "" || candidate.String < oldestAt) {
			oldestAt = candidate.String
		}
	}
	return map[string]any{
		"pending":                   clusterPending + onlineSuggestions,
		"cluster_proposals_pending": clusterPending,
		"online_suggestions":        onlineSuggestions,
		"oldest_pending_at":         oldestAt,
	}, nil
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
	lessonRevision, err := s.LessonRevision(ctx)
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
		"lesson_revision":        lessonRevision,
		"retrieval_revision":     revision,
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
	return s.appendEventWithID(tx, identity.New("event"), eventType, operationID, payload)
}

func (s *Store) appendEventWithID(
	tx *sql.Tx,
	eventID string,
	eventType string,
	operationID string,
	payload any,
) (string, error) {
	data, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
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
		string(input.Cause.Layer),
		string(input.Cause.FailureMode),
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
