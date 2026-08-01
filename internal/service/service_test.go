package service

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"

	"github.com/CongBao/failure-memory/internal/model"
)

func TestGlobalFlowRejectsUpdatesReusesLessonsAndTracesRecall(t *testing.T) {
	root := t.TempDir()
	t.Setenv("FAILURE_MEMORY_HOME", root)
	t.Setenv("FAILURE_MEMORY_HARNESS", "codex")
	t.Setenv("FAILURE_MEMORY_SESSION_ID", "service-test")
	svc, err := Open("test")
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := svc.Close(); err != nil {
			t.Error(err)
		}
	}()

	rejected, err := svc.Remember(context.Background(), model.RememberInput{
		Summary:        "The user added a new output requirement.",
		Classification: model.RequirementUpdate,
	})
	if err != nil {
		t.Fatal(err)
	}
	if rejected.Status != model.NotFailure || rejected.LessonID != "" {
		t.Fatalf("unexpected rejected result: %#v", rejected)
	}

	first, err := svc.Remember(context.Background(), testFailure())
	if err != nil {
		t.Fatal(err)
	}
	second, err := svc.Remember(context.Background(), testFailure())
	if err != nil {
		t.Fatal(err)
	}
	if first.LessonID == "" || second.LessonID != first.LessonID {
		t.Fatalf("exact lesson was not reused: first=%#v second=%#v", first, second)
	}
	if first.Deduplication != "distinct" || second.Deduplication != "exact_reuse" {
		t.Fatalf("unexpected deduplication: %q %q", first.Deduplication, second.Deduplication)
	}

	recalled, err := svc.Recall(context.Background(), model.RecallInput{
		Text:              "Plan a safe schema migration.",
		ExpectedInvariant: "Run the compatibility preflight before schema edits.",
		Component:         "schema migration workflow",
		Mode:              "hybrid",
		TopK:              3,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(recalled.Lessons) != 1 || recalled.Lessons[0].LessonID != first.LessonID {
		t.Fatalf("unexpected recall: %#v", recalled)
	}
	if recalled.AttemptID == "" {
		t.Fatal("recall attempt was not traced")
	}
	recallOutcome, err := svc.RecordRecallOutcome(context.Background(), model.RecallOutcomeInput{
		RecallAttemptID: recalled.AttemptID,
		LessonVersionID: recalled.Lessons[0].LessonVersionID,
		Outcome:         "useful",
		Confidence:      0.9,
	})
	if err != nil || recallOutcome.EventID == "" {
		t.Fatalf("recall outcome: result=%#v err=%v", recallOutcome, err)
	}
	repairOutcome, err := svc.RecordRepairOutcome(context.Background(), model.RepairOutcomeInput{
		RepairRecommendationID: first.RepairID,
		Outcome:                "verified_effective",
		Confidence:             "high",
	})
	if err != nil || repairOutcome.EventID == "" {
		t.Fatalf("repair outcome: result=%#v err=%v", repairOutcome, err)
	}
	clusters, err := svc.ProposeClusters(context.Background(), 2)
	if err != nil {
		t.Fatal(err)
	}
	if clusters.RunID == "" || clusters.LessonCount != 1 {
		t.Fatalf("cluster result = %#v", clusters)
	}

	doctor, err := svc.Doctor(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if doctor["integrity_check"] != "ok" {
		t.Fatalf("doctor = %#v", doctor)
	}

	db, err := sql.Open("sqlite", filepath.Join(
		root, "adapters", "event-store", "sqlite", "v1", "events.sqlite3",
	))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec("DELETE FROM event_log"); err == nil {
		t.Fatal("append-only event log accepted a delete")
	}
	if _, err := db.Exec("DELETE FROM lesson_projection"); err == nil {
		t.Fatal("append-only lesson projection accepted a delete")
	}
}

func TestRelatedFailureIsRetainedAndQueuedForGeneralization(t *testing.T) {
	t.Setenv("FAILURE_MEMORY_HOME", t.TempDir())
	svc, err := Open("test")
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()
	first, err := svc.Remember(context.Background(), testFailure())
	if err != nil {
		t.Fatal(err)
	}
	related := testFailure()
	related.Summary = "The agent skipped compatibility checks for a new migration."
	related.Expectation.Invariant = "Validate compatibility before writing a migration."
	related.Lesson.Rule = "Every persisted migration needs a compatibility check before edits."
	second, err := svc.Remember(context.Background(), related)
	if err != nil {
		t.Fatal(err)
	}
	if second.LessonID == first.LessonID {
		t.Fatal("non-identical lessons were silently merged")
	}
	if second.Deduplication != "related_pending_generalization" {
		t.Fatalf("deduplication = %q", second.Deduplication)
	}
	if second.GeneralizationHint == "" {
		t.Fatal("related lesson did not return a review hint")
	}
}

func TestRecallRequiresBoundedEvidence(t *testing.T) {
	t.Setenv("FAILURE_MEMORY_HOME", t.TempDir())
	svc, err := Open("test")
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()
	_, err = svc.Recall(context.Background(), model.RecallInput{Text: "broad query"})
	if err == nil {
		t.Fatal("recall accepted a query without a discriminator")
	}
}

func TestRecallRepairsAnIncompleteDerivedIndex(t *testing.T) {
	root := t.TempDir()
	t.Setenv("FAILURE_MEMORY_HOME", root)
	svc, err := Open("test")
	if err != nil {
		t.Fatal(err)
	}
	recorded, err := svc.Remember(context.Background(), testFailure())
	if err != nil {
		t.Fatal(err)
	}
	if err := svc.Close(); err != nil {
		t.Fatal(err)
	}

	indexPath := filepath.Join(
		root, "adapters", "retrieval", "sqlite-vec", "v1", "index.sqlite3",
	)
	db, err := sql.Open("sqlite", indexPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("DELETE FROM lesson_vec"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	svc, err = Open("test")
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()
	recalled, err := svc.Recall(context.Background(), model.RecallInput{
		Text:      "safe schema migration",
		Component: "schema migration workflow",
		TopK:      3,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(recalled.Lessons) != 1 || recalled.Lessons[0].LessonID != recorded.LessonID {
		t.Fatalf("recalled lessons after repair = %#v", recalled.Lessons)
	}
	doctor, err := svc.Doctor(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if doctor["retrieval_index_complete"] != true {
		t.Fatalf("doctor after repair = %#v", doctor)
	}
}

func TestDoctorRepairsSameCountManifestDrift(t *testing.T) {
	root := t.TempDir()
	t.Setenv("FAILURE_MEMORY_HOME", root)
	svc, err := Open("test")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Remember(context.Background(), testFailure()); err != nil {
		t.Fatal(err)
	}
	if err := svc.Close(); err != nil {
		t.Fatal(err)
	}

	indexPath := filepath.Join(
		root, "adapters", "retrieval", "sqlite-vec", "v1", "index.sqlite3",
	)
	db, err := sql.Open("sqlite", indexPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE lesson_document SET rule = 'tampered'"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	svc, err = Open("test")
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()
	doctor, err := svc.Doctor(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if doctor["retrieval_index_complete"] != true {
		t.Fatalf("doctor did not repair manifest drift: %#v", doctor)
	}
	if doctor["retrieval_sync_state"] != "repaired" {
		t.Fatalf("retrieval sync state = %#v", doctor["retrieval_sync_state"])
	}
}

func testFailure() model.RememberInput {
	return model.RememberInput{
		Summary:        "The agent skipped an established compatibility preflight.",
		Classification: model.RealFailure,
		Expectation: &model.ExpectationEvidence{
			Invariant: "Run the compatibility preflight before schema edits.",
			Source:    "loaded_skill",
			Evidence:  "The skill stated this before implementation.",
		},
		Observed: &model.ObservedEvidence{
			Outcome: "The schema edit was made without the preflight.",
			Impact:  "The migration could write incompatible rows.",
		},
		Cause: &model.CauseEvidence{
			Layer:             "skill_instruction",
			FailureMode:       "ignored",
			Component:         "schema migration workflow",
			Evidence:          "No preflight result existed before the edit.",
			RecommendedChange: "Make the preflight a completion gate.",
			Verification:      "Replay with the preflight before edits.",
			Confidence:        "high",
		},
		Lesson: &model.LessonEvidence{
			Title:        "Run schema preflight first",
			Rule:         "Do not edit persisted schema before its compatibility preflight passes.",
			Prevention:   "Run the preflight before the first edit.",
			Verification: "Retain the passing preflight result.",
		},
	}
}
