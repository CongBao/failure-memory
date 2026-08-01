package sqlite

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/CongBao/failure-memory/internal/model"
)

func TestBackupVerifyAndRestoreRoundTrip(t *testing.T) {
	root := t.TempDir()
	eventStore := filepath.Join(root, "events.sqlite3")
	store, err := Open(eventStore, model.Context{Harness: "test", Transport: "test"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.appendStandaloneEvent(
		context.Background(), "test_event", "test-operation", map[string]any{"value": "before"},
	); err != nil {
		t.Fatal(err)
	}
	backupPath := filepath.Join(root, "backups", "first")
	created, err := store.CreateBackup(context.Background(), backupPath)
	if err != nil {
		t.Fatal(err)
	}
	if created.Manifest.EventCount != 1 || created.Manifest.StoreID != store.StoreID() {
		t.Fatalf("created backup = %#v", created)
	}
	if _, err := store.appendStandaloneEvent(
		context.Background(), "test_event", "test-operation", map[string]any{"value": "after"},
	); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	verified, err := VerifyBackup(context.Background(), backupPath)
	if err != nil {
		t.Fatal(err)
	}
	if verified.Status != "verified" || verified.Manifest.EventCount != 1 {
		t.Fatalf("verified backup = %#v", verified)
	}
	restored, err := RestoreBackup(
		context.Background(), eventStore, backupPath, filepath.Join(root, "safety"),
	)
	if err != nil {
		t.Fatal(err)
	}
	if restored.Status != "restored" || restored.SafetyBackup == "" {
		t.Fatalf("restore result = %#v", restored)
	}
	safety, err := VerifyBackup(context.Background(), restored.SafetyBackup)
	if err != nil {
		t.Fatal(err)
	}
	if safety.Manifest.EventCount != 2 {
		t.Fatalf("safety backup event count = %d, want 2", safety.Manifest.EventCount)
	}

	store, err = Open(eventStore, model.Context{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	counts, err := store.Counts(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if counts["events"] != 1 || store.StoreID() != created.Manifest.StoreID {
		t.Fatalf("restored store counts=%#v id=%q", counts, store.StoreID())
	}
}

func TestRestoreRefusesAnEventStoreInUse(t *testing.T) {
	root := t.TempDir()
	eventStore := filepath.Join(root, "events.sqlite3")
	store, err := Open(eventStore, model.Context{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	backupPath := filepath.Join(root, "backup")
	if _, err := store.CreateBackup(context.Background(), backupPath); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 75*time.Millisecond)
	defer cancel()
	if _, err := RestoreBackup(ctx, eventStore, backupPath, filepath.Join(root, "safety")); err == nil {
		t.Fatal("restore accepted an event store held by a running service")
	}
}

func TestVerifyRejectsTamperedBackup(t *testing.T) {
	root := t.TempDir()
	store, err := Open(filepath.Join(root, "events.sqlite3"), model.Context{})
	if err != nil {
		t.Fatal(err)
	}
	backupPath := filepath.Join(root, "backup")
	if _, err := store.CreateBackup(context.Background(), backupPath); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	file, err := os.OpenFile(
		filepath.Join(backupPath, "events.sqlite3"), os.O_WRONLY|os.O_APPEND, 0,
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.Write([]byte("tampered")); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := VerifyBackup(context.Background(), backupPath); err == nil {
		t.Fatal("tampered backup was accepted")
	}
}

func TestOpenRecoversAnInterruptedRestoreBeforePublication(t *testing.T) {
	root := t.TempDir()
	eventStore := filepath.Join(root, "events.sqlite3")
	store, err := Open(eventStore, model.Context{})
	if err != nil {
		t.Fatal(err)
	}
	storeID := store.StoreID()
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	rollbackPath := eventStore + ".restore-rollback"
	if err := os.Rename(eventStore, rollbackPath); err != nil {
		t.Fatal(err)
	}
	if err := writeJSONFileAtomically(eventStore+".restore-state.json", restoreMarker{
		EventStorePath: eventStore,
		RollbackPath:   rollbackPath,
	}); err != nil {
		t.Fatal(err)
	}
	store, err = Open(eventStore, model.Context{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if store.StoreID() != storeID {
		t.Fatalf("recovered store id = %q, want %q", store.StoreID(), storeID)
	}
	if _, err := os.Stat(eventStore + ".restore-state.json"); !os.IsNotExist(err) {
		t.Fatalf("restore marker remains: %v", err)
	}
}
