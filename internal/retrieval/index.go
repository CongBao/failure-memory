package retrieval

import (
	"context"
	"database/sql"
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/gofrs/flock"
	_ "modernc.org/sqlite"
	_ "modernc.org/sqlite/vec"

	"github.com/CongBao/failure-memory/internal/config"
	"github.com/CongBao/failure-memory/internal/lessonmanifest"
	"github.com/CongBao/failure-memory/internal/model"
	"github.com/CongBao/failure-memory/internal/ports"
)

const baseSchema = `
CREATE TABLE IF NOT EXISTS retrieval_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lesson_document (
    lesson_version_id TEXT PRIMARY KEY,
    lesson_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    title TEXT NOT NULL,
    rule TEXT NOT NULL,
    prevention TEXT NOT NULL,
    verification TEXT NOT NULL,
    applicability TEXT NOT NULL,
    counterexamples TEXT NOT NULL,
    cause_layer TEXT NOT NULL,
    failure_mode TEXT NOT NULL,
    component TEXT NOT NULL,
    document TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS lesson_document_signature_idx
ON lesson_document(signature);

CREATE TABLE IF NOT EXISTS exact_key (
    key TEXT NOT NULL,
    lesson_version_id TEXT NOT NULL,
    field TEXT NOT NULL,
    PRIMARY KEY(key, lesson_version_id, field),
    FOREIGN KEY(lesson_version_id) REFERENCES lesson_document(lesson_version_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS exact_key_lookup_idx ON exact_key(key);

CREATE VIRTUAL TABLE IF NOT EXISTS lesson_fts USING fts5(
    lesson_version_id UNINDEXED,
    document,
    tokenize = 'unicode61 remove_diacritics 2'
);
`

type Index struct {
	db       *sql.DB
	path     string
	embedder Embedder
}

type Candidate = ports.Candidate
type SearchResult = ports.SearchResult
type VectorCluster = ports.VectorCluster
type RetrievalStatus = ports.RetrievalStatus

var _ ports.RetrievalIndex = (*Index)(nil)

func Open(path string, embedder Embedder) (*Index, error) {
	if embedder == nil {
		embedder = NewFeatureHashEmbedder(384)
	}
	if err := config.EnsurePrivateDir(filepath.Dir(path)); err != nil {
		return nil, fmt.Errorf("create retrieval directory: %w", err)
	}
	dsn := fmt.Sprintf(
		"file:%s?_txlock=immediate&_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)&_pragma=synchronous(NORMAL)",
		filepath.ToSlash(path),
	)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open retrieval index: %w", err)
	}
	db.SetMaxOpenConns(2)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping retrieval index: %w", err)
	}
	initializationLock := flock.New(path + ".initialization.lock")
	locked, err := initializationLock.TryLockContext(ctx, 25*time.Millisecond)
	if err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("lock retrieval initialization: %w", err)
	}
	if !locked {
		_ = db.Close()
		return nil, errors.New("retrieval initialization is busy")
	}
	defer func() {
		_ = initializationLock.Unlock()
		_ = initializationLock.Close()
	}()
	if _, err := db.ExecContext(ctx, "PRAGMA journal_mode=WAL;"+baseSchema); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("initialize retrieval index: %w", err)
	}
	if err := initializeVectorSchema(ctx, db, embedder); err != nil {
		_ = db.Close()
		return nil, err
	}
	if err := os.Chmod(path, 0o600); err != nil && !errors.Is(err, os.ErrNotExist) {
		_ = db.Close()
		return nil, fmt.Errorf("protect retrieval index: %w", err)
	}
	return &Index{db: db, path: path, embedder: embedder}, nil
}

func initializeVectorSchema(ctx context.Context, db *sql.DB, embedder Embedder) error {
	profile := embedder.Profile() + ":" + strconv.Itoa(embedder.Dimensions())
	var existing string
	err := db.QueryRowContext(
		ctx,
		"SELECT value FROM retrieval_metadata WHERE key = 'embedding_profile'",
	).Scan(&existing)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	if existing != "" && existing != profile {
		if _, err := db.ExecContext(ctx, "DROP TABLE IF EXISTS lesson_vec"); err != nil {
			return err
		}
	}
	statement := fmt.Sprintf(
		"CREATE VIRTUAL TABLE IF NOT EXISTS lesson_vec USING vec0(lesson_version_id TEXT PRIMARY KEY, embedding float[%d] distance_metric=cosine)",
		embedder.Dimensions(),
	)
	if _, err := db.ExecContext(ctx, statement); err != nil {
		return fmt.Errorf("initialize sqlite-vec table: %w", err)
	}
	if _, err := db.ExecContext(ctx, `
		INSERT INTO retrieval_metadata(key, value) VALUES ('schema_version', '1')
		ON CONFLICT(key) DO UPDATE SET value = excluded.value;
		INSERT INTO retrieval_metadata(key, value) VALUES ('embedding_profile', ?)
		ON CONFLICT(key) DO UPDATE SET value = excluded.value;
		INSERT OR IGNORE INTO retrieval_metadata(key, value) VALUES ('source_revision', '0')`,
		profile,
	); err != nil {
		return err
	}
	return nil
}

func (i *Index) Close() error {
	var embedderErr error
	if closer, ok := i.embedder.(interface{ Close() error }); ok {
		embedderErr = closer.Close()
	}
	return errors.Join(i.db.Close(), embedderErr)
}

func (i *Index) Warm(ctx context.Context) error {
	if !i.embedder.Semantic() {
		return nil
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	default:
	}
	_, err := i.embedder.Embed("query: failure memory warmup")
	return err
}

func (i *Index) Path() string {
	return i.path
}

func (i *Index) Semantic() bool {
	return i.embedder.Semantic()
}

func (i *Index) Profile() string {
	return i.embedder.Profile()
}

func (i *Index) Status(ctx context.Context) (RetrievalStatus, error) {
	var status RetrievalStatus
	for query, destination := range map[string]*int64{
		"SELECT COUNT(*) FROM lesson_document": &status.Documents,
		"SELECT COUNT(*) FROM lesson_fts":      &status.Lexical,
		"SELECT COUNT(*) FROM lesson_vec":      &status.Vectors,
	} {
		if err := i.db.QueryRowContext(ctx, query).Scan(destination); err != nil {
			return RetrievalStatus{}, err
		}
	}
	var revisionText string
	if err := i.db.QueryRowContext(
		ctx,
		"SELECT value FROM retrieval_metadata WHERE key = 'source_revision'",
	).Scan(&revisionText); err != nil {
		return RetrievalStatus{}, err
	}
	revision, err := strconv.ParseInt(revisionText, 10, 64)
	if err != nil {
		return RetrievalStatus{}, fmt.Errorf("invalid retrieval source revision %q", revisionText)
	}
	status.SourceRevision = revision
	return status, nil
}

func (i *Index) Manifest(ctx context.Context) (string, error) {
	documents, err := i.listDocuments(ctx)
	if err != nil {
		return "", err
	}
	return lessonmanifest.Digest(documents), nil
}

func (i *Index) Upsert(
	ctx context.Context,
	lesson model.LessonDocument,
	sourceRevision int64,
) error {
	vector, err := i.embedder.Embed("passage: " + lesson.Document)
	if err != nil {
		return fmt.Errorf("embed lesson: %w", err)
	}
	blob := serializeFloat32(vector)
	tx, err := i.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	if err := upsertTx(ctx, tx, lesson, blob); err != nil {
		return err
	}
	if err := updateSourceRevisionTx(ctx, tx, sourceRevision); err != nil {
		return err
	}
	return tx.Commit()
}

func upsertTx(
	ctx context.Context,
	tx *sql.Tx,
	lesson model.LessonDocument,
	blob []byte,
) error {
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO lesson_document(
			lesson_version_id, lesson_id, signature, title, rule, prevention,
			verification, applicability, counterexamples, cause_layer,
			failure_mode, component, document, indexed_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(lesson_version_id) DO UPDATE SET
			lesson_id = excluded.lesson_id,
			signature = excluded.signature,
			title = excluded.title,
			rule = excluded.rule,
			prevention = excluded.prevention,
			verification = excluded.verification,
			applicability = excluded.applicability,
			counterexamples = excluded.counterexamples,
			cause_layer = excluded.cause_layer,
			failure_mode = excluded.failure_mode,
			component = excluded.component,
			document = excluded.document,
			indexed_at = excluded.indexed_at`,
		lesson.LessonVersionID,
		lesson.LessonID,
		lesson.Signature,
		lesson.Title,
		lesson.Rule,
		lesson.Prevention,
		lesson.Verification,
		lesson.Applicability,
		lesson.Counterexamples,
		lesson.CauseLayer,
		lesson.FailureMode,
		lesson.Component,
		lesson.Document,
		time.Now().UTC().Format(time.RFC3339Nano),
	); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM exact_key WHERE lesson_version_id = ?", lesson.LessonVersionID); err != nil {
		return err
	}
	keys := map[string]string{
		normalizeExact(lesson.Title):      "title",
		normalizeExact(lesson.Rule):       "rule",
		normalizeExact(lesson.Prevention): "prevention",
		normalizeExact(lesson.Component):  "component",
	}
	for key, field := range keys {
		if key == "" {
			continue
		}
		if _, err := tx.ExecContext(ctx, `
			INSERT OR IGNORE INTO exact_key(key, lesson_version_id, field)
			VALUES (?, ?, ?)`, key, lesson.LessonVersionID, field); err != nil {
			return err
		}
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM lesson_fts WHERE lesson_version_id = ?", lesson.LessonVersionID); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO lesson_fts(lesson_version_id, document) VALUES (?, ?)`,
		lesson.LessonVersionID,
		lexicalDocument(lesson.Document),
	); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM lesson_vec WHERE lesson_version_id = ?", lesson.LessonVersionID); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO lesson_vec(lesson_version_id, embedding) VALUES (?, ?)`,
		lesson.LessonVersionID,
		blob,
	); err != nil {
		return err
	}
	return nil
}

func (i *Index) Rebuild(
	ctx context.Context,
	lessons []model.LessonDocument,
	sourceRevision int64,
) error {
	type preparedLesson struct {
		document model.LessonDocument
		vector   []byte
	}
	prepared := make([]preparedLesson, 0, len(lessons))
	for _, lesson := range lessons {
		if err := ctx.Err(); err != nil {
			return err
		}
		vector, err := i.embedder.Embed("passage: " + lesson.Document)
		if err != nil {
			return fmt.Errorf("prepare lesson %s: %w", lesson.LessonVersionID, err)
		}
		prepared = append(prepared, preparedLesson{
			document: lesson,
			vector:   serializeFloat32(vector),
		})
	}
	tx, err := i.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	if _, err := tx.ExecContext(ctx, `
		DELETE FROM exact_key;
		DELETE FROM lesson_fts;
		DELETE FROM lesson_vec;
		DELETE FROM lesson_document;
	`); err != nil {
		return err
	}
	for _, lesson := range prepared {
		if err := upsertTx(ctx, tx, lesson.document, lesson.vector); err != nil {
			return err
		}
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO retrieval_metadata(key, value) VALUES ('source_revision', ?)
		ON CONFLICT(key) DO UPDATE SET value = excluded.value
	`, strconv.FormatInt(sourceRevision, 10)); err != nil {
		return err
	}
	return tx.Commit()
}

func updateSourceRevisionTx(ctx context.Context, tx *sql.Tx, sourceRevision int64) error {
	var currentText string
	if err := tx.QueryRowContext(
		ctx,
		"SELECT value FROM retrieval_metadata WHERE key = 'source_revision'",
	).Scan(&currentText); err != nil {
		return err
	}
	current, err := strconv.ParseInt(currentText, 10, 64)
	if err != nil {
		return fmt.Errorf("invalid retrieval source revision %q", currentText)
	}
	if sourceRevision < current {
		sourceRevision = current
	}
	_, err = tx.ExecContext(ctx, `
		UPDATE retrieval_metadata SET value = ? WHERE key = 'source_revision'
	`, strconv.FormatInt(sourceRevision, 10))
	return err
}

func (i *Index) listDocuments(ctx context.Context) ([]model.LessonDocument, error) {
	rows, err := i.db.QueryContext(ctx, `
		SELECT lesson_id, lesson_version_id, signature, title, rule, prevention,
		       verification, applicability, counterexamples, cause_layer,
		       failure_mode, component, document
		FROM lesson_document
		ORDER BY lesson_version_id
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var lessons []model.LessonDocument
	for rows.Next() {
		var lesson model.LessonDocument
		if err := rows.Scan(
			&lesson.LessonID,
			&lesson.LessonVersionID,
			&lesson.Signature,
			&lesson.Title,
			&lesson.Rule,
			&lesson.Prevention,
			&lesson.Verification,
			&lesson.Applicability,
			&lesson.Counterexamples,
			&lesson.CauseLayer,
			&lesson.FailureMode,
			&lesson.Component,
			&lesson.Document,
		); err != nil {
			return nil, err
		}
		lessons = append(lessons, lesson)
	}
	return lessons, rows.Err()
}

func (i *Index) Search(ctx context.Context, input model.RecallInput) (SearchResult, error) {
	mode := strings.ToLower(strings.TrimSpace(input.Mode))
	if mode == "" || mode == "auto" {
		if i.embedder.Semantic() {
			mode = "hybrid"
		} else {
			mode = "hybrid"
		}
	}
	switch mode {
	case "exact", "lexical", "semantic", "hybrid":
	default:
		return SearchResult{}, fmt.Errorf("unsupported retrieval mode %q", mode)
	}
	adaptiveTopK := input.TopK <= 0
	topK := input.TopK
	if topK <= 0 {
		topK = 3
	}
	if topK > 3 {
		topK = 3
	}
	minRelevance := input.MinRelevance
	if minRelevance < 0 || minRelevance > 1 {
		return SearchResult{}, errors.New("min_relevance must be between 0 and 1")
	}
	if minRelevance == 0 {
		minRelevance = defaultMinRelevance(mode, i.embedder.Semantic())
	}
	query := recallText(input)
	result := SearchResult{Mode: mode}
	if i.embedder.Semantic() {
		result.SemanticStatus = "ready"
	} else {
		result.SemanticStatus = "vector_fallback_nonsemantic"
	}

	rankings := map[string]map[string]int{}
	rawScores := map[string]map[string]float64{}
	if mode == "exact" || mode == "hybrid" {
		exact, err := i.exact(ctx, input, 12)
		if err != nil {
			return SearchResult{}, err
		}
		addRanking(rankings, rawScores, "exact", exact)
	}
	if mode == "lexical" || mode == "hybrid" {
		lexical, err := i.lexical(ctx, query, 20)
		if err != nil {
			return SearchResult{}, err
		}
		addRanking(rankings, rawScores, "lexical", lexical)
	}
	if mode == "semantic" || mode == "hybrid" {
		vector, err := i.vector(ctx, query, 20)
		if err != nil {
			return SearchResult{}, err
		}
		reason := "vector"
		if i.embedder.Semantic() {
			reason = "semantic"
		}
		addRanking(rankings, rawScores, reason, vector)
	}

	candidates := make([]Candidate, 0, len(rankings))
	for versionID, reasons := range rankings {
		score := 0.0
		names := make([]string, 0, len(reasons))
		for reason, rank := range reasons {
			names = append(names, reason)
			weight := 1.0
			if reason == "exact" {
				weight = 4
			}
			score += weight / float64(60+rank)
		}
		raw := rawScores[versionID]
		relevance := calibratedRelevance(reasons, raw)
		for _, value := range raw {
			score += value * 0.0001
		}
		sort.Strings(names)
		candidates = append(candidates, Candidate{
			LessonVersionID: versionID,
			Score:           score,
			RelevanceScore:  relevance,
			Reasons:         names,
		})
	}
	sort.SliceStable(candidates, func(left, right int) bool {
		if math.Abs(candidates[left].Score-candidates[right].Score) < 1e-12 {
			return candidates[left].LessonVersionID < candidates[right].LessonVersionID
		}
		return candidates[left].Score > candidates[right].Score
	})
	retrievedCount := len(candidates)
	eligible := candidates[:0]
	for _, candidate := range candidates {
		if candidate.RelevanceScore >= minRelevance {
			eligible = append(eligible, candidate)
		}
	}
	filtered := retrievedCount - len(eligible)
	candidates = eligible
	if len(input.Representatives) > 0 {
		seen := map[string]bool{}
		collapsed := candidates[:0]
		for _, candidate := range candidates {
			representative := resolvedRepresentative(
				candidate.LessonVersionID, input.Representatives,
			)
			if seen[representative] {
				result.CollapsedByCluster++
				continue
			}
			seen[representative] = true
			collapsed = append(collapsed, candidate)
		}
		candidates = collapsed
	}
	if adaptiveTopK && len(candidates) > 1 {
		beforeAdaptiveLimit := len(candidates)
		limit := 1
		for limit < len(candidates) && limit < topK &&
			candidates[0].RelevanceScore-candidates[limit].RelevanceScore <= 0.02 {
			limit++
		}
		candidates = candidates[:limit]
		result.TrimmedByAdaptiveLimit = beforeAdaptiveLimit - limit
	}
	if len(candidates) > topK {
		candidates = candidates[:topK]
	}
	result.Candidates = candidates
	result.RetrievedCount = retrievedCount
	result.FilteredBelowThreshold = filtered
	result.AppliedTopK = topK
	result.AppliedMinRelevance = minRelevance
	if len(candidates) == 0 && retrievedCount > 0 {
		result.AbstentionReason = "no_candidate_met_relevance_threshold"
	}
	return result, nil
}

func resolvedRepresentative(versionID string, representatives map[string]string) string {
	current := versionID
	seen := map[string]bool{}
	for {
		if seen[current] {
			// Corrupt/cyclic projections must still collapse deterministically.
			members := make([]string, 0, len(seen))
			for member := range seen {
				members = append(members, member)
			}
			sort.Strings(members)
			return members[0]
		}
		seen[current] = true
		next := representatives[current]
		if next == "" || next == current {
			return current
		}
		current = next
	}
}

// Clusters groups nearby lesson vectors without mutating any lesson. The
// resulting groups are proposals for human/agent review, never automatic
// merges.
func (i *Index) Clusters(ctx context.Context, threshold float64) ([]VectorCluster, int, error) {
	if threshold <= 0 || threshold > 2 {
		return nil, 0, errors.New("distance threshold must be greater than 0 and at most 2")
	}
	rows, err := i.db.QueryContext(ctx, `
		SELECT lesson_version_id, embedding
		FROM lesson_vec
		ORDER BY lesson_version_id`)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	var ids []string
	var vectors [][]float32
	for rows.Next() {
		var id string
		var blob []byte
		if err := rows.Scan(&id, &blob); err != nil {
			return nil, 0, err
		}
		vector, err := deserializeFloat32(blob)
		if err != nil {
			return nil, 0, fmt.Errorf("decode vector for %s: %w", id, err)
		}
		ids = append(ids, id)
		vectors = append(vectors, vector)
	}
	if err := rows.Err(); err != nil {
		return nil, 0, err
	}
	parent := make([]int, len(ids))
	for index := range parent {
		parent[index] = index
	}
	var find func(int) int
	find = func(value int) int {
		if parent[value] != value {
			parent[value] = find(parent[value])
		}
		return parent[value]
	}
	union := func(left, right int) {
		leftRoot, rightRoot := find(left), find(right)
		if leftRoot != rightRoot {
			parent[rightRoot] = leftRoot
		}
	}
	for left := range vectors {
		for right := left + 1; right < len(vectors); right++ {
			if cosineDistance(vectors[left], vectors[right]) <= threshold {
				union(left, right)
			}
		}
	}
	grouped := map[int][]string{}
	for index, id := range ids {
		root := find(index)
		grouped[root] = append(grouped[root], id)
	}
	var result []VectorCluster
	for _, members := range grouped {
		if len(members) < 2 {
			continue
		}
		sort.Strings(members)
		result = append(result, VectorCluster{
			Key:              "cluster-" + strconv.Itoa(len(result)+1),
			LessonVersionIDs: members,
		})
	}
	sort.Slice(result, func(left, right int) bool {
		return result[left].LessonVersionIDs[0] < result[right].LessonVersionIDs[0]
	})
	for index := range result {
		result[index].Key = "cluster-" + strconv.Itoa(index+1)
	}
	return result, len(ids), nil
}

func (i *Index) exact(ctx context.Context, input model.RecallInput, limit int) ([]Candidate, error) {
	values := []string{
		input.Text,
		input.ExpectedInvariant,
		input.ControllableCause,
		input.PreventionAction,
		input.Component,
	}
	seen := map[string]Candidate{}
	for _, value := range values {
		key := normalizeExact(value)
		if key == "" {
			continue
		}
		rows, err := i.db.QueryContext(ctx, `
			SELECT lesson_version_id FROM exact_key WHERE key = ? ORDER BY lesson_version_id LIMIT ?`,
			key,
			limit,
		)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var versionID string
			if err := rows.Scan(&versionID); err != nil {
				_ = rows.Close()
				return nil, err
			}
			seen[versionID] = Candidate{
				LessonVersionID: versionID,
				Score:           1,
				Reasons:         []string{"exact"},
			}
		}
		if err := rows.Close(); err != nil {
			return nil, err
		}
	}
	result := make([]Candidate, 0, len(seen))
	for _, candidate := range seen {
		result = append(result, candidate)
	}
	sort.Slice(result, func(left, right int) bool {
		return result[left].LessonVersionID < result[right].LessonVersionID
	})
	return result, nil
}

func (i *Index) lexical(ctx context.Context, query string, limit int) ([]Candidate, error) {
	ftsQuery := lexicalQuery(query)
	if ftsQuery == "" {
		return nil, nil
	}
	rows, err := i.db.QueryContext(ctx, `
		SELECT lesson_version_id, bm25(lesson_fts) AS distance
		FROM lesson_fts
		WHERE lesson_fts MATCH ?
		ORDER BY distance, lesson_version_id
		LIMIT ?`,
		ftsQuery,
		limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []Candidate
	for rows.Next() {
		var versionID string
		var distance float64
		if err := rows.Scan(&versionID, &distance); err != nil {
			return nil, err
		}
		result = append(result, Candidate{
			LessonVersionID: versionID,
			Score:           -distance,
			Reasons:         []string{"lexical"},
		})
	}
	return result, rows.Err()
}

func (i *Index) vector(ctx context.Context, query string, limit int) ([]Candidate, error) {
	vector, err := i.embedder.Embed("query: " + query)
	if err != nil {
		return nil, err
	}
	blob := serializeFloat32(vector)
	rows, err := i.db.QueryContext(ctx, `
		SELECT lesson_version_id, distance
		FROM lesson_vec
		WHERE embedding MATCH ? AND k = ?
		ORDER BY distance`,
		blob,
		limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []Candidate
	for rows.Next() {
		var versionID string
		var distance float64
		if err := rows.Scan(&versionID, &distance); err != nil {
			return nil, err
		}
		result = append(result, Candidate{
			LessonVersionID: versionID,
			Score:           1 - distance,
			Reasons:         []string{"vector"},
		})
	}
	return result, rows.Err()
}

func addRanking(
	rankings map[string]map[string]int,
	rawScores map[string]map[string]float64,
	reason string,
	candidates []Candidate,
) {
	for index, candidate := range candidates {
		if rankings[candidate.LessonVersionID] == nil {
			rankings[candidate.LessonVersionID] = map[string]int{}
			rawScores[candidate.LessonVersionID] = map[string]float64{}
		}
		rankings[candidate.LessonVersionID][reason] = index + 1
		rawScores[candidate.LessonVersionID][reason] = candidate.Score
	}
}

func calibratedRelevance(reasons map[string]int, raw map[string]float64) float64 {
	if _, exact := reasons["exact"]; exact {
		return 1
	}
	vector := math.Max(raw["semantic"], raw["vector"])
	if vector < 0 {
		vector = 0
	}
	if vector > 1 {
		vector = 1
	}
	lexical := 0.0
	if rank, ok := reasons["lexical"]; ok && rank > 0 {
		// SQLite FTS5 BM25 magnitudes vary with corpus size and are often tiny.
		// Rank is stable enough to calibrate lexical evidence to a bounded value.
		lexical = 1 / float64(rank+1)
	}
	if _, ok := reasons["lexical"]; ok {
		if _, semantic := reasons["semantic"]; semantic {
			return vector
		}
		if _, fallback := reasons["vector"]; fallback {
			return 0.7*vector + 0.3*lexical
		}
		return lexical
	}
	return vector
}

func defaultMinRelevance(mode string, semantic bool) float64 {
	switch mode {
	case "exact":
		return 1
	case "lexical":
		return 0.15
	case "semantic":
		if semantic {
			return 0.84
		}
		return 0.45
	default:
		if semantic {
			return 0.84
		}
		return 0.45
	}
}

func recallText(input model.RecallInput) string {
	return strings.Join([]string{
		input.Text,
		input.ExpectedInvariant,
		input.ControllableCause,
		input.PreventionAction,
		input.Component,
	}, " ")
}

func normalizeExact(value string) string {
	return strings.Join(strings.Fields(strings.ToLower(strings.TrimSpace(value))), " ")
}

func lexicalDocument(value string) string {
	tokens := lexicalTokens(value)
	return value + " " + strings.Join(tokens, " ")
}

func lexicalQuery(value string) string {
	tokens := lexicalTokens(value)
	if len(tokens) > 32 {
		tokens = tokens[:32]
	}
	quoted := make([]string, 0, len(tokens))
	for _, token := range tokens {
		token = strings.ReplaceAll(token, `"`, `""`)
		if token != "" {
			quoted = append(quoted, `"`+token+`"`)
		}
	}
	return strings.Join(quoted, " OR ")
}

func lexicalTokens(value string) []string {
	lower := strings.ToLower(value)
	fields := strings.FieldsFunc(lower, func(r rune) bool {
		return !(unicode.IsLetter(r) || unicode.IsDigit(r) || unicode.Is(unicode.Han, r))
	})
	seen := map[string]struct{}{}
	result := make([]string, 0, len(fields)*2)
	add := func(token string) {
		token = strings.TrimSpace(token)
		if token == "" {
			return
		}
		if _, ok := seen[token]; ok {
			return
		}
		seen[token] = struct{}{}
		result = append(result, token)
	}
	for _, field := range fields {
		add(field)
		runes := []rune(field)
		hasHan := false
		for _, r := range runes {
			if unicode.Is(unicode.Han, r) {
				hasHan = true
				break
			}
		}
		if hasHan {
			for index := 0; index+1 < len(runes); index++ {
				add(string(runes[index : index+2]))
			}
		}
	}
	return result
}

func serializeFloat32(vector []float32) []byte {
	buffer := make([]byte, len(vector)*4)
	for index, value := range vector {
		binary.LittleEndian.PutUint32(buffer[index*4:], math.Float32bits(value))
	}
	return buffer
}

func deserializeFloat32(blob []byte) ([]float32, error) {
	if len(blob)%4 != 0 {
		return nil, errors.New("invalid float32 vector length")
	}
	vector := make([]float32, len(blob)/4)
	for index := range vector {
		vector[index] = math.Float32frombits(binary.LittleEndian.Uint32(blob[index*4:]))
	}
	return vector, nil
}

func cosineDistance(left, right []float32) float64 {
	if len(left) == 0 || len(left) != len(right) {
		return 2
	}
	var dot, leftNorm, rightNorm float64
	for index := range left {
		l, r := float64(left[index]), float64(right[index])
		dot += l * r
		leftNorm += l * l
		rightNorm += r * r
	}
	if leftNorm == 0 || rightNorm == 0 {
		return 2
	}
	similarity := dot / (math.Sqrt(leftNorm) * math.Sqrt(rightNorm))
	if similarity > 1 {
		similarity = 1
	}
	if similarity < -1 {
		similarity = -1
	}
	return 1 - similarity
}
