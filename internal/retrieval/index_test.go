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
