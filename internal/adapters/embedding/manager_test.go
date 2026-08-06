package embedding

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"
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

func TestBoundTextToTokenLimitConvergesWithoutTokenizerSpans(t *testing.T) {
	countTokens := func(value string) int {
		return 2 + len([]rune(value))
	}
	bounded, err := boundTextToTokenLimit(
		strings.Repeat("界", 900),
		512,
		countTokens,
	)
	if err != nil {
		t.Fatal(err)
	}
	if got := countTokens(bounded); got > 512 {
		t.Fatalf("bounded token count = %d, want at most 512", got)
	}
	if got := len([]rune(bounded)); got != 510 {
		t.Fatalf("bounded runes = %d, want 510", got)
	}
}

func TestBoundTextToTokenLimitBoundsTokenizerWorkAndPreservesUTF8(t *testing.T) {
	maximumInputBytes := 0
	countTokens := func(value string) int {
		if !utf8.ValidString(value) {
			t.Fatal("tokenizer received invalid UTF-8")
		}
		if len(value) > maximumInputBytes {
			maximumInputBytes = len(value)
		}
		return 2 + len([]rune(value))
	}
	bounded, err := boundTextToTokenLimit(
		strings.Repeat("界", 20_000),
		512,
		countTokens,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !utf8.ValidString(bounded) {
		t.Fatal("bounded text is not valid UTF-8")
	}
	if got := countTokens(bounded); got > 512 {
		t.Fatalf("bounded token count = %d, want at most 512", got)
	}
	if maximumInputBytes > 512*8 {
		t.Fatalf("tokenizer saw %d bytes, want at most %d", maximumInputBytes, 512*8)
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
