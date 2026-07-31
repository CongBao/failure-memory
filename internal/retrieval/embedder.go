package retrieval

import (
	"hash/fnv"
	"math"
	"strings"
	"unicode"
)

type Embedder interface {
	Profile() string
	Dimensions() int
	Semantic() bool
	Embed(text string) ([]float32, error)
}

// FeatureHashEmbedder is a zero-dependency vector fallback. It keeps vector
// indexing and clustering available before an optional semantic model pack is
// installed, but it is never reported as semantic search.
type FeatureHashEmbedder struct {
	dimensions int
}

func NewFeatureHashEmbedder(dimensions int) *FeatureHashEmbedder {
	return &FeatureHashEmbedder{dimensions: dimensions}
}

func (e *FeatureHashEmbedder) Profile() string {
	return "feature-hash-v1"
}

func (e *FeatureHashEmbedder) Dimensions() int {
	return e.dimensions
}

func (e *FeatureHashEmbedder) Semantic() bool {
	return false
}

func (e *FeatureHashEmbedder) Embed(text string) ([]float32, error) {
	vector := make([]float32, e.dimensions)
	for _, token := range vectorTokens(text) {
		hash := fnv.New64a()
		_, _ = hash.Write([]byte(token))
		value := hash.Sum64()
		index := int(value % uint64(e.dimensions))
		sign := float32(1)
		if value&(1<<63) != 0 {
			sign = -1
		}
		vector[index] += sign
	}
	var norm float64
	for _, value := range vector {
		norm += float64(value * value)
	}
	if norm == 0 {
		return vector, nil
	}
	scale := float32(1 / math.Sqrt(norm))
	for index := range vector {
		vector[index] *= scale
	}
	return vector, nil
}

func vectorTokens(text string) []string {
	lower := strings.ToLower(strings.TrimSpace(text))
	fields := strings.FieldsFunc(lower, func(r rune) bool {
		return !(unicode.IsLetter(r) || unicode.IsDigit(r) || unicode.Is(unicode.Han, r))
	})
	tokens := make([]string, 0, len(fields)*2)
	for _, field := range fields {
		if field == "" {
			continue
		}
		tokens = append(tokens, field)
		runes := []rune(field)
		for index := 0; index+1 < len(runes); index++ {
			tokens = append(tokens, string(runes[index:index+2]))
		}
	}
	return tokens
}
