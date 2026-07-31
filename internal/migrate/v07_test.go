package migrate

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"
)

func TestReadV07BuildsStableReviewedImport(t *testing.T) {
	path := filepath.Join(t.TempDir(), "legacy.sqlite3")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	schema := `
	CREATE TABLE schema_migration(version INTEGER);
	INSERT INTO schema_migration VALUES (8);
	CREATE TABLE capture_attempt(
		id TEXT, created_at TEXT, source_harness TEXT, summary TEXT,
		classification TEXT, decision TEXT, reason_codes_json TEXT,
		expectation_source TEXT, expectation_evidence TEXT,
		failure_portion_summary TEXT
	);
	CREATE TABLE incident(
		id TEXT, capture_attempt_id TEXT, outcome_summary TEXT,
		expected_invariant TEXT, controllable_cause TEXT,
		material_impact TEXT, recurrence_risk TEXT
	);
	CREATE TABLE incident_lesson_relation(
		incident_id TEXT, lesson_id TEXT, lesson_version_id TEXT
	);
	CREATE TABLE lesson_version(
		id TEXT, lesson_id TEXT, signature TEXT, title TEXT, rule TEXT,
		prevention_action TEXT, verification_action TEXT, applicability TEXT,
		counterexamples TEXT, lifecycle_state TEXT
	);
	CREATE TABLE failure_causal_assessment(id TEXT, capture_attempt_id TEXT);
	CREATE TABLE failure_causal_factor(
		assessment_id TEXT, role TEXT, layer TEXT, failure_mode TEXT,
		component_reference TEXT, evidence_summary TEXT, confidence TEXT
	);
	CREATE TABLE failure_repair_recommendation(
		id TEXT, assessment_id TEXT, ordinal INTEGER, target_layer TEXT,
		target_reference TEXT, recommended_change TEXT,
		verification_action TEXT, rationale TEXT, confidence TEXT
	);
	INSERT INTO capture_attempt VALUES (
		'cap-1','2026-07-31T00:00:00Z','codex','preflight skipped',
		'real_failure','accept','["real_failure_criteria_met"]',
		'repository_contract','existing rule',NULL
	);
	INSERT INTO incident VALUES (
		'inc-1','cap-1','incompatible write','run preflight','gate absent',
		'bad persisted state','recurs'
	);
	INSERT INTO incident_lesson_relation VALUES ('inc-1','lesson-1','lessonv-1');
	INSERT INTO lesson_version VALUES (
		'lessonv-1','lesson-1','signature-1','run preflight',
		'preflight before writes','add gate','test gate',
		'migrations','read-only operations','proposed'
	);
	INSERT INTO failure_causal_assessment VALUES ('cause-1','cap-1');
	INSERT INTO failure_causal_factor VALUES (
		'cause-1','primary','application_logic','missing',
		'write gate','no preflight result','high'
	);
	INSERT INTO failure_repair_recommendation VALUES (
		'repair-1','cause-1',1,'application_logic','write gate',
		'add preflight gate','test incompatible write','gate was absent','high'
	);
	INSERT INTO capture_attempt VALUES (
		'cap-2','2026-07-31T00:01:00Z','codex','YAML requested later',
		'requirement_update','reject','["not_preexisting_requirement"]',
		'none',NULL,NULL
	);`
	if _, err := db.Exec(schema); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	first, err := ReadV07(context.Background(), path)
	if err != nil {
		t.Fatal(err)
	}
	second, err := ReadV07(context.Background(), path)
	if err != nil {
		t.Fatal(err)
	}
	if first.SourceIdentity == "" || first.SourceIdentity != second.SourceIdentity {
		t.Fatalf("unstable source identity: %q %q", first.SourceIdentity, second.SourceIdentity)
	}
	if len(first.Captures) != 2 {
		t.Fatalf("capture count = %d", len(first.Captures))
	}
	if got := first.Captures[0]; got.LessonID != "lesson-1" ||
		got.RepairID != "repair-1" || got.CauseLayer != "application_logic" {
		t.Fatalf("accepted capture was not reconstructed: %#v", got)
	}
	if got := first.Captures[1]; got.Decision != "reject" || got.LessonID != "" {
		t.Fatalf("rejected capture was not preserved: %#v", got)
	}
}
