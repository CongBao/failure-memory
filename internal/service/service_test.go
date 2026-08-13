package service

import (
	"context"
	"database/sql"
	"path/filepath"
	"sync"
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
	recallOutcome, err := svc.ReportOutcome(context.Background(), model.MemoryOutcomeInput{
		TargetType:       "recall",
		TargetID:         recalled.AttemptID,
		LessonVersionIDs: []string{recalled.Lessons[0].LessonVersionID},
		Outcome:          "applied",
		Confidence:       0.9,
		EvidenceCode:     "test_applied",
	})
	if err != nil || recallOutcome.EventID == "" {
		t.Fatalf("recall outcome: result=%#v err=%v", recallOutcome, err)
	}
	repairOutcome, err := svc.ReportOutcome(context.Background(), model.MemoryOutcomeInput{
		TargetType:   "repair",
		TargetID:     first.RepairID,
		Outcome:      "verified_effective",
		Confidence:   0.9,
		EvidenceCode: "test_verified_effective",
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

func TestReportOutcomeRetainsButStopsRecallingFalsePositiveLesson(t *testing.T) {
	t.Setenv("FAILURE_MEMORY_HOME", t.TempDir())
	svc, err := Open("test")
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()
	recorded, err := svc.Remember(context.Background(), testFailure())
	if err != nil {
		t.Fatal(err)
	}

	first, err := svc.ReportOutcome(context.Background(), model.MemoryOutcomeInput{
		TargetType:   "lesson",
		TargetID:     recorded.LessonVersionID,
		Outcome:      "false_positive",
		EvidenceCode: "user_confirmed_requirement_change",
	})
	if err != nil {
		t.Fatal(err)
	}
	second, err := svc.ReportOutcome(context.Background(), model.MemoryOutcomeInput{
		TargetType:   "lesson",
		TargetID:     recorded.LessonVersionID,
		Outcome:      "false_positive",
		EvidenceCode: "user_confirmed_requirement_change",
	})
	if err != nil {
		t.Fatal(err)
	}
	if first.EventID == "" || !second.Duplicate || second.EventID != first.EventID {
		t.Fatalf("outcome idempotency: first=%#v second=%#v", first, second)
	}
	if first.LessonVersionID != recorded.LessonVersionID || first.LessonID != recorded.LessonID {
		t.Fatalf("lesson outcome identity = %#v", first)
	}

	recalled, err := svc.Recall(context.Background(), model.RecallInput{
		Text:         "schema migration workflow",
		Component:    "schema migration workflow",
		Mode:         "hybrid",
		TopK:         3,
		MinRelevance: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(recalled.Lessons) != 0 {
		t.Fatalf("false-positive lesson was recalled: %#v", recalled.Lessons)
	}
	metrics, err := svc.Metrics(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if metrics["counts"].(map[string]int64)["lessons"] != 1 {
		t.Fatalf("false-positive lesson was not retained: %#v", metrics)
	}
	outcomes := metrics["outcome_counts"].(map[string]map[string]int64)
	if outcomes["lesson"]["false_positive"] != 1 {
		t.Fatalf("false-positive outcome metrics = %#v", outcomes)
	}
}

func TestMetricsReportsRecallCostAndRelevanceDecisions(t *testing.T) {
	t.Setenv("FAILURE_MEMORY_HOME", t.TempDir())
	svc, err := Open("test")
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()
	if _, err := svc.Remember(context.Background(), testFailure()); err != nil {
		t.Fatal(err)
	}
	var firstAttempt string
	for index, input := range []model.RecallInput{
		{
			Text:         "Do not edit persisted schema before its compatibility preflight passes.",
			MinRelevance: 1,
		},
		{Text: "quarterly catering menu", MinRelevance: 0.99},
	} {
		recalled, err := svc.Recall(context.Background(), input)
		if err != nil {
			t.Fatal(err)
		}
		if index == 0 {
			firstAttempt = recalled.AttemptID
		}
	}
	if _, err := svc.ReportOutcome(context.Background(), model.MemoryOutcomeInput{
		TargetType: "recall", TargetID: firstAttempt, Outcome: "applied",
		EvidenceCode: "test_applied",
	}); err != nil {
		t.Fatal(err)
	}
	metrics, err := svc.Metrics(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	performance := metrics["recall_performance"].(map[string]any)
	if performance["attempts"] != int64(2) || performance["abstentions"] != int64(1) {
		t.Fatalf("recall performance = %#v", performance)
	}
	if performance["average_returned"].(float64) != 0.5 ||
		performance["request_bytes"].(int64) <= 0 ||
		performance["response_bytes"].(int64) <= 0 {
		t.Fatalf("recall cost metrics = %#v", performance)
	}
	for _, key := range []string{
		"zero_result_rate", "average_retrieved", "average_filtered_below_threshold",
		"average_collapsed_by_cluster", "average_min_relevance", "latency_ms",
		"average_trimmed_by_adaptive_limit", "phase_latency_ms",
	} {
		if _, found := performance[key]; !found {
			t.Fatalf("recall performance lacks %q: %#v", key, performance)
		}
	}
	latency := performance["latency_ms"].(map[string]int64)
	if latency["p50"] < 0 || latency["p95"] < latency["p50"] ||
		latency["max"] < latency["p95"] {
		t.Fatalf("recall latency distribution = %#v", latency)
	}
	lifecycle := metrics["lesson_lifecycle"].(map[string]int64)
	if lifecycle["proposed"] != 1 {
		t.Fatalf("lesson lifecycle = %#v", lifecycle)
	}
	coverage := metrics["outcome_coverage"].(map[string]map[string]any)
	if coverage["recall"]["observed"].(int64) != 1 ||
		coverage["recall"]["eligible"].(int64) != 2 {
		t.Fatalf("outcome coverage = %#v", coverage)
	}
	harnesses := metrics["harness_usage"].(map[string]map[string]any)
	if harnesses["generic"]["recalls"].(int64) != 2 {
		t.Fatalf("harness usage = %#v", harnesses)
	}
	backlog := metrics["generalization_backlog"].(map[string]any)
	if backlog["pending"].(int64) != 0 {
		t.Fatalf("generalization backlog = %#v", backlog)
	}
}

func TestAcceptedGeneralizationRecallsParentAndRetainsChildren(t *testing.T) {
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
	related.Summary = "The compatibility validation was delayed until after a persisted edit."
	related.Lesson.Rule = "Persisted edits must wait for their compatibility validation."
	related.Lesson.Prevention = "Gate the first persisted edit on validation evidence."
	second, err := svc.Remember(context.Background(), related)
	if err != nil {
		t.Fatal(err)
	}
	clustered, err := svc.ProposeClusters(context.Background(), 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(clustered.Clusters) != 1 {
		t.Fatalf("clusters = %#v", clustered)
	}
	if _, err := svc.ReviewGeneralization(context.Background(), model.GeneralizationReviewInput{
		RunID: clustered.RunID, ClusterKey: "not-in-this-run", Decision: "defer",
		RationaleCode:            "test_invalid_cluster",
		SupportingLessonVersions: []string{first.LessonVersionID, second.LessonVersionID},
	}); err == nil {
		t.Fatal("review accepted a cluster that was not proposed by the run")
	}
	generalizedRule := "Never modify persisted state until its compatibility gate passes."
	reviewed, err := svc.ReviewGeneralization(context.Background(), model.GeneralizationReviewInput{
		RunID:                    clustered.RunID,
		ClusterKey:               clustered.Clusters[0].Key,
		Decision:                 "accept",
		RationaleCode:            "same_cause_and_prevention",
		SupportingLessonVersions: []string{first.LessonVersionID, second.LessonVersionID},
		GeneralizedLesson: &model.GeneralizedLessonInput{
			Title:           "Gate persisted changes on compatibility",
			Rule:            generalizedRule,
			Prevention:      "Require compatibility evidence before the first persisted edit.",
			Verification:    "Replay both incidents and observe the gate before mutation.",
			Applicability:   "Persisted state and schema changes.",
			Counterexamples: "Read-only analysis with no persisted mutation.",
			CauseLayer:      "test_evaluation_gap",
			FailureMode:     "insufficient_validation",
			Component:       "persisted compatibility gate",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if reviewed.LessonVersionID == "" {
		t.Fatalf("generalization result = %#v", reviewed)
	}
	recalled, err := svc.Recall(context.Background(), model.RecallInput{
		Text:         generalizedRule,
		TopK:         3,
		MinRelevance: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(recalled.Lessons) != 1 || recalled.Lessons[0].LessonVersionID != reviewed.LessonVersionID {
		t.Fatalf("generalized recall = %#v", recalled)
	}
	metrics, err := svc.Metrics(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if metrics["counts"].(map[string]int64)["lessons"] != 3 {
		t.Fatalf("child lessons were not retained: %#v", metrics)
	}
	if metrics["generalization_backlog"].(map[string]any)["pending"].(int64) != 0 {
		t.Fatalf("accepted generalization remained pending: %#v", metrics)
	}
	lifecycle := metrics["lesson_lifecycle"].(map[string]int64)
	if lifecycle["active"] != 1 || lifecycle["superseded"] != 2 {
		t.Fatalf("generalized lifecycle = %#v", lifecycle)
	}
	if _, err := svc.ReviewGeneralization(context.Background(), model.GeneralizationReviewInput{
		RunID: clustered.RunID, ClusterKey: clustered.Clusters[0].Key,
		Decision: "reject", RationaleCode: "duplicate_final_review",
		SupportingLessonVersions: []string{first.LessonVersionID, second.LessonVersionID},
	}); err == nil {
		t.Fatal("generalization accepted a second final review")
	}
}

func TestMemoryOutcomeIsIdempotentUnderConcurrentRetries(t *testing.T) {
	t.Setenv("FAILURE_MEMORY_HOME", t.TempDir())
	svc, err := Open("test")
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()
	if _, err := svc.Remember(context.Background(), testFailure()); err != nil {
		t.Fatal(err)
	}
	recalled, err := svc.Recall(context.Background(), model.RecallInput{
		Text: "Run compatibility checks before persisted schema edits.", MinRelevance: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := svc.ReportOutcome(context.Background(), model.MemoryOutcomeInput{
		TargetType: "recall", TargetID: recalled.AttemptID, Outcome: "applied",
		LessonVersionIDs: []string{"lessonv-not-returned"},
		EvidenceCode:     "invalid_candidate",
	}); err == nil {
		t.Fatal("recall outcome accepted a lesson that was not returned")
	}

	const workers = 8
	results := make(chan model.OutcomeResult, workers)
	errorsSeen := make(chan error, workers)
	var wait sync.WaitGroup
	for range workers {
		wait.Add(1)
		go func() {
			defer wait.Done()
			result, err := svc.ReportOutcome(context.Background(), model.MemoryOutcomeInput{
				TargetType: "recall", TargetID: recalled.AttemptID, Outcome: "applied",
				EvidenceCode: "same_retry", IdempotencyKey: "concurrent-retry-key",
			})
			if err != nil {
				errorsSeen <- err
				return
			}
			results <- result
		}()
	}
	wait.Wait()
	close(results)
	close(errorsSeen)
	for err := range errorsSeen {
		t.Error(err)
	}
	var eventID string
	for result := range results {
		if eventID == "" {
			eventID = result.EventID
		}
		if result.EventID != eventID {
			t.Fatalf("concurrent retry created multiple events: %q and %q", eventID, result.EventID)
		}
	}
	if t.Failed() {
		return
	}
	metrics, err := svc.Metrics(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if metrics["outcome_counts"].(map[string]map[string]int64)["recall"]["applied"] != 1 {
		t.Fatalf("outcomes were duplicated: %#v", metrics["outcome_counts"])
	}
	if _, err := svc.ReportOutcome(context.Background(), model.MemoryOutcomeInput{
		TargetType: "recall", TargetID: recalled.AttemptID, Outcome: "ignored",
		EvidenceCode: "same_retry", IdempotencyKey: "concurrent-retry-key",
	}); err == nil {
		t.Fatal("reused idempotency key accepted a different outcome")
	}
}

func TestRecallAcceptsCompactTextWithoutInventedDiscriminators(t *testing.T) {
	t.Setenv("FAILURE_MEMORY_HOME", t.TempDir())
	svc, err := Open("test")
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()
	recorded, err := svc.Remember(context.Background(), testFailure())
	if err != nil {
		t.Fatal(err)
	}
	recalled, err := svc.Recall(context.Background(), model.RecallInput{
		Text:         "Do not edit persisted schema before its compatibility preflight passes.",
		MinRelevance: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(recalled.Lessons) != 1 || recalled.Lessons[0].LessonID != recorded.LessonID {
		t.Fatalf("compact recall = %#v", recalled)
	}
}

func TestRememberOffersOnlyOneBoundedContractCorrection(t *testing.T) {
	t.Setenv("FAILURE_MEMORY_HOME", t.TempDir())
	svc, err := Open("test")
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()

	invalid := testFailure()
	invalid.Cause.Layer = "skill_tool_contract"
	invalid.Cause.FailureMode = "schema_mismatch"
	first, err := svc.Remember(context.Background(), invalid)
	if err != nil {
		t.Fatal(err)
	}
	if first.Status != model.Deferred || !first.Retryable || first.Correction == nil {
		t.Fatalf("first result = %#v", first)
	}
	if first.Correction.CorrectionOfCaptureEventID != first.CaptureEventID ||
		len(first.Correction.AllowedValues["cause.layer"]) == 0 ||
		len(first.Correction.AllowedValues["cause.failure_mode"]) == 0 {
		t.Fatalf("correction guidance = %#v", first.Correction)
	}

	corrected := testFailure()
	corrected.CorrectionOf = first.CaptureEventID
	second, err := svc.Remember(context.Background(), corrected)
	if err != nil {
		t.Fatal(err)
	}
	if second.Status != model.Recorded || second.Retryable || second.LessonID == "" {
		t.Fatalf("corrected result = %#v", second)
	}

	stillInvalid := invalid
	stillInvalid.CorrectionOf = first.CaptureEventID
	third, err := svc.Remember(context.Background(), stillInvalid)
	if err != nil {
		t.Fatal(err)
	}
	if third.Retryable || third.Correction != nil {
		t.Fatalf("second correction was offered: %#v", third)
	}
	counts, err := svc.Metrics(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	projected := counts["counts"].(map[string]int64)
	if projected["captures"] != 3 || projected["corrections"] != 2 ||
		projected["lessons"] != 1 || projected["incidents"] != 1 {
		t.Fatalf("counts = %#v", projected)
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
