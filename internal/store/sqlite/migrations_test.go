package sqlite

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"

	"github.com/CongBao/failure-memory/internal/model"
)

func TestOpenMigratesV1StoreWithoutChangingProjectedLesson(t *testing.T) {
	path := filepath.Join(t.TempDir(), "events.sqlite3")
	createV1Fixture(t, path)

	store, err := Open(path, model.Context{Harness: "test", Transport: "test"})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = store.Close() }()

	version, err := store.SchemaVersion(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if version != currentSchemaVersion {
		t.Fatalf("schema version = %d, want %d", version, currentSchemaVersion)
	}
	if store.StoreID() != "store-v1-fixture" {
		t.Fatalf("store id = %q", store.StoreID())
	}
	revision, err := store.LessonRevision(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if revision != 1 {
		t.Fatalf("lesson revision = %d, want 1", revision)
	}
	lesson, found, err := store.LessonByVersion(context.Background(), "lessonv-v1")
	if err != nil || !found {
		t.Fatalf("fixture lesson: found=%v err=%v", found, err)
	}
	if lesson.Rule != "preserve v1 lessons" || lesson.LessonID != "lesson-v1" {
		t.Fatalf("fixture lesson changed: %#v", lesson)
	}

	backups, err := filepath.Glob(filepath.Join(
		filepath.Dir(path), "migration-backups", "events-pre-v1-to-v3-*.sqlite3",
	))
	if err != nil {
		t.Fatal(err)
	}
	if len(backups) != 1 {
		t.Fatalf("migration backups = %#v, want one", backups)
	}
	info, err := os.Stat(backups[0])
	if err != nil {
		t.Fatal(err)
	}
	if info.Size() == 0 {
		t.Fatal("migration backup is empty")
	}
}

func TestOpenRejectsMigrationChecksumMismatch(t *testing.T) {
	path := filepath.Join(t.TempDir(), "events.sqlite3")
	store, err := Open(path, model.Context{})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 2"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(path, model.Context{}); err == nil {
		t.Fatal("store with a tampered migration checksum was accepted")
	}
}

func TestOpenRejectsNewerSchema(t *testing.T) {
	path := filepath.Join(t.TempDir(), "events.sqlite3")
	store, err := Open(path, model.Context{})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE store_metadata SET value = '99' WHERE key = 'schema_version'"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(path, model.Context{}); err == nil {
		t.Fatal("newer event-store schema was accepted")
	}
}

func TestLessonRevisionTriggerCoversOlderWriters(t *testing.T) {
	path := filepath.Join(t.TempDir(), "events.sqlite3")
	store, err := Open(path, model.Context{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(`
		INSERT INTO lesson_projection(
			lesson_id, lesson_version_id, signature, title, rule, prevention,
			verification, applicability, counterexamples, cause_layer,
			failure_mode, component, document, state, created_at, source_event_id
		) VALUES (
			'legacy-lesson', 'legacy-version', 'legacy-signature', 'legacy title',
			'legacy rule', 'legacy prevention', 'legacy verification', '', '',
			'unknown', 'unknown', 'legacy writer', 'legacy document', 'proposed',
			'2026-01-01T00:00:00Z', 'legacy-event'
		)
	`); err != nil {
		t.Fatal(err)
	}
	revision, err := store.LessonRevision(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if revision != 1 {
		t.Fatalf("lesson revision = %d, want 1", revision)
	}
}

func createV1Fixture(t *testing.T, path string) {
	t.Helper()
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(schemaV1); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`
		INSERT INTO store_metadata(key, value) VALUES
			('schema_version', '1'),
			('store_id', 'store-v1-fixture');
		INSERT INTO lesson_projection(
			lesson_id, lesson_version_id, signature, title, rule, prevention,
			verification, applicability, counterexamples, cause_layer,
			failure_mode, component, document, state, created_at, source_event_id
		) VALUES (
			'lesson-v1', 'lessonv-v1', 'signature-v1', 'v1 lesson',
			'preserve v1 lessons', 'migrate transactionally', 'compare the IDs',
			'', '', 'schema_migration', 'insufficient_validation', 'event store',
			'v1 lesson preserve v1 lessons', 'proposed',
			'2026-01-01T00:00:00Z', 'event-v1'
		);
	`); err != nil {
		t.Fatal(err)
	}
}
