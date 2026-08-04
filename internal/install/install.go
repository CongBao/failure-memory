// Package install provides duplicate-safe runtime and harness plugin
// installation. Every harness plugin references the same native executable and
// owner-private data store.
package install

import (
	"context"
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
	"strings"
	"time"

	"github.com/CongBao/failure-memory/internal/config"
	"github.com/CongBao/failure-memory/internal/version"
)

const (
	MarketplaceName   = "failure-memory"
	MarketplaceSource = "CongBao/failure-memory"
	PluginID          = "failure-memory@failure-memory"
)

var supportedHarnesses = []string{"codex", "claude-code", "copilot-cli", "cursor"}

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

type PluginResult struct {
	Harness         string `json:"harness"`
	Status          string `json:"status"`
	Executable      string `json:"executable,omitempty"`
	PluginID        string `json:"plugin_id,omitempty"`
	MCPConfigured   bool   `json:"mcp_configured"`
	Message         string `json:"message,omitempty"`
	RestartRequired bool   `json:"restart_required"`
}

type AllResult struct {
	Runtime RuntimeResult  `json:"runtime"`
	Plugins []PluginResult `json:"plugins"`
}

type commandExecutor interface {
	Run(context.Context, string, ...string) (string, error)
}

type osCommandExecutor struct{}

func (osCommandExecutor) Run(ctx context.Context, name string, args ...string) (string, error) {
	command := exec.CommandContext(ctx, name, args...)
	output, err := command.CombinedOutput()
	if err != nil {
		message := strings.TrimSpace(string(output))
		if message == "" {
			return "", fmt.Errorf("%s: %w", filepath.Base(name), err)
		}
		return message, fmt.Errorf("%s: %w: %s", filepath.Base(name), err, message)
	}
	return strings.TrimSpace(string(output)), nil
}

type installEnvironment struct {
	executor               commandExecutor
	lookup                 func(string) (string, error)
	home                   func() (string, error)
	goos                   string
	copilotDirectInstalled func() bool
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
	environment := defaultInstallEnvironment()
	states := []HarnessState{
		detectHarnessPath("codex", findHarnessExecutable("codex", environment), codexPluginPaths()),
		detectHarnessPath("claude-code", findHarnessExecutable("claude-code", environment), claudePluginPaths()),
		detectHarnessPath("copilot-cli", findHarnessExecutable("copilot-cli", environment), copilotPluginPaths()),
		detectHarnessPath("cursor", findHarnessExecutable("cursor", environment), cursorPluginPaths()),
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

// InstallAll installs or updates the one shared runtime, then installs the
// public plugin through each selected harness's native plugin manager.
func InstallAll(
	ctx context.Context, paths config.Paths, requestedHarnesses []string,
) (AllResult, error) {
	runtimeResult, err := InstallRuntime(paths)
	if err != nil {
		return AllResult{}, err
	}
	plugins, err := installPlugins(
		ctx, requestedHarnesses, runtimeResult.RuntimePath, defaultInstallEnvironment(),
	)
	return AllResult{Runtime: runtimeResult, Plugins: plugins}, err
}

// InstallPlugins installs or updates the public plugin without creating a
// second runtime or data store. Use "auto" to target every detected harness.
func InstallPlugins(ctx context.Context, requestedHarnesses []string) ([]PluginResult, error) {
	runtimePath, err := DefaultRuntimePath()
	if err != nil {
		return nil, err
	}
	if !regularExecutable(runtimePath) {
		return nil, errors.New("shared runtime is not installed; run failure-memory install all")
	}
	return installPlugins(ctx, requestedHarnesses, runtimePath, defaultInstallEnvironment())
}

func defaultInstallEnvironment() installEnvironment {
	return installEnvironment{
		executor:               osCommandExecutor{},
		lookup:                 exec.LookPath,
		home:                   os.UserHomeDir,
		goos:                   runtime.GOOS,
		copilotDirectInstalled: copilotDirectPluginInstalled,
	}
}

func installPlugins(
	ctx context.Context,
	requestedHarnesses []string,
	runtimePath string,
	environment installEnvironment,
) ([]PluginResult, error) {
	harnesses, auto, err := normalizeHarnesses(requestedHarnesses)
	if err != nil {
		return nil, err
	}
	results := make([]PluginResult, 0, len(harnesses))
	var failures []error
	for _, harness := range harnesses {
		executable := findHarnessExecutable(harness, environment)
		if executable == "" {
			if auto {
				continue
			}
			results = append(results, PluginResult{
				Harness: harness,
				Status:  "not_detected",
				Message: harness + " is not installed or its command is not discoverable",
			})
			failures = append(failures, fmt.Errorf("%s was not detected", harness))
			continue
		}
		result, installErr := installHarnessPlugin(
			ctx, harness, executable, runtimePath, environment,
		)
		results = append(results, result)
		if installErr != nil {
			failures = append(failures, installErr)
		}
	}
	if auto && len(results) == 0 {
		return results, errors.New("no supported agent application was detected; rerun with --harness <name>")
	}
	return results, errors.Join(failures...)
}

func normalizeHarnesses(requested []string) ([]string, bool, error) {
	if len(requested) == 0 {
		requested = []string{"auto"}
	}
	aliases := map[string]string{
		"codex": "codex", "claude": "claude-code", "claude-code": "claude-code",
		"copilot": "copilot-cli", "copilot-cli": "copilot-cli", "cursor": "cursor",
	}
	auto := false
	unique := map[string]bool{}
	for _, value := range requested {
		for _, item := range strings.Split(value, ",") {
			item = strings.ToLower(strings.TrimSpace(item))
			if item == "auto" {
				auto = true
				continue
			}
			canonical, ok := aliases[item]
			if !ok {
				return nil, false, fmt.Errorf(
					"unsupported harness %q (choose codex, claude, copilot, cursor, or auto)",
					item,
				)
			}
			unique[canonical] = true
		}
	}
	if auto && len(unique) != 0 {
		return nil, false, errors.New("auto cannot be combined with named harnesses")
	}
	if auto {
		return append([]string(nil), supportedHarnesses...), true, nil
	}
	if len(unique) == 0 {
		return nil, false, errors.New("at least one harness is required")
	}
	result := make([]string, 0, len(unique))
	for _, harness := range supportedHarnesses {
		if unique[harness] {
			result = append(result, harness)
		}
	}
	return result, false, nil
}

func installHarnessPlugin(
	ctx context.Context,
	harness string,
	executable string,
	runtimePath string,
	environment installEnvironment,
) (PluginResult, error) {
	result := PluginResult{
		Harness:         harness,
		Executable:      executable,
		PluginID:        PluginID,
		RestartRequired: true,
	}
	var err error
	switch harness {
	case "codex":
		err = installCodex(ctx, executable, environment.executor)
	case "claude-code":
		err = installClaude(ctx, executable, environment.executor)
	case "copilot-cli":
		migrated := false
		migrated, err = installCopilot(ctx, executable, environment)
		if migrated {
			result.Message = "replaced the deprecated direct install with the marketplace plugin"
		}
	case "cursor":
		result.Status = "manual_action_required"
		result.Message = "MCP is configured; in Cursor, run /add-plugin CongBao/failure-memory for the skills and hook"
	default:
		err = fmt.Errorf("unsupported harness %q", harness)
	}
	if err != nil {
		result.Status = "failed"
		result.Message = err.Error()
		return result, fmt.Errorf("install %s plugin: %w", harness, err)
	}
	if harness != "cursor" {
		result.Status = "installed_or_updated"
	}
	if err := configureHarnessMCP(
		ctx, harness, executable, runtimePath, environment,
	); err != nil {
		result.Status = "failed"
		result.Message = err.Error()
		return result, fmt.Errorf("configure %s MCP server: %w", harness, err)
	}
	result.MCPConfigured = true
	return result, nil
}

func configureHarnessMCP(
	ctx context.Context,
	harness string,
	executable string,
	runtimePath string,
	environment installEnvironment,
) error {
	switch harness {
	case "codex":
		return replaceCommandMCP(
			ctx,
			environment.executor,
			executable,
			[]string{"mcp", "remove", MarketplaceName},
			[]string{
				"mcp", "add", "--env", "FAILURE_MEMORY_HARNESS=codex",
				MarketplaceName, "--", runtimePath, "mcp", "--stdio",
			},
		)
	case "claude-code":
		return replaceCommandMCP(
			ctx,
			environment.executor,
			executable,
			[]string{"mcp", "remove", MarketplaceName},
			[]string{
				"mcp", "add", "--env", "FAILURE_MEMORY_HARNESS=claude-code",
				"--transport", "stdio", "--scope", "user",
				MarketplaceName, "--", runtimePath, "mcp", "--stdio",
			},
		)
	case "copilot-cli":
		return replaceCommandMCP(
			ctx,
			environment.executor,
			executable,
			[]string{"mcp", "remove", MarketplaceName},
			[]string{
				"mcp", "add", MarketplaceName,
				"--env", "FAILURE_MEMORY_HARNESS=copilot-cli",
				"--timeout", "10000", "--", runtimePath, "mcp", "--stdio",
			},
		)
	case "cursor":
		home, err := environment.home()
		if err != nil {
			return err
		}
		return writeCursorMCP(filepath.Join(home, ".cursor", "mcp.json"), runtimePath)
	default:
		return fmt.Errorf("unsupported harness %q", harness)
	}
}

func replaceCommandMCP(
	ctx context.Context,
	executor commandExecutor,
	executable string,
	removeArgs []string,
	addArgs []string,
) error {
	output, err := executor.Run(ctx, executable, removeArgs...)
	if err != nil && !missingMCP(output) {
		return fmt.Errorf("remove previous Failure Memory MCP projection: %w", err)
	}
	if _, err := executor.Run(ctx, executable, addArgs...); err != nil {
		return fmt.Errorf("add Failure Memory MCP projection: %w", err)
	}
	return nil
}

func missingMCP(output string) bool {
	value := strings.ToLower(output)
	return strings.Contains(value, "not found") ||
		strings.Contains(value, "does not exist") ||
		strings.Contains(value, "no server") ||
		strings.Contains(value, "not configured")
}

func writeCursorMCP(path string, runtimePath string) error {
	if err := config.EnsurePrivateDir(filepath.Dir(path)); err != nil {
		return err
	}
	configuration := map[string]any{}
	if data, err := os.ReadFile(path); err == nil {
		if err := json.Unmarshal(data, &configuration); err != nil {
			return fmt.Errorf("read Cursor MCP configuration: %w", err)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	servers, _ := configuration["mcpServers"].(map[string]any)
	if servers == nil {
		servers = map[string]any{}
		configuration["mcpServers"] = servers
	}
	servers[MarketplaceName] = map[string]any{
		"command": runtimePath,
		"args":    []string{"mcp", "--stdio"},
		"env": map[string]string{
			"FAILURE_MEMORY_HARNESS": "cursor",
		},
	}
	return writeJSONAtomically(path, configuration)
}

func writeJSONAtomically(path string, value any) error {
	temporary, err := os.CreateTemp(filepath.Dir(path), ".failure-memory-config-*")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer func() { _ = os.Remove(temporaryPath) }()
	encoder := json.NewEncoder(temporary)
	encoder.SetIndent("", "  ")
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

func installCodex(ctx context.Context, executable string, executor commandExecutor) error {
	output, err := executor.Run(
		ctx, executable, "plugin", "marketplace", "add", MarketplaceSource, "--ref", "main", "--json",
	)
	if err != nil {
		if !codexMarketplaceSourceConflict(output) {
			return err
		}
		if _, err := executor.Run(
			ctx, executable, "plugin", "marketplace", "upgrade", MarketplaceName, "--json",
		); err != nil {
			return err
		}
		_, err = executor.Run(ctx, executable, "plugin", "add", PluginID, "--json")
		return err
	}
	var addResult struct {
		AlreadyAdded bool `json:"alreadyAdded"`
	}
	if jsonErr := json.Unmarshal([]byte(output), &addResult); jsonErr != nil || addResult.AlreadyAdded {
		if _, err := executor.Run(
			ctx, executable, "plugin", "marketplace", "upgrade", MarketplaceName, "--json",
		); err != nil {
			return err
		}
	}
	_, err = executor.Run(ctx, executable, "plugin", "add", PluginID, "--json")
	return err
}

func codexMarketplaceSourceConflict(output string) bool {
	value := strings.ToLower(output)
	return strings.Contains(value, "marketplace") &&
		strings.Contains(value, "already added") &&
		strings.Contains(value, "different source")
}

func installClaude(ctx context.Context, executable string, executor commandExecutor) error {
	output, addErr := executor.Run(
		ctx, executable, "plugin", "marketplace", "add", MarketplaceSource,
	)
	registered := addErr != nil && alreadyRegistered(output)
	if addErr != nil && !registered {
		return addErr
	}
	if registered {
		if _, err := executor.Run(
			ctx, executable, "plugin", "marketplace", "update", MarketplaceName,
		); err != nil {
			return err
		}
	}
	_, err := executor.Run(ctx, executable, "plugin", "install", PluginID, "--scope", "user")
	return err
}

func installCopilot(
	ctx context.Context, executable string, environment installEnvironment,
) (bool, error) {
	migrated := environment.copilotDirectInstalled != nil &&
		environment.copilotDirectInstalled()
	if migrated {
		if _, err := environment.executor.Run(
			ctx, executable, "plugin", "uninstall", "failure-memory",
		); err != nil {
			return false, err
		}
	}
	output, addErr := environment.executor.Run(
		ctx, executable, "plugin", "marketplace", "add", MarketplaceSource,
	)
	registered := addErr != nil && alreadyRegistered(output)
	if addErr != nil && !registered {
		return migrated, addErr
	}
	if registered {
		if _, err := environment.executor.Run(
			ctx, executable, "plugin", "marketplace", "update", MarketplaceName,
		); err != nil {
			return migrated, err
		}
	}
	_, err := environment.executor.Run(ctx, executable, "plugin", "install", PluginID)
	return migrated, err
}

func alreadyRegistered(output string) bool {
	value := strings.ToLower(output)
	return strings.Contains(value, "already registered") ||
		strings.Contains(value, "already added") ||
		strings.Contains(value, "already exists")
}

func findHarnessExecutable(harness string, environment installEnvironment) string {
	commands := map[string][]string{
		"codex":       {"codex"},
		"claude-code": {"claude"},
		"copilot-cli": {"copilot"},
		"cursor":      {"cursor-agent", "cursor"},
	}
	for _, command := range commands[harness] {
		if path, err := environment.lookup(command); err == nil && path != "" {
			return path
		}
	}
	if environment.goos != "darwin" {
		return ""
	}
	fallbacks := map[string][]string{
		"codex": {
			"/Applications/ChatGPT.app/Contents/Resources/codex",
			"/Applications/Codex.app/Contents/Resources/codex",
		},
		"cursor": {
			"/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
		},
	}
	for _, candidate := range fallbacks[harness] {
		if regularExecutable(candidate) {
			return candidate
		}
	}
	return ""
}

func DefaultRuntimePath() (string, error) {
	if override := strings.TrimSpace(os.Getenv("FAILURE_MEMORY_RUNTIME_PATH")); override != "" {
		if !filepath.IsAbs(override) {
			return "", errors.New("FAILURE_MEMORY_RUNTIME_PATH must be an absolute path")
		}
		return filepath.Clean(override), nil
	}
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
	return detectHarnessPath(name, executablePath, candidates)
}

func detectHarnessPath(name, executablePath string, candidates []string) HarnessState {
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
	result, _ := filepath.Glob(filepath.Join(
		home, ".copilot", "installed-plugins", "_direct", "*failure-memory",
	))
	marketplaceMatches, _ := filepath.Glob(filepath.Join(
		home, ".copilot", "installed-plugins", "*", "failure-memory",
	))
	return append(result, marketplaceMatches...)
}

func copilotDirectPluginInstalled() bool {
	home, err := os.UserHomeDir()
	if err != nil {
		return false
	}
	matches, _ := filepath.Glob(filepath.Join(
		home, ".copilot", "installed-plugins", "_direct", "*failure-memory",
	))
	for _, path := range matches {
		if info, statErr := os.Stat(path); statErr == nil && info.IsDir() {
			return true
		}
	}
	return false
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
