package retrieval

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"github.com/CongBao/failure-memory/internal/model"
)

type failingEmbedder struct {
	base   Embedder
	failOn string
}

func (e *failingEmbedder) Profile() string { return e.base.Profile() }
func (e *failingEmbedder) Dimensions() int { return e.base.Dimensions() }
func (e *failingEmbedder) Semantic() bool  { return e.base.Semantic() }
func (e *failingEmbedder) Embed(text string) ([]float32, error) {
	if e.failOn != "" && contains(text, e.failOn) {
		return nil, errors.New("synthetic embedding failure")
	}
	return e.base.Embed(text)
}

func contains(value, substring string) bool {
	for index := 0; index+len(substring) <= len(value); index++ {
		if value[index:index+len(substring)] == substring {
			return true
		}
	}
	return false
}

func TestRebuildPreservesExistingIndexWhenEmbeddingPreparationFails(t *testing.T) {
	ctx := context.Background()
	base := NewFeatureHashEmbedder(32)
	index, err := Open(filepath.Join(t.TempDir(), "index.sqlite3"), base)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = index.Close() }()

	original := testLesson("lesson-v1", "original")
	if err := index.Upsert(ctx, original, 1); err != nil {
		t.Fatal(err)
	}
	index.embedder = &failingEmbedder{base: base, failOn: "second"}
	err = index.Rebuild(ctx, []model.LessonDocument{
		testLesson("lesson-v2", "first"),
		testLesson("lesson-v3", "second"),
	}, 2)
	if err == nil {
		t.Fatal("rebuild unexpectedly succeeded")
	}
	status, statusErr := index.Status(ctx)
	if statusErr != nil {
		t.Fatal(statusErr)
	}
	if status.Documents != 1 || status.Lexical != 1 || status.Vectors != 1 {
		t.Fatalf("existing index was changed after failed preparation: %#v", status)
	}
	var retained int
	if err := index.db.QueryRowContext(
		ctx,
		"SELECT COUNT(*) FROM lesson_document WHERE lesson_version_id = ?",
		original.LessonVersionID,
	).Scan(&retained); err != nil {
		t.Fatal(err)
	}
	if retained != 1 {
		t.Fatal("original lesson was not retained")
	}
}

func TestSearchUsesRelevanceThresholdBeforeTopK(t *testing.T) {
	ctx := context.Background()
	index, err := Open(filepath.Join(t.TempDir(), "index.sqlite3"), NewFeatureHashEmbedder(128))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = index.Close() }()

	lessons := []model.LessonDocument{
		testLesson("lesson-v1", "schema migration compatibility"),
		testLesson("lesson-v2", "browser credential boundary"),
		testLesson("lesson-v3", "audio rendering pipeline"),
	}
	if err := index.Rebuild(ctx, lessons, int64(len(lessons))); err != nil {
		t.Fatal(err)
	}

	result, err := index.Search(ctx, model.RecallInput{
		Text:         "quarterly catering menu",
		Component:    "office lunch",
		Mode:         "hybrid",
		TopK:         3,
		MinRelevance: 0.95,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Candidates) != 0 {
		t.Fatalf("below-threshold candidates returned: %#v", result.Candidates)
	}
	if result.RetrievedCount != 3 || result.FilteredBelowThreshold != 3 {
		t.Fatalf("search diagnostics = %#v", result)
	}
	if result.AppliedTopK != 3 || result.AppliedMinRelevance != 0.95 {
		t.Fatalf("applied policy = %#v", result)
	}
}

func TestSearchTreatsTopKAsAnUpperBoundAfterThresholding(t *testing.T) {
	ctx := context.Background()
	index, err := Open(filepath.Join(t.TempDir(), "index.sqlite3"), NewFeatureHashEmbedder(128))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = index.Close() }()
	lessons := []model.LessonDocument{
		testLesson("lesson-v1", "one"),
		testLesson("lesson-v2", "two"),
		testLesson("lesson-v3", "three"),
	}
	if err := index.Rebuild(ctx, lessons, int64(len(lessons))); err != nil {
		t.Fatal(err)
	}

	result, err := index.Search(ctx, model.RecallInput{
		Text:         "bounded retrieval",
		Component:    "retrieval",
		Mode:         "hybrid",
		TopK:         2,
		MinRelevance: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Candidates) != 2 {
		t.Fatalf("candidate count = %d, want 2: %#v", len(result.Candidates), result)
	}
	for _, candidate := range result.Candidates {
		if candidate.RelevanceScore != 1 {
			t.Fatalf("exact candidate relevance = %f, want 1", candidate.RelevanceScore)
		}
	}
}

func TestSearchUsesAdaptiveLimitOnlyWhenTopKIsOmitted(t *testing.T) {
	ctx := context.Background()
	index, err := Open(filepath.Join(t.TempDir(), "index.sqlite3"), NewFeatureHashEmbedder(128))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = index.Close() }()
	lessons := []model.LessonDocument{
		testLesson("lesson-v1", "shared recall token alpha"),
		testLesson("lesson-v2", "shared recall token beta"),
		testLesson("lesson-v3", "shared recall token gamma"),
	}
	if err := index.Rebuild(ctx, lessons, int64(len(lessons))); err != nil {
		t.Fatal(err)
	}
	adaptive, err := index.Search(ctx, model.RecallInput{
		Text: "shared recall token", Mode: "lexical",
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(adaptive.Candidates) != 1 {
		t.Fatalf("adaptive default returned %d candidates: %#v", len(adaptive.Candidates), adaptive)
	}
	if adaptive.TrimmedByAdaptiveLimit != 2 || adaptive.AppliedTopK != 3 {
		t.Fatalf("adaptive diagnostics = %#v", adaptive)
	}
	explicit, err := index.Search(ctx, model.RecallInput{
		Text: "shared recall token", Mode: "lexical", TopK: 3,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(explicit.Candidates) != 3 {
		t.Fatalf("explicit top_k returned %d candidates: %#v", len(explicit.Candidates), explicit)
	}
}

func TestSearchUsesProfileThresholdWhenCallerOmitsMinimum(t *testing.T) {
	ctx := context.Background()
	index, err := Open(filepath.Join(t.TempDir(), "index.sqlite3"), NewFeatureHashEmbedder(128))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = index.Close() }()
	lessons := []model.LessonDocument{
		testLesson("lesson-v1", "schema migration compatibility"),
		testLesson("lesson-v2", "browser credential boundary"),
		testLesson("lesson-v3", "audio rendering pipeline"),
	}
	if err := index.Rebuild(ctx, lessons, int64(len(lessons))); err != nil {
		t.Fatal(err)
	}

	result, err := index.Search(ctx, model.RecallInput{
		Text: "quarterly catering menu",
		Mode: "auto",
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.AppliedMinRelevance <= 0 {
		t.Fatalf("profile threshold was not applied: %#v", result)
	}
	if len(result.Candidates) != 0 || result.AbstentionReason == "" {
		t.Fatalf("unrelated query did not abstain: %#v", result)
	}
}

func TestSearchReturnsOneWhenAResultClearlyLeads(t *testing.T) {
	ctx := context.Background()
	index, err := Open(filepath.Join(t.TempDir(), "index.sqlite3"), NewFeatureHashEmbedder(128))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = index.Close() }()
	lessons := []model.LessonDocument{
		testLesson("lesson-v1", "schema migration compatibility"),
		testLesson("lesson-v2", "browser credential boundary"),
		testLesson("lesson-v3", "audio rendering pipeline"),
	}
	if err := index.Rebuild(ctx, lessons, int64(len(lessons))); err != nil {
		t.Fatal(err)
	}
	result, err := index.Search(ctx, model.RecallInput{
		Text: "rule schema migration compatibility",
		Mode: "auto",
		TopK: 3,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Candidates) != 1 || result.Candidates[0].LessonVersionID != "lesson-v1" {
		t.Fatalf("clear leader result = %#v", result)
	}
}

func TestSearchCollapsesRelatedLessonsBeforeApplyingTopK(t *testing.T) {
	ctx := context.Background()
	index, err := Open(filepath.Join(t.TempDir(), "index.sqlite3"), NewFeatureHashEmbedder(128))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = index.Close() }()
	lessons := []model.LessonDocument{
		testLesson("lesson-v1", "one"),
		testLesson("lesson-v2", "two"),
		testLesson("lesson-v3", "three"),
	}
	if err := index.Rebuild(ctx, lessons, int64(len(lessons))); err != nil {
		t.Fatal(err)
	}
	result, err := index.Search(ctx, model.RecallInput{
		Text:         "bounded retrieval",
		Component:    "retrieval",
		TopK:         3,
		MinRelevance: 1,
		Representatives: map[string]string{
			"lesson-v1": "pending-cluster-1",
			"lesson-v2": "pending-cluster-1",
			"lesson-v3": "lesson-v2",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Candidates) != 1 || result.CollapsedByCluster != 2 {
		t.Fatalf("cluster collapse result = %#v", result)
	}
}

func testLesson(versionID, marker string) model.LessonDocument {
	return model.LessonDocument{
		LessonID:        "lesson-" + marker,
		LessonVersionID: versionID,
		Signature:       "signature-" + marker,
		Title:           "title " + marker,
		Rule:            "rule " + marker,
		Prevention:      "prevention " + marker,
		Verification:    "verification " + marker,
		Applicability:   "applicability " + marker,
		Counterexamples: "counterexamples " + marker,
		CauseLayer:      "application_logic",
		FailureMode:     "insufficient_validation",
		Component:       "retrieval",
		Document:        "document " + marker,
	}
}
