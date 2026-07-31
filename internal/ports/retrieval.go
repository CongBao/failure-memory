// Package ports defines the implementation-neutral contracts used by the
// application service. Alternative retrieval backends (for example Milvus)
// implement this interface without changing qualification, persistence, MCP,
// CLI, or skill behavior.
package ports

import (
	"context"

	"github.com/CongBao/failure-memory/internal/model"
)

type Candidate struct {
	LessonVersionID string
	Score           float64
	Reasons         []string
}

type SearchResult struct {
	Mode           string
	SemanticStatus string
	Candidates     []Candidate
}

type VectorCluster struct {
	Key              string
	LessonVersionIDs []string
}

type RetrievalStatus struct {
	Documents int64 `json:"documents"`
	Lexical   int64 `json:"lexical"`
	Vectors   int64 `json:"vectors"`
}

type RetrievalIndex interface {
	Close() error
	Warm(context.Context) error
	Profile() string
	Semantic() bool
	Status(context.Context) (RetrievalStatus, error)
	Upsert(context.Context, model.LessonDocument) error
	Rebuild(context.Context, []model.LessonDocument) error
	Search(context.Context, model.RecallInput) (SearchResult, error)
	Clusters(context.Context, float64) ([]VectorCluster, int, error)
}
