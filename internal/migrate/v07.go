package migrate

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	_ "modernc.org/sqlite"

	"github.com/CongBao/failure-memory/internal/model"
)

const legacyQuery = `
SELECT
	c.id, c.created_at, c.source_harness, c.summary, c.classification, c.decision,
	c.reason_codes_json, c.expectation_source, COALESCE(c.expectation_evidence, ''),
	COALESCE(c.failure_portion_summary, ''),
	COALESCE(i.id, ''), COALESCE(i.outcome_summary, ''),
	COALESCE(i.expected_invariant, ''), COALESCE(i.controllable_cause, ''),
	COALESCE(i.material_impact, ''), COALESCE(i.recurrence_risk, ''),
	COALESCE(v.lesson_id, ''), COALESCE(v.id, ''), COALESCE(v.signature, ''),
	COALESCE(v.title, ''), COALESCE(v.rule, ''), COALESCE(v.prevention_action, ''),
	COALESCE(v.verification_action, ''), COALESCE(v.applicability, ''),
	COALESCE(v.counterexamples, ''), COALESCE(v.lifecycle_state, ''),
	COALESCE(cf.layer, ''), COALESCE(cf.failure_mode, ''),
	COALESCE(cf.component_reference, ''), COALESCE(cf.evidence_summary, ''),
	COALESCE(cf.confidence, ''),
	COALESCE(rr.id, ''), COALESCE(rr.target_layer, ''),
	COALESCE(rr.target_reference, ''), COALESCE(rr.recommended_change, ''),
	COALESCE(rr.verification_action, ''), COALESCE(rr.rationale, ''),
	COALESCE(rr.confidence, '')
FROM capture_attempt AS c
LEFT JOIN incident AS i ON i.capture_attempt_id = c.id
LEFT JOIN incident_lesson_relation AS rel ON rel.incident_id = i.id
LEFT JOIN lesson_version AS v ON v.id = rel.lesson_version_id
LEFT JOIN failure_causal_assessment AS ca ON ca.capture_attempt_id = c.id
LEFT JOIN failure_causal_factor AS cf
	ON cf.assessment_id = ca.id AND cf.role = 'primary'
LEFT JOIN failure_repair_recommendation AS rr
	ON rr.assessment_id = ca.id AND rr.ordinal = 1
ORDER BY c.created_at, c.id`

func ReadV07(ctx context.Context, source string) (model.LegacyImport, error) {
	absolute, err := filepath.Abs(source)
	if err != nil {
		return model.LegacyImport{}, err
	}
	info, err := os.Lstat(absolute)
	if err != nil {
		return model.LegacyImport{}, err
	}
	if !info.Mode().IsRegular() {
		return model.LegacyImport{}, errors.New("legacy source must be a regular SQLite file")
	}
	digest, err := fileSHA256(absolute)
	if err != nil {
		return model.LegacyImport{}, err
	}
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(absolute)+"?mode=ro")
	if err != nil {
		return model.LegacyImport{}, err
	}
	defer db.Close()
	var version int
	if err := db.QueryRowContext(ctx, `
		SELECT MAX(version) FROM schema_migration`).Scan(&version); err != nil {
		return model.LegacyImport{}, fmt.Errorf("not a supported v0 failure-memory store: %w", err)
	}
	if version < 3 || version > 8 {
		return model.LegacyImport{}, fmt.Errorf("unsupported legacy schema version %d", version)
	}
	rows, err := db.QueryContext(ctx, legacyQuery)
	if err != nil {
		return model.LegacyImport{}, err
	}
	defer rows.Close()
	var captures []model.LegacyCapture
	for rows.Next() {
		var capture model.LegacyCapture
		if err := rows.Scan(
			&capture.CaptureID,
			&capture.CreatedAt,
			&capture.SourceHarness,
			&capture.Summary,
			&capture.Classification,
			&capture.Decision,
			&capture.ReasonCodesJSON,
			&capture.ExpectationSource,
			&capture.ExpectationEvidence,
			&capture.FailurePortion,
			&capture.IncidentID,
			&capture.Outcome,
			&capture.ExpectedInvariant,
			&capture.ControllableCause,
			&capture.MaterialImpact,
			&capture.RecurrenceRisk,
			&capture.LessonID,
			&capture.LessonVersionID,
			&capture.LessonSignature,
			&capture.LessonTitle,
			&capture.LessonRule,
			&capture.LessonPrevention,
			&capture.LessonVerification,
			&capture.LessonApplicability,
			&capture.LessonCounterexamples,
			&capture.LessonState,
			&capture.CauseLayer,
			&capture.FailureMode,
			&capture.Component,
			&capture.CauseEvidence,
			&capture.CauseConfidence,
			&capture.RepairID,
			&capture.RepairTargetLayer,
			&capture.RepairTarget,
			&capture.RecommendedChange,
			&capture.RepairVerification,
			&capture.RepairRationale,
			&capture.RepairConfidence,
		); err != nil {
			return model.LegacyImport{}, err
		}
		captures = append(captures, capture)
	}
	if err := rows.Err(); err != nil {
		return model.LegacyImport{}, err
	}
	content, err := json.Marshal(captures)
	if err != nil {
		return model.LegacyImport{}, err
	}
	contentDigest := sha256.Sum256(content)
	return model.LegacyImport{
		SourceIdentity: "failure-memory-v0-content:" + hex.EncodeToString(contentDigest[:]),
		SourceSHA256:   digest,
		Captures:       captures,
	}, nil
}

func fileSHA256(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}
