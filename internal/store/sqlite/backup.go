package sqlite

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/gofrs/flock"
	_ "modernc.org/sqlite"

	"github.com/CongBao/failure-memory/internal/config"
)

const backupManifestVersion = 1

type BackupManifest struct {
	ManifestVersion int    `json:"manifest_version"`
	CreatedAt       string `json:"created_at"`
	StoreID         string `json:"store_id"`
	SchemaVersion   int    `json:"schema_version"`
	DatabaseFile    string `json:"database_file"`
	DatabaseSHA256  string `json:"database_sha256"`
	EventCount      int64  `json:"event_count"`
	LessonCount     int64  `json:"lesson_count"`
}

type BackupResult struct {
	Status   string         `json:"status"`
	Path     string         `json:"path"`
	Manifest BackupManifest `json:"manifest"`
}

type RestoreResult struct {
	Status       string         `json:"status"`
	Path         string         `json:"path"`
	SafetyBackup string         `json:"safety_backup,omitempty"`
	Manifest     BackupManifest `json:"manifest"`
}

type restoreMarker struct {
	EventStorePath string `json:"event_store_path"`
	RollbackPath   string `json:"rollback_path"`
}

func (s *Store) CreateBackup(ctx context.Context, destination string) (BackupResult, error) {
	return createBackupFromDB(ctx, s.db, destination)
}

func createBackupFromDB(
	ctx context.Context,
	db *sql.DB,
	destination string,
) (BackupResult, error) {
	destination, err := filepath.Abs(strings.TrimSpace(destination))
	if err != nil || destination == "" {
		return BackupResult{}, errors.New("backup destination is required")
	}
	if _, err := os.Lstat(destination); err == nil {
		return BackupResult{}, fmt.Errorf("backup destination already exists: %s", destination)
	} else if !errors.Is(err, os.ErrNotExist) {
		return BackupResult{}, err
	}
	parent := filepath.Dir(destination)
	if err := config.EnsurePrivateDir(parent); err != nil {
		return BackupResult{}, err
	}
	temporary, err := os.MkdirTemp(parent, ".failure-memory-backup-*")
	if err != nil {
		return BackupResult{}, err
	}
	defer func() { _ = os.RemoveAll(temporary) }()
	databasePath := filepath.Join(temporary, "events.sqlite3")
	if _, err := db.ExecContext(ctx, "VACUUM INTO ?", databasePath); err != nil {
		return BackupResult{}, fmt.Errorf("snapshot event store: %w", err)
	}
	if runtime.GOOS != "windows" {
		if err := os.Chmod(databasePath, 0o600); err != nil {
			return BackupResult{}, err
		}
	}
	verified, err := verifyDatabase(ctx, databasePath)
	if err != nil {
		return BackupResult{}, fmt.Errorf("verify backup snapshot: %w", err)
	}
	digest, err := fileDigest(databasePath)
	if err != nil {
		return BackupResult{}, err
	}
	manifest := BackupManifest{
		ManifestVersion: backupManifestVersion,
		CreatedAt:       time.Now().UTC().Format(time.RFC3339Nano),
		StoreID:         verified.StoreID,
		SchemaVersion:   verified.SchemaVersion,
		DatabaseFile:    "events.sqlite3",
		DatabaseSHA256:  digest,
		EventCount:      verified.EventCount,
		LessonCount:     verified.LessonCount,
	}
	if err := writeBackupManifest(filepath.Join(temporary, "manifest.json"), manifest); err != nil {
		return BackupResult{}, err
	}
	if err := os.Rename(temporary, destination); err != nil {
		return BackupResult{}, fmt.Errorf("publish backup: %w", err)
	}
	if err := syncDirectory(parent); err != nil {
		return BackupResult{}, fmt.Errorf("sync backup directory: %w", err)
	}
	return BackupResult{Status: "created", Path: destination, Manifest: manifest}, nil
}

func VerifyBackup(ctx context.Context, backupPath string) (BackupResult, error) {
	backupPath, err := filepath.Abs(strings.TrimSpace(backupPath))
	if err != nil || backupPath == "" {
		return BackupResult{}, errors.New("backup path is required")
	}
	info, err := os.Lstat(backupPath)
	if err != nil {
		return BackupResult{}, err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return BackupResult{}, errors.New("backup path must be a real directory")
	}
	manifest, err := readBackupManifest(filepath.Join(backupPath, "manifest.json"))
	if err != nil {
		return BackupResult{}, err
	}
	if manifest.ManifestVersion != backupManifestVersion {
		return BackupResult{}, fmt.Errorf(
			"unsupported backup manifest version %d",
			manifest.ManifestVersion,
		)
	}
	if manifest.DatabaseFile != "events.sqlite3" {
		return BackupResult{}, errors.New("backup manifest references an unsupported database file")
	}
	databasePath := filepath.Join(backupPath, manifest.DatabaseFile)
	digest, err := fileDigest(databasePath)
	if err != nil {
		return BackupResult{}, err
	}
	if digest != manifest.DatabaseSHA256 {
		return BackupResult{}, errors.New("backup database checksum mismatch")
	}
	verified, err := verifyDatabase(ctx, databasePath)
	if err != nil {
		return BackupResult{}, err
	}
	if verified.StoreID != manifest.StoreID ||
		verified.SchemaVersion != manifest.SchemaVersion ||
		verified.EventCount != manifest.EventCount ||
		verified.LessonCount != manifest.LessonCount {
		return BackupResult{}, errors.New("backup manifest does not match the database")
	}
	return BackupResult{Status: "verified", Path: backupPath, Manifest: manifest}, nil
}

func RestoreBackup(
	ctx context.Context,
	eventStorePath string,
	backupPath string,
	safetyBackupRoot string,
) (RestoreResult, error) {
	verified, err := VerifyBackup(ctx, backupPath)
	if err != nil {
		return RestoreResult{}, err
	}
	if err := config.EnsurePrivateDir(filepath.Dir(eventStorePath)); err != nil {
		return RestoreResult{}, err
	}
	if err := recoverInterruptedRestore(eventStorePath); err != nil {
		return RestoreResult{}, err
	}
	usageLock := flock.New(eventStorePath + ".usage.lock")
	lockContext, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	locked, err := usageLock.TryLockContext(lockContext, 25*time.Millisecond)
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			return RestoreResult{}, errors.New(
				"event store is in use; stop running agent applications before restore",
			)
		}
		return RestoreResult{}, fmt.Errorf("lock event store for restore: %w", err)
	}
	if !locked {
		return RestoreResult{}, errors.New(
			"event store is in use; stop running agent applications before restore",
		)
	}
	defer func() {
		_ = usageLock.Unlock()
		_ = usageLock.Close()
	}()

	var safetyPath string
	if info, statErr := os.Stat(eventStorePath); statErr == nil && info.Mode().IsRegular() {
		if err := config.EnsurePrivateDir(safetyBackupRoot); err != nil {
			return RestoreResult{}, err
		}
		safetyPath = filepath.Join(
			safetyBackupRoot,
			"pre-restore-"+time.Now().UTC().Format("20060102T150405.000000000Z"),
		)
		current, err := sql.Open("sqlite", sqliteDSN(eventStorePath, false))
		if err != nil {
			return RestoreResult{}, err
		}
		if _, err := current.ExecContext(ctx, "PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
			_ = current.Close()
			return RestoreResult{}, err
		}
		_, backupErr := createBackupFromDB(ctx, current, safetyPath)
		closeErr := current.Close()
		if err := errors.Join(backupErr, closeErr); err != nil {
			return RestoreResult{}, fmt.Errorf("create pre-restore safety backup: %w", err)
		}
	} else if statErr != nil && !errors.Is(statErr, os.ErrNotExist) {
		return RestoreResult{}, statErr
	}

	source := filepath.Join(verified.Path, verified.Manifest.DatabaseFile)
	temporary, err := copyIntoDirectory(source, filepath.Dir(eventStorePath))
	if err != nil {
		return RestoreResult{}, err
	}
	defer func() { _ = os.Remove(temporary) }()
	if _, err := verifyDatabase(ctx, temporary); err != nil {
		return RestoreResult{}, fmt.Errorf("verify staged restore: %w", err)
	}
	rollbackPath := eventStorePath + ".restore-rollback"
	markerPath := eventStorePath + ".restore-state.json"
	_ = os.Remove(rollbackPath)
	if err := writeJSONFileAtomically(markerPath, restoreMarker{
		EventStorePath: eventStorePath,
		RollbackPath:   rollbackPath,
	}); err != nil {
		return RestoreResult{}, err
	}
	if err := syncDirectory(filepath.Dir(eventStorePath)); err != nil {
		return RestoreResult{}, fmt.Errorf("sync restore marker: %w", err)
	}
	hadCurrent := false
	if _, err := os.Stat(eventStorePath); err == nil {
		if err := os.Rename(eventStorePath, rollbackPath); err != nil {
			return RestoreResult{}, fmt.Errorf("stage current event store: %w", err)
		}
		hadCurrent = true
	} else if !errors.Is(err, os.ErrNotExist) {
		return RestoreResult{}, err
	}
	if err := os.Rename(temporary, eventStorePath); err != nil {
		if hadCurrent {
			_ = os.Rename(rollbackPath, eventStorePath)
		}
		_ = os.Remove(markerPath)
		return RestoreResult{}, fmt.Errorf("publish restored event store: %w", err)
	}
	_ = syncDirectory(filepath.Dir(eventStorePath))
	for _, suffix := range []string{"-wal", "-shm"} {
		_ = os.Remove(eventStorePath + suffix)
	}
	if hadCurrent {
		_ = os.Remove(rollbackPath)
	}
	_ = os.Remove(markerPath)
	if runtime.GOOS != "windows" {
		if err := os.Chmod(eventStorePath, 0o600); err != nil {
			return RestoreResult{}, err
		}
	}
	return RestoreResult{
		Status:       "restored",
		Path:         eventStorePath,
		SafetyBackup: safetyPath,
		Manifest:     verified.Manifest,
	}, nil
}

func recoverInterruptedRestore(eventStorePath string) error {
	markerPath := eventStorePath + ".restore-state.json"
	if _, err := os.Lstat(markerPath); errors.Is(err, os.ErrNotExist) {
		return nil
	} else if err != nil {
		return err
	}
	lock := flock.New(eventStorePath + ".usage.lock")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	locked, err := lock.TryLockContext(ctx, 25*time.Millisecond)
	if err != nil {
		return fmt.Errorf("lock interrupted restore recovery: %w", err)
	}
	if !locked {
		return errors.New("interrupted restore recovery is busy")
	}
	defer func() {
		_ = lock.Unlock()
		_ = lock.Close()
	}()
	file, err := os.Open(markerPath)
	if err != nil {
		return err
	}
	var marker restoreMarker
	decodeErr := json.NewDecoder(io.LimitReader(file, 1<<20)).Decode(&marker)
	closeErr := file.Close()
	if err := errors.Join(decodeErr, closeErr); err != nil {
		return fmt.Errorf("read interrupted restore marker: %w", err)
	}
	if marker.EventStorePath != eventStorePath ||
		marker.RollbackPath != eventStorePath+".restore-rollback" {
		return errors.New("interrupted restore marker does not match the event store")
	}
	if info, err := os.Stat(eventStorePath); err == nil && info.Mode().IsRegular() {
		_ = os.Remove(marker.RollbackPath)
		return os.Remove(markerPath)
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if info, err := os.Stat(marker.RollbackPath); err == nil && info.Mode().IsRegular() {
		if err := os.Rename(marker.RollbackPath, eventStorePath); err != nil {
			return fmt.Errorf("restore pre-operation event store: %w", err)
		}
		_ = syncDirectory(filepath.Dir(eventStorePath))
		return os.Remove(markerPath)
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return errors.New("interrupted restore has neither a restored nor rollback event store")
}

type verifiedDatabase struct {
	StoreID       string
	SchemaVersion int
	EventCount    int64
	LessonCount   int64
}

func verifyDatabase(ctx context.Context, path string) (verifiedDatabase, error) {
	db, err := sql.Open("sqlite", sqliteDSN(path, true))
	if err != nil {
		return verifiedDatabase{}, err
	}
	defer db.Close()
	var integrity string
	if err := db.QueryRowContext(ctx, "PRAGMA quick_check").Scan(&integrity); err != nil {
		return verifiedDatabase{}, err
	}
	if integrity != "ok" {
		return verifiedDatabase{}, fmt.Errorf("database quick_check: %s", integrity)
	}
	var result verifiedDatabase
	var schemaText string
	if err := db.QueryRowContext(
		ctx,
		"SELECT value FROM store_metadata WHERE key = 'store_id'",
	).Scan(&result.StoreID); err != nil {
		return verifiedDatabase{}, err
	}
	if err := db.QueryRowContext(
		ctx,
		"SELECT value FROM store_metadata WHERE key = 'schema_version'",
	).Scan(&schemaText); err != nil {
		return verifiedDatabase{}, err
	}
	if _, err := fmt.Sscanf(schemaText, "%d", &result.SchemaVersion); err != nil ||
		result.SchemaVersion < 1 || result.SchemaVersion > currentSchemaVersion {
		return verifiedDatabase{}, fmt.Errorf("unsupported backup schema %q", schemaText)
	}
	if err := db.QueryRowContext(ctx, "SELECT COUNT(*) FROM event_log").Scan(&result.EventCount); err != nil {
		return verifiedDatabase{}, err
	}
	if err := db.QueryRowContext(ctx, "SELECT COUNT(*) FROM lesson_projection").Scan(&result.LessonCount); err != nil {
		return verifiedDatabase{}, err
	}
	rows, err := db.QueryContext(ctx, `
		SELECT event_id, payload_json, payload_sha256 FROM event_log ORDER BY sequence
	`)
	if err != nil {
		return verifiedDatabase{}, err
	}
	defer rows.Close()
	for rows.Next() {
		var eventID, payload, expected string
		if err := rows.Scan(&eventID, &payload, &expected); err != nil {
			return verifiedDatabase{}, err
		}
		digest := sha256.Sum256([]byte(payload))
		if hex.EncodeToString(digest[:]) != expected {
			return verifiedDatabase{}, fmt.Errorf("event payload checksum mismatch: %s", eventID)
		}
	}
	if err := rows.Err(); err != nil {
		return verifiedDatabase{}, err
	}
	return result, nil
}

func sqliteDSN(path string, readOnly bool) string {
	query := "_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)"
	if readOnly {
		query = "mode=ro&_pragma=query_only(1)&" + query
	}
	return fmt.Sprintf("file:%s?%s", filepath.ToSlash(path), query)
}

func fileDigest(path string) (string, error) {
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

func writeBackupManifest(path string, manifest BackupManifest) error {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	encodeErr := encoder.Encode(manifest)
	syncErr := file.Sync()
	closeErr := file.Close()
	return errors.Join(encodeErr, syncErr, closeErr)
}

func writeJSONFileAtomically(path string, value any) error {
	temporary, err := os.CreateTemp(filepath.Dir(path), ".failure-memory-state-*")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer func() { _ = os.Remove(temporaryPath) }()
	encoder := json.NewEncoder(temporary)
	encodeErr := encoder.Encode(value)
	syncErr := temporary.Sync()
	closeErr := temporary.Close()
	if err := errors.Join(encodeErr, syncErr, closeErr); err != nil {
		return err
	}
	if runtime.GOOS != "windows" {
		if err := os.Chmod(temporaryPath, 0o600); err != nil {
			return err
		}
	}
	return os.Rename(temporaryPath, path)
}

func syncDirectory(path string) error {
	if runtime.GOOS == "windows" {
		return nil
	}
	directory, err := os.Open(path)
	if err != nil {
		return err
	}
	return errors.Join(directory.Sync(), directory.Close())
}

func readBackupManifest(path string) (BackupManifest, error) {
	file, err := os.Open(path)
	if err != nil {
		return BackupManifest{}, err
	}
	defer file.Close()
	decoder := json.NewDecoder(io.LimitReader(file, 1<<20))
	decoder.DisallowUnknownFields()
	var manifest BackupManifest
	if err := decoder.Decode(&manifest); err != nil {
		return BackupManifest{}, fmt.Errorf("read backup manifest: %w", err)
	}
	return manifest, nil
}

func copyIntoDirectory(source string, directory string) (string, error) {
	input, err := os.Open(source)
	if err != nil {
		return "", err
	}
	defer input.Close()
	output, err := os.CreateTemp(directory, ".failure-memory-restore-*")
	if err != nil {
		return "", err
	}
	path := output.Name()
	copyErr := func() error {
		if _, err := io.Copy(output, input); err != nil {
			return err
		}
		if err := output.Sync(); err != nil {
			return err
		}
		return output.Close()
	}()
	if copyErr != nil {
		_ = output.Close()
		_ = os.Remove(path)
		return "", copyErr
	}
	if runtime.GOOS != "windows" {
		if err := os.Chmod(path, 0o600); err != nil {
			_ = os.Remove(path)
			return "", err
		}
	}
	return path, nil
}
