package embedding

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	tokenizerapi "github.com/gomlx/go-huggingface/tokenizers/api"
)

func TestStatusValidatesPinnedManifestAndEveryChecksum(t *testing.T) {
	root := t.TempDir()
	files := map[string]string{}
	for _, name := range requiredFiles {
		content := []byte("synthetic-" + name)
		if err := os.WriteFile(filepath.Join(root, name), content, 0o600); err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(content)
		files[name] = hex.EncodeToString(sum[:])
	}
	manifest := Manifest{
		SchemaVersion: 1,
		Provider:      "hugot-go",
		Model:         ModelRepository,
		Revision:      ModelRevision,
		ONNXFilename:  ModelFilename,
		Dimensions:    ModelDimensions,
		Files:         files,
	}
	data, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "manifest.json"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	if status := StatusAt(root); !status.Valid {
		t.Fatalf("valid adapter rejected: %#v", status)
	}
	if err := os.WriteFile(filepath.Join(root, "tokenizer.json"), []byte("tampered"), 0o600); err != nil {
		t.Fatal(err)
	}
	if status := StatusAt(root); status.Valid {
		t.Fatalf("tampered adapter accepted: %#v", status)
	}
}

func TestTruncateAnnotatedKeepsUTF8BoundaryAndSpecialTokenBudget(t *testing.T) {
	text := "甲乙丙丁戊"
	encoded := tokenizerapi.AnnotatedEncoding{
		IDs:               []int{101, 1, 2, 3, 4, 5, 102},
		Spans:             []tokenizerapi.TokenSpan{{Start: -1, End: -1}, {Start: 0, End: 3}, {Start: 3, End: 6}, {Start: 6, End: 9}, {Start: 9, End: 12}, {Start: 12, End: 15}, {Start: -1, End: -1}},
		SpecialTokensMask: []int{1, 0, 0, 0, 0, 0, 1},
	}
	bounded, err := truncateAnnotated(text, encoded, 5)
	if err != nil {
		t.Fatal(err)
	}
	if bounded != "甲乙丙" {
		t.Fatalf("bounded text = %q, want %q", bounded, "甲乙丙")
	}
}

func TestTruncateAnnotatedRejectsIncompleteAnnotations(t *testing.T) {
	_, err := truncateAnnotated(
		"too long",
		tokenizerapi.AnnotatedEncoding{
			IDs:               []int{1, 2, 3},
			Spans:             []tokenizerapi.TokenSpan{{Start: 0, End: 3}},
			SpecialTokensMask: []int{0, 0, 0},
		},
		2,
	)
	if err == nil {
		t.Fatal("incomplete annotations were accepted")
	}
}

func TestHugotEmbedderAcceptsLongInput(t *testing.T) {
	modelPath := os.Getenv("FAILURE_MEMORY_TEST_MODEL_PATH")
	if modelPath == "" {
		t.Skip("set FAILURE_MEMORY_TEST_MODEL_PATH to run the local model integration test")
	}
	embedder := &HugotEmbedder{modelPath: modelPath}
	defer func() { _ = embedder.Close() }()
	vector, err := embedder.Embed(
		"passage: " + strings.Repeat("long input boundary verification ", 700),
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(vector) != ModelDimensions {
		t.Fatalf("embedding dimensions = %d, want %d", len(vector), ModelDimensions)
	}
}
