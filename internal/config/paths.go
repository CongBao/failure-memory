package config

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

const identityKeySize = 32

type Paths struct {
	Root           string
	EventStore     string
	RetrievalIndex string
	EmbeddingModel string
	IdentityKey    string
	InstallReceipt string
}

func ResolvePaths() (Paths, error) {
	root := strings.TrimSpace(os.Getenv("FAILURE_MEMORY_HOME"))
	if root == "" {
		var err error
		root, err = defaultRoot()
		if err != nil {
			return Paths{}, err
		}
	}
	root, err := filepath.Abs(root)
	if err != nil {
		return Paths{}, fmt.Errorf("resolve data root: %w", err)
	}
	return Paths{
		Root:           root,
		EventStore:     filepath.Join(root, "adapters", "event-store", "sqlite", "v1", "events.sqlite3"),
		RetrievalIndex: filepath.Join(root, "adapters", "retrieval", "sqlite-vec", "v1", "index.sqlite3"),
		EmbeddingModel: filepath.Join(
			root,
			"adapters",
			"embedding",
			"hugot",
			"multilingual-e5-small",
			"761b726dd34fb83930e26aab4e9ac3899aa1fa78",
		),
		IdentityKey:    filepath.Join(root, "bootstrap", "identity.key"),
		InstallReceipt: filepath.Join(root, "installs", "receipts.json"),
	}, nil
}

func defaultRoot() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("resolve user home: %w", err)
	}
	switch runtime.GOOS {
	case "darwin":
		return filepath.Join(home, "Library", "Application Support", "failure-memory"), nil
	case "windows":
		base := strings.TrimSpace(os.Getenv("LOCALAPPDATA"))
		if base == "" {
			base = filepath.Join(home, "AppData", "Local")
		}
		return filepath.Join(base, "FailureMemory"), nil
	default:
		base := strings.TrimSpace(os.Getenv("XDG_DATA_HOME"))
		if base == "" {
			base = filepath.Join(home, ".local", "share")
		}
		return filepath.Join(base, "failure-memory"), nil
	}
}

func EnsurePrivateDir(path string) error {
	if err := os.MkdirAll(path, 0o700); err != nil {
		return err
	}
	if runtime.GOOS != "windows" {
		if err := os.Chmod(path, 0o700); err != nil {
			return err
		}
	}
	return nil
}

func LoadOrCreateIdentityKey(path string) ([]byte, error) {
	data, err := os.ReadFile(path)
	if err == nil {
		if len(data) != identityKeySize {
			return nil, errors.New("identity key has invalid length")
		}
		return data, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	if err := EnsurePrivateDir(filepath.Dir(path)); err != nil {
		return nil, err
	}
	data = make([]byte, identityKeySize)
	if _, err := rand.Read(data); err != nil {
		return nil, fmt.Errorf("generate identity key: %w", err)
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		if errors.Is(err, os.ErrExist) {
			return LoadOrCreateIdentityKey(path)
		}
		return nil, err
	}
	if _, writeErr := file.Write(data); writeErr != nil {
		_ = file.Close()
		return nil, writeErr
	}
	if err := file.Close(); err != nil {
		return nil, err
	}
	return data, nil
}

func Fingerprint(key []byte, value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(value))
	return hex.EncodeToString(mac.Sum(nil)[:16])
}

func RuntimeContext(paths Paths, transport string) (harness, workspace, session string, err error) {
	key, err := LoadOrCreateIdentityKey(paths.IdentityKey)
	if err != nil {
		return "", "", "", err
	}
	harness = strings.TrimSpace(os.Getenv("FAILURE_MEMORY_HARNESS"))
	if harness == "" {
		harness = "generic"
	}
	cwd, cwdErr := os.Getwd()
	if cwdErr != nil {
		return "", "", "", cwdErr
	}
	sessionRaw := strings.TrimSpace(os.Getenv("FAILURE_MEMORY_SESSION_ID"))
	workspace = Fingerprint(key, filepath.Clean(cwd))
	session = Fingerprint(key, sessionRaw)
	if session == "" {
		session = Fingerprint(key, harness+":"+transport+":"+fmt.Sprint(os.Getpid()))
	}
	return harness, workspace, session, nil
}
