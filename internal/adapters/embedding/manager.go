package embedding

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/gomlx/go-huggingface/hub"
	tokenizerapi "github.com/gomlx/go-huggingface/tokenizers/api"
	"github.com/knights-analytics/hugot"
	"github.com/knights-analytics/hugot/pipelines"

	"github.com/CongBao/failure-memory/internal/config"
	"github.com/CongBao/failure-memory/internal/retrieval"
)

const (
	ModelRepository = "Xenova/multilingual-e5-small"
	ModelRevision   = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
	ModelONNXPath   = "onnx/model_quantized.onnx"
	ModelFilename   = "model_quantized.onnx"
	ModelDimensions = 384
)

var requiredFiles = []string{
	"config.json",
	ModelFilename,
	"special_tokens_map.json",
	"tokenizer.json",
	"tokenizer_config.json",
}

type Manifest struct {
	SchemaVersion int               `json:"schema_version"`
	Provider      string            `json:"provider"`
	Model         string            `json:"model"`
	Revision      string            `json:"revision"`
	ONNXFilename  string            `json:"onnx_filename"`
	Dimensions    int               `json:"dimensions"`
	InstalledAt   string            `json:"installed_at"`
	Files         map[string]string `json:"files"`
}

type Status struct {
	Installed  bool   `json:"installed"`
	Valid      bool   `json:"valid"`
	Provider   string `json:"provider"`
	Model      string `json:"model"`
	Revision   string `json:"revision"`
	Dimensions int    `json:"dimensions"`
	Path       string `json:"path"`
	Reason     string `json:"reason,omitempty"`
}

type HugotEmbedder struct {
	modelPath string
	mu        sync.Mutex
	session   *hugot.Session
	pipeline  *pipelines.FeatureExtractionPipeline
}

func StatusAt(modelPath string) Status {
	return statusAt(modelPath, true)
}

func statusAt(modelPath string, verifyChecksums bool) Status {
	status := Status{
		Provider:   "hugot-go",
		Model:      ModelRepository,
		Revision:   ModelRevision,
		Dimensions: ModelDimensions,
		Path:       modelPath,
	}
	manifest, err := validate(modelPath, verifyChecksums)
	if err != nil {
		status.Reason = err.Error()
		return status
	}
	status.Installed = true
	status.Valid = manifest.Dimensions == ModelDimensions
	if !status.Valid {
		status.Reason = "embedding dimensions do not match the built-in profile"
	}
	return status
}

func Resolve(modelPath string) (retrieval.Embedder, Status) {
	// Normal agent calls use a cheap structural check. Explicit installation,
	// adapter status, and doctor paths still hash every model artifact.
	status := statusAt(modelPath, false)
	if !status.Valid {
		return retrieval.NewFeatureHashEmbedder(ModelDimensions), status
	}
	return &HugotEmbedder{modelPath: modelPath}, status
}

func Install(ctx context.Context, modelPath string) (Status, error) {
	if status := StatusAt(modelPath); status.Valid {
		if err := Verify(ctx, modelPath); err != nil {
			return status, fmt.Errorf("verify installed embedding adapter: %w", err)
		}
		return status, nil
	}
	if err := config.EnsurePrivateDir(filepath.Dir(modelPath)); err != nil {
		return Status{}, err
	}
	temporaryParent, err := os.MkdirTemp(filepath.Dir(modelPath), ".installing-*")
	if err != nil {
		return Status{}, err
	}
	defer func() { _ = os.RemoveAll(temporaryParent) }()

	cachePath := filepath.Join(temporaryParent, "download-cache")
	repository := hub.New(ModelRepository).
		WithCacheDir(cachePath).
		WithRevision(ModelRevision)
	repository.MaxParallelDownload = 4
	remoteFiles := []string{
		"config.json",
		ModelONNXPath,
		"special_tokens_map.json",
		"tokenizer.json",
		"tokenizer_config.json",
	}
	downloadedFiles, err := repository.DownloadFiles(remoteFiles...)
	if err != nil {
		return Status{}, fmt.Errorf("download embedding adapter: %w", err)
	}
	downloaded := filepath.Join(temporaryParent, "model")
	if err := config.EnsurePrivateDir(downloaded); err != nil {
		return Status{}, err
	}
	for index, source := range downloadedFiles {
		destination := filepath.Join(downloaded, filepath.Base(remoteFiles[index]))
		if err := copyRegularFile(ctx, source, destination); err != nil {
			return Status{}, err
		}
	}
	if err := os.RemoveAll(cachePath); err != nil {
		return Status{}, fmt.Errorf("remove temporary embedding cache: %w", err)
	}
	files := map[string]string{}
	for _, name := range requiredFiles {
		path := filepath.Join(downloaded, name)
		info, err := os.Lstat(path)
		if err != nil {
			return Status{}, fmt.Errorf("embedding adapter missing %s: %w", name, err)
		}
		if !info.Mode().IsRegular() {
			return Status{}, fmt.Errorf("embedding adapter file %s is not regular", name)
		}
		digest, err := fileSHA256(path)
		if err != nil {
			return Status{}, err
		}
		files[name] = digest
	}
	manifest := Manifest{
		SchemaVersion: 1,
		Provider:      "hugot-go",
		Model:         ModelRepository,
		Revision:      ModelRevision,
		ONNXFilename:  ModelFilename,
		Dimensions:    ModelDimensions,
		InstalledAt:   time.Now().UTC().Format(time.RFC3339Nano),
		Files:         files,
	}
	if err := writeJSONExclusive(filepath.Join(downloaded, "manifest.json"), manifest); err != nil {
		return Status{}, err
	}
	if err := makeTreePrivate(downloaded); err != nil {
		return Status{}, err
	}
	if err := Verify(ctx, downloaded); err != nil {
		return Status{}, fmt.Errorf("verify downloaded embedding adapter: %w", err)
	}
	if _, err := os.Lstat(modelPath); err == nil {
		return Status{}, errors.New("invalid embedding adapter already exists; move it aside before retrying")
	} else if !errors.Is(err, os.ErrNotExist) {
		return Status{}, err
	}
	if err := os.Rename(downloaded, modelPath); err != nil {
		return Status{}, fmt.Errorf("publish embedding adapter: %w", err)
	}
	return StatusAt(modelPath), nil
}

// Verify performs a real inference, including the long-input boundary that has
// historically differed between tokenizer implementations. Artifact checksums
// alone are not sufficient evidence that an adapter is usable.
func Verify(ctx context.Context, modelPath string) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	default:
	}
	embedder := &HugotEmbedder{modelPath: modelPath}
	defer func() { _ = embedder.Close() }()
	probe := "query: " + strings.Repeat("bounded semantic adapter verification ", 600)
	vector, err := embedder.Embed(probe)
	if err != nil {
		return err
	}
	if len(vector) != ModelDimensions {
		return fmt.Errorf(
			"embedding verification returned %d dimensions, expected %d",
			len(vector),
			ModelDimensions,
		)
	}
	return nil
}

func (e *HugotEmbedder) Profile() string {
	return "multilingual-e5-small-int8@" + ModelRevision[:12]
}

func (e *HugotEmbedder) Dimensions() int {
	return ModelDimensions
}

func (e *HugotEmbedder) Semantic() bool {
	return true
}

func (e *HugotEmbedder) Embed(text string) ([]float32, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.pipeline == nil {
		if err := e.load(context.Background()); err != nil {
			return nil, err
		}
	}
	bounded, err := e.boundText(text)
	if err != nil {
		return nil, err
	}
	output, err := e.pipeline.RunPipeline(context.Background(), []string{bounded})
	if err != nil {
		return nil, err
	}
	if len(output.Embeddings) != 1 || len(output.Embeddings[0]) != ModelDimensions {
		return nil, fmt.Errorf("embedding adapter returned unexpected shape")
	}
	return output.Embeddings[0], nil
}

func (e *HugotEmbedder) Close() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.session == nil {
		return nil
	}
	err := e.session.Destroy()
	e.session = nil
	e.pipeline = nil
	return err
}

func (e *HugotEmbedder) load(ctx context.Context) error {
	session, err := hugot.NewGoSession(ctx)
	if err != nil {
		return fmt.Errorf("start pure-Go embedding session: %w", err)
	}
	pipeline, err := hugot.NewPipeline(session, hugot.FeatureExtractionConfig{
		ModelPath:    e.modelPath,
		Name:         "failure-memory-embedding",
		OnnxFilename: ModelFilename,
		Options: []hugot.FeatureExtractionOption{
			pipelines.WithNormalization(),
		},
	})
	if err != nil {
		_ = session.Destroy()
		return fmt.Errorf("load embedding model: %w", err)
	}
	if pipeline.Model == nil ||
		pipeline.Model.Tokenizer == nil ||
		pipeline.Model.Tokenizer.GoTokenizer == nil ||
		pipeline.Model.Tokenizer.GoTokenizer.Tokenizer == nil {
		_ = session.Destroy()
		return errors.New("pure-Go embedding pipeline did not expose a tokenizer")
	}
	maxTokens := pipeline.Model.MaxPositionEmbeddings
	if maxTokens <= 0 {
		_ = session.Destroy()
		return errors.New("embedding model did not declare a positive token limit")
	}
	// gomlx/go-huggingface v0.4 accepts MaxLen but does not apply it in its
	// Hugging Face tokenizer implementation. Request token annotations so this
	// adapter can enforce the model boundary before inference.
	if err := pipeline.Model.Tokenizer.GoTokenizer.Tokenizer.With(tokenizerapi.EncodeOptions{
		AddSpecialTokens:         true,
		MaxLen:                   maxTokens,
		IncludeSpans:             true,
		IncludeSpecialTokensMask: true,
	}); err != nil {
		_ = session.Destroy()
		return fmt.Errorf("configure embedding tokenizer boundary: %w", err)
	}
	pipeline.Model.Tokenizer.MaxAllowedTokens = maxTokens
	e.session = session
	e.pipeline = pipeline
	return nil
}

func (e *HugotEmbedder) boundText(text string) (string, error) {
	if e.pipeline == nil ||
		e.pipeline.Model == nil ||
		e.pipeline.Model.Tokenizer == nil ||
		e.pipeline.Model.Tokenizer.GoTokenizer == nil {
		return "", errors.New("embedding tokenizer is not loaded")
	}
	tokenizer := e.pipeline.Model.Tokenizer.GoTokenizer.Tokenizer
	maxTokens := e.pipeline.Model.MaxPositionEmbeddings
	bounded := text
	for attempts := 0; attempts < 32; attempts++ {
		encoded := tokenizer.EncodeWithAnnotations(bounded)
		if len(encoded.IDs) <= maxTokens {
			return bounded, nil
		}
		next, err := truncateAnnotated(bounded, encoded, maxTokens)
		if err != nil {
			return "", err
		}
		if len(next) >= len(bounded) {
			runes := []rune(bounded)
			if len(runes) <= 1 {
				return "", errors.New("embedding tokenizer could not enforce its token limit")
			}
			next = string(runes[:len(runes)-1])
		}
		bounded = next
	}
	return "", errors.New("embedding tokenizer did not converge on its token limit")
}

func truncateAnnotated(
	text string,
	encoded tokenizerapi.AnnotatedEncoding,
	maxTokens int,
) (string, error) {
	if len(encoded.IDs) <= maxTokens {
		return text, nil
	}
	if len(encoded.Spans) != len(encoded.IDs) ||
		len(encoded.SpecialTokensMask) != len(encoded.IDs) {
		return "", errors.New("embedding tokenizer did not return complete token annotations")
	}
	specialTokens := 0
	for _, value := range encoded.SpecialTokensMask {
		if value != 0 {
			specialTokens++
		}
	}
	contentBudget := maxTokens - specialTokens
	if contentBudget <= 0 {
		return "", errors.New("embedding model token limit leaves no content capacity")
	}
	contentTokens := 0
	end := 0
	for index, span := range encoded.Spans {
		if encoded.SpecialTokensMask[index] != 0 {
			continue
		}
		contentTokens++
		if contentTokens > contentBudget {
			break
		}
		if span.End > end {
			end = span.End
		}
	}
	if end > len(text) {
		end = len(text)
	}
	if end <= 0 {
		return "", fmt.Errorf(
			"embedding tokenizer could not identify a safe truncation boundary (tokens=%d, special=%d, content_budget=%d, end=%d)",
			len(encoded.IDs),
			specialTokens,
			contentBudget,
			end,
		)
	}
	return text[:end], nil
}

func validate(modelPath string, verifyChecksums bool) (Manifest, error) {
	data, err := os.ReadFile(filepath.Join(modelPath, "manifest.json"))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return Manifest{}, errors.New("semantic adapter is not installed")
		}
		return Manifest{}, err
	}
	var manifest Manifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		return Manifest{}, fmt.Errorf("invalid embedding manifest: %w", err)
	}
	if manifest.SchemaVersion != 1 ||
		manifest.Provider != "hugot-go" ||
		manifest.Model != ModelRepository ||
		manifest.Revision != ModelRevision ||
		manifest.ONNXFilename != ModelFilename {
		return Manifest{}, errors.New("embedding manifest does not match the supported profile")
	}
	names := make([]string, 0, len(manifest.Files))
	for name := range manifest.Files {
		names = append(names, name)
	}
	sort.Strings(names)
	if strings.Join(names, "\x00") != strings.Join(requiredFiles, "\x00") {
		return Manifest{}, errors.New("embedding manifest file set is incomplete")
	}
	for _, name := range requiredFiles {
		path := filepath.Join(modelPath, name)
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() {
			return Manifest{}, fmt.Errorf("embedding file %s is missing or unsafe", name)
		}
		if info.Size() == 0 {
			return Manifest{}, fmt.Errorf("embedding file %s is empty", name)
		}
		if !verifyChecksums {
			continue
		}
		digest, err := fileSHA256(path)
		if err != nil {
			return Manifest{}, err
		}
		if digest != manifest.Files[name] {
			return Manifest{}, fmt.Errorf("embedding file %s failed checksum validation", name)
		}
	}
	return manifest, nil
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

func copyRegularFile(ctx context.Context, source, destination string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.OpenFile(destination, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(output, input)
	closeErr := output.Close()
	return errors.Join(copyErr, closeErr)
}

func writeJSONExclusive(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	if _, err := file.Write(data); err != nil {
		_ = file.Close()
		return err
	}
	return file.Close()
}

func makeTreePrivate(root string) error {
	return filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if runtime.GOOS == "windows" {
			return nil
		}
		mode := os.FileMode(0o600)
		if info.IsDir() {
			mode = 0o700
		}
		return os.Chmod(path, mode)
	})
}
