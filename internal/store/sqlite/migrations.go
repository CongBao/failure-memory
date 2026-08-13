package sqlite

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/gofrs/flock"

	"github.com/CongBao/failure-memory/internal/config"
	"github.com/CongBao/failure-memory/internal/identity"
)

const currentSchemaVersion = 4

const schemaV2 = `
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

INSERT OR IGNORE INTO store_metadata(key, value)
VALUES ('lesson_revision', CAST((SELECT COUNT(*) FROM lesson_projection) AS TEXT));
`

const schemaV3 = `
CREATE TRIGGER IF NOT EXISTS lesson_projection_revision_insert
AFTER INSERT ON lesson_projection BEGIN
    UPDATE store_metadata
    SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
    WHERE key = 'lesson_revision';
END;
`

const schemaV4 = `
CREATE TABLE IF NOT EXISTS lesson_lifecycle_projection (
    lesson_version_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    representative_lesson_version_id TEXT NOT NULL,
    last_outcome TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    FOREIGN KEY(lesson_version_id) REFERENCES lesson_projection(lesson_version_id)
);

CREATE INDEX IF NOT EXISTS lesson_lifecycle_state_idx
ON lesson_lifecycle_projection(state, lesson_version_id);

CREATE TABLE IF NOT EXISTS memory_outcome_receipt (
    idempotency_key TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS memory_outcome_receipt_no_update
BEFORE UPDATE ON memory_outcome_receipt BEGIN
    SELECT RAISE(ABORT, 'memory_outcome_receipt is append-only');
END;

CREATE TRIGGER IF NOT EXISTS memory_outcome_receipt_no_delete
BEFORE DELETE ON memory_outcome_receipt BEGIN
    SELECT RAISE(ABORT, 'memory_outcome_receipt is append-only');
END;

INSERT OR IGNORE INTO lesson_lifecycle_projection(
    lesson_version_id, state, representative_lesson_version_id,
    last_outcome, updated_at, source_event_id
)
SELECT lesson_version_id, state, lesson_version_id, '', created_at, source_event_id
FROM lesson_projection;

UPDATE lesson_lifecycle_projection AS lifecycle
SET representative_lesson_version_id = COALESCE((
    SELECT json_extract(event.payload_json, '$.related_lesson_version_ids[0]')
    FROM event_log AS event
    WHERE event.event_type = 'generalization_review_proposed'
      AND json_extract(event.payload_json, '$.new_lesson_version_id') = lifecycle.lesson_version_id
    ORDER BY event.sequence DESC
    LIMIT 1
), lifecycle.lesson_version_id)
WHERE EXISTS (
    SELECT 1 FROM event_log AS event
    WHERE event.event_type = 'generalization_review_proposed'
      AND json_extract(event.payload_json, '$.new_lesson_version_id') = lifecycle.lesson_version_id
);

INSERT OR IGNORE INTO store_metadata(key, value)
VALUES ('retrieval_revision',
    COALESCE((SELECT value FROM store_metadata WHERE key = 'lesson_revision'), '0'));

DROP TRIGGER IF EXISTS lesson_projection_revision_insert;
CREATE TRIGGER lesson_projection_revision_insert
AFTER INSERT ON lesson_projection BEGIN
    UPDATE store_metadata
    SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
    WHERE key = 'lesson_revision';
    UPDATE store_metadata
    SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
    WHERE key = 'retrieval_revision';
    INSERT OR IGNORE INTO lesson_lifecycle_projection(
        lesson_version_id, state, representative_lesson_version_id,
        last_outcome, updated_at, source_event_id
    ) VALUES (
        NEW.lesson_version_id, NEW.state, NEW.lesson_version_id,
        '', NEW.created_at, NEW.source_event_id
    );
END;
`

type migration struct {
	version int
	sql     string
}

var migrations = []migration{
	{version: 2, sql: schemaV2},
	{version: 3, sql: schemaV3},
	{version: 4, sql: schemaV4},
}

func initializeAndMigrate(
	ctx context.Context,
	db *sql.DB,
	path string,
) (string, error) {
	lock := flock.New(path + ".migration.lock")
	locked, err := lock.TryLockContext(ctx, 25*time.Millisecond)
	if err != nil {
		return "", fmt.Errorf("lock event-store migration: %w", err)
	}
	if !locked {
		return "", errors.New("event-store migration is busy")
	}
	defer func() {
		_ = lock.Unlock()
		_ = lock.Close()
	}()
	if _, err := db.ExecContext(ctx, "PRAGMA journal_mode=WAL"); err != nil {
		return "", fmt.Errorf("enable event-store WAL mode: %w", err)
	}

	existing, err := tableExists(ctx, db, "store_metadata")
	if err != nil {
		return "", err
	}
	if !existing {
		if err := initializeV1(ctx, db); err != nil {
			return "", err
		}
	}
	version, err := readSchemaVersion(ctx, db)
	if err != nil {
		return "", err
	}
	if version > currentSchemaVersion {
		return "", fmt.Errorf(
			"event-store schema %d is newer than supported schema %d",
			version,
			currentSchemaVersion,
		)
	}
	if version < 1 {
		return "", fmt.Errorf("unsupported event-store schema %d", version)
	}
	if existing && version < currentSchemaVersion {
		if _, err := createMigrationSnapshot(ctx, db, path, version, currentSchemaVersion); err != nil {
			return "", fmt.Errorf("create pre-migration backup: %w", err)
		}
	}
	if err := applyMigrations(ctx, db, version); err != nil {
		return "", err
	}
	if err := validateMigrationChecksums(ctx, db); err != nil {
		return "", err
	}
	var storeID string
	if err := db.QueryRowContext(
		ctx,
		"SELECT value FROM store_metadata WHERE key = 'store_id'",
	).Scan(&storeID); err != nil {
		return "", err
	}
	return storeID, nil
}

func initializeV1(ctx context.Context, db *sql.DB) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	if _, err := tx.ExecContext(ctx, schemaV1); err != nil {
		return fmt.Errorf("initialize event-store schema v1: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT OR IGNORE INTO store_metadata(key, value) VALUES ('schema_version', '1');
	`); err != nil {
		return err
	}
	if _, err := tx.ExecContext(
		ctx,
		"INSERT OR IGNORE INTO store_metadata(key, value) VALUES ('store_id', ?)",
		identity.New("store"),
	); err != nil {
		return err
	}
	return tx.Commit()
}

func applyMigrations(ctx context.Context, db *sql.DB, from int) error {
	for _, item := range migrations {
		if item.version <= from {
			continue
		}
		if err := applyMigration(ctx, db, item); err != nil {
			return err
		}
	}
	return nil
}

func applyMigration(ctx context.Context, db *sql.DB, item migration) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	var liveVersionText string
	if err := tx.QueryRowContext(
		ctx,
		"SELECT value FROM store_metadata WHERE key = 'schema_version'",
	).Scan(&liveVersionText); err != nil {
		return err
	}
	liveVersion, err := strconv.Atoi(liveVersionText)
	if err != nil {
		return fmt.Errorf("invalid event-store schema version %q", liveVersionText)
	}
	if liveVersion >= item.version {
		return nil
	}
	if liveVersion != item.version-1 {
		return fmt.Errorf(
			"cannot migrate event-store schema %d to %d",
			liveVersion,
			item.version,
		)
	}
	if _, err := tx.ExecContext(ctx, item.sql); err != nil {
		return fmt.Errorf("apply event-store migration v%d: %w", item.version, err)
	}
	if item.version == 2 {
		if _, err := tx.ExecContext(ctx, `
			INSERT OR IGNORE INTO schema_migrations(version, checksum, applied_at)
			VALUES (1, ?, ?)
		`, migrationChecksum(schemaV1), time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
			return err
		}
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO schema_migrations(version, checksum, applied_at)
		VALUES (?, ?, ?)
		ON CONFLICT(version) DO UPDATE SET
			checksum = excluded.checksum,
			applied_at = excluded.applied_at
	`, item.version, migrationChecksum(item.sql), time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
		return err
	}
	if _, err := tx.ExecContext(
		ctx,
		"UPDATE store_metadata SET value = ? WHERE key = 'schema_version'",
		strconv.Itoa(item.version),
	); err != nil {
		return err
	}
	return tx.Commit()
}

func validateMigrationChecksums(ctx context.Context, db *sql.DB) error {
	checksums := map[int]string{1: migrationChecksum(schemaV1)}
	for _, item := range migrations {
		checksums[item.version] = migrationChecksum(item.sql)
	}
	rows, err := db.QueryContext(ctx, `
		SELECT version, checksum FROM schema_migrations ORDER BY version
	`)
	if err != nil {
		return err
	}
	defer rows.Close()
	seen := map[int]bool{}
	for rows.Next() {
		var version int
		var actual string
		if err := rows.Scan(&version, &actual); err != nil {
			return err
		}
		expected, ok := checksums[version]
		if !ok || actual != expected {
			return fmt.Errorf("event-store migration v%d checksum mismatch", version)
		}
		seen[version] = true
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for version := 1; version <= currentSchemaVersion; version++ {
		if !seen[version] {
			return fmt.Errorf("event-store migration v%d is not recorded", version)
		}
	}
	return nil
}

func tableExists(ctx context.Context, db *sql.DB, name string) (bool, error) {
	var count int
	err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?
	`, name).Scan(&count)
	return count > 0, err
}

func readSchemaVersion(ctx context.Context, db *sql.DB) (int, error) {
	var value string
	if err := db.QueryRowContext(
		ctx,
		"SELECT value FROM store_metadata WHERE key = 'schema_version'",
	).Scan(&value); err != nil {
		return 0, err
	}
	version, err := strconv.Atoi(value)
	if err != nil {
		return 0, fmt.Errorf("invalid event-store schema version %q", value)
	}
	return version, nil
}

func migrationChecksum(value string) string {
	digest := sha256.Sum256([]byte(value))
	return hex.EncodeToString(digest[:])
}

func createMigrationSnapshot(
	ctx context.Context,
	db *sql.DB,
	path string,
	from int,
	to int,
) (string, error) {
	directory := filepath.Join(filepath.Dir(path), "migration-backups")
	if err := config.EnsurePrivateDir(directory); err != nil {
		return "", err
	}
	name := fmt.Sprintf(
		"events-pre-v%d-to-v%d-%s.sqlite3",
		from,
		to,
		time.Now().UTC().Format("20060102T150405.000000000Z"),
	)
	destination := filepath.Join(directory, name)
	if _, err := db.ExecContext(ctx, "VACUUM INTO ?", destination); err != nil {
		_ = os.Remove(destination)
		return "", err
	}
	if err := os.Chmod(destination, 0o600); err != nil && !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	return destination, nil
}

func lessonRevisionTx(ctx context.Context, tx *sql.Tx) (int64, error) {
	var value string
	if err := tx.QueryRowContext(
		ctx,
		"SELECT value FROM store_metadata WHERE key = 'lesson_revision'",
	).Scan(&value); err != nil {
		return 0, err
	}
	return strconv.ParseInt(value, 10, 64)
}

func syncLessonRevisionTx(ctx context.Context, tx *sql.Tx) (int64, error) {
	if _, err := tx.ExecContext(ctx, `
		UPDATE store_metadata
		SET value = CAST((SELECT COUNT(*) FROM lesson_projection) AS TEXT)
		WHERE key = 'lesson_revision'
	`); err != nil {
		return 0, err
	}
	return lessonRevisionTx(ctx, tx)
}

func (s *Store) SchemaVersion(ctx context.Context) (int, error) {
	return readSchemaVersion(ctx, s.db)
}

func (s *Store) LessonRevision(ctx context.Context) (int64, error) {
	var value string
	if err := s.db.QueryRowContext(
		ctx,
		"SELECT value FROM store_metadata WHERE key = 'lesson_revision'",
	).Scan(&value); err != nil {
		return 0, err
	}
	return strconv.ParseInt(value, 10, 64)
}

func retrievalRevisionTx(ctx context.Context, tx *sql.Tx) (int64, error) {
	var value string
	if err := tx.QueryRowContext(
		ctx,
		"SELECT value FROM store_metadata WHERE key = 'retrieval_revision'",
	).Scan(&value); err != nil {
		return 0, err
	}
	return strconv.ParseInt(value, 10, 64)
}

func (s *Store) RetrievalRevision(ctx context.Context) (int64, error) {
	var value string
	if err := s.db.QueryRowContext(
		ctx,
		"SELECT value FROM store_metadata WHERE key = 'retrieval_revision'",
	).Scan(&value); err != nil {
		return 0, err
	}
	return strconv.ParseInt(value, 10, 64)
}
