// Package install provides duplicate-safe runtime installation and harness
// discovery. Native plugin managers remain responsible for plugin lifecycle;
// this package installs the one shared executable they reference.
package install

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"time"

	"github.com/CongBao/failure-memory/internal/config"
	"github.com/CongBao/failure-memory/internal/version"
)

type HarnessState struct {
	Name                  string   `json:"name"`
	Executable            string   `json:"executable,omitempty"`
	PluginPath            string   `json:"plugin_path,omitempty"`
	PluginPaths           []string `json:"plugin_paths,omitempty"`
	PluginCount           int      `json:"plugin_count"`
	DuplicateInstallation bool     `json:"duplicate_installation"`
	Detected              bool     `json:"detected"`
	Installed             bool     `json:"plugin_installed"`
}

type Status struct {
	Version        string         `json:"version"`
	RuntimePath    string         `json:"runtime_path"`
	RuntimeReady   bool           `json:"runtime_ready"`
	SharedDataRoot string         `json:"shared_data_root"`
	Harnesses      []HarnessState `json:"harnesses"`
}

type RuntimeResult struct {
	Status      string `json:"status"`
	Version     string `json:"version"`
	RuntimePath string `json:"runtime_path"`
	SHA256      string `json:"sha256"`
}

type receipt struct {
	SchemaVersion int    `json:"schema_version"`
	Version       string `json:"version"`
	RuntimePath   string `json:"runtime_path"`
	SHA256        string `json:"sha256"`
	InstalledAt   string `json:"installed_at"`
}

func Inspect(paths config.Paths) (Status, error) {
	runtimePath, err := DefaultRuntimePath()
	if err != nil {
		return Status{}, err
	}
	states := []HarnessState{
		detectHarness("codex", "codex", codexPluginPaths()),
		detectHarness("claude-code", "claude", claudePluginPaths()),
		detectHarness("copilot-cli", "copilot", copilotPluginPaths()),
		detectHarness("cursor", "cursor-agent", cursorPluginPaths()),
	}
	return Status{
		Version:        version.Version,
		RuntimePath:    runtimePath,
		RuntimeReady:   regularExecutable(runtimePath),
		SharedDataRoot: paths.Root,
		Harnesses:      states,
	}, nil
}

func InstallRuntime(paths config.Paths) (RuntimeResult, error) {
	source, err := os.Executable()
	if err != nil {
		return RuntimeResult{}, err
	}
	source, err = filepath.EvalSymlinks(source)
	if err != nil {
		return RuntimeResult{}, err
	}
	destination, err := DefaultRuntimePath()
	if err != nil {
		return RuntimeResult{}, err
	}
	sourceDigest, err := fileSHA256(source)
	if err != nil {
		return RuntimeResult{}, err
	}
	status := "installed"
	if destinationDigest, digestErr := fileSHA256(destination); digestErr == nil &&
		destinationDigest == sourceDigest {
		status = "noop"
	} else {
		if err := config.EnsurePrivateDir(filepath.Dir(destination)); err != nil {
			return RuntimeResult{}, err
		}
		temporary, err := os.CreateTemp(filepath.Dir(destination), ".failure-memory-*")
		if err != nil {
			return RuntimeResult{}, err
		}
		temporaryPath := temporary.Name()
		defer func() { _ = os.Remove(temporaryPath) }()
		sourceFile, err := os.Open(source)
		if err != nil {
			_ = temporary.Close()
			return RuntimeResult{}, err
		}
		_, copyErr := io.Copy(temporary, sourceFile)
		closeSourceErr := sourceFile.Close()
		syncErr := temporary.Sync()
		closeErr := temporary.Close()
		if err := errors.Join(copyErr, closeSourceErr, syncErr, closeErr); err != nil {
			return RuntimeResult{}, err
		}
		if runtime.GOOS != "windows" {
			if err := os.Chmod(temporaryPath, 0o755); err != nil {
				return RuntimeResult{}, err
			}
		}
		if err := os.Rename(temporaryPath, destination); err != nil {
			return RuntimeResult{}, fmt.Errorf("publish runtime: %w", err)
		}
	}
	result := RuntimeResult{
		Status:      status,
		Version:     version.Version,
		RuntimePath: destination,
		SHA256:      sourceDigest,
	}
	if err := writeReceipt(paths.InstallReceipt, receipt{
		SchemaVersion: 1,
		Version:       version.Version,
		RuntimePath:   destination,
		SHA256:        sourceDigest,
		InstalledAt:   time.Now().UTC().Format(time.RFC3339Nano),
	}); err != nil {
		return RuntimeResult{}, err
	}
	return result, nil
}

func DefaultRuntimePath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	if runtime.GOOS == "windows" {
		base := os.Getenv("LOCALAPPDATA")
		if base == "" {
			base = filepath.Join(home, "AppData", "Local")
		}
		return filepath.Join(base, "FailureMemory", "bin", "failure-memory.exe"), nil
	}
	return filepath.Join(home, ".local", "bin", "failure-memory"), nil
}

func detectHarness(name, executable string, candidates []string) HarnessState {
	executablePath, _ := exec.LookPath(executable)
	pluginPaths := existingUniquePaths(candidates)
	state := HarnessState{
		Name:                  name,
		Executable:            executablePath,
		PluginPaths:           pluginPaths,
		PluginCount:           len(pluginPaths),
		DuplicateInstallation: len(pluginPaths) > 1,
		Detected:              executablePath != "",
		Installed:             len(pluginPaths) > 0,
	}
	if len(pluginPaths) > 0 {
		state.PluginPath = pluginPaths[0]
	}
	return state
}

func existingUniquePaths(candidates []string) []string {
	unique := map[string]string{}
	for _, candidate := range candidates {
		info, err := os.Stat(candidate)
		if err != nil || !info.IsDir() {
			continue
		}
		absolute, err := filepath.Abs(candidate)
		if err != nil {
			continue
		}
		canonical, err := filepath.EvalSymlinks(absolute)
		if err != nil {
			canonical = absolute
		}
		if existing, ok := unique[canonical]; !ok || absolute < existing {
			unique[canonical] = absolute
		}
	}
	result := make([]string, 0, len(unique))
	for _, path := range unique {
		result = append(result, path)
	}
	sort.Strings(result)
	return result
}

func codexPluginPaths() []string {
	home, _ := os.UserHomeDir()
	matches, _ := filepath.Glob(filepath.Join(
		home, ".codex", "plugins", "cache", "*", "failure-memory", "*",
	))
	return matches
}

func claudePluginPaths() []string {
	home, _ := os.UserHomeDir()
	var result []string
	for _, pattern := range []string{
		filepath.Join(home, ".claude", "plugins", "cache", "*", "failure-memory", "*"),
		filepath.Join(home, ".claude", "plugins", "failure-memory"),
	} {
		matches, _ := filepath.Glob(pattern)
		result = append(result, matches...)
	}
	return result
}

func copilotPluginPaths() []string {
	home, _ := os.UserHomeDir()
	return []string{
		filepath.Join(home, ".copilot", "installed-plugins", "_direct", "failure-memory"),
	}
}

func cursorPluginPaths() []string {
	home, _ := os.UserHomeDir()
	return []string{
		filepath.Join(home, ".cursor", "plugins", "local", "failure-memory"),
		filepath.Join(home, ".cursor", "plugins", "failure-memory"),
	}
}

func regularExecutable(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular() &&
		(runtime.GOOS == "windows" || info.Mode().Perm()&0o111 != 0)
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

func writeReceipt(path string, value receipt) error {
	if err := config.EnsurePrivateDir(filepath.Dir(path)); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".receipts-*")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer func() { _ = os.Remove(temporaryPath) }()
	encoder := json.NewEncoder(temporary)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(value); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if runtime.GOOS != "windows" {
		if err := os.Chmod(temporaryPath, 0o600); err != nil {
			return err
		}
	}
	return os.Rename(temporaryPath, path)
}
