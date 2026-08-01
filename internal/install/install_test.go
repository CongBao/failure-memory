package install

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
)

type recordedCommand struct {
	name string
	args []string
}

type fakeExecutor struct {
	commands []recordedCommand
	failures map[string]error
	outputs  map[string]string
}

func (executor *fakeExecutor) Run(
	_ context.Context, name string, args ...string,
) (string, error) {
	command := strings.Join(append([]string{name}, args...), " ")
	executor.commands = append(executor.commands, recordedCommand{name: name, args: args})
	return executor.outputs[command], executor.failures[command]
}

func fakeEnvironment(executor commandExecutor, paths map[string]string) installEnvironment {
	return installEnvironment{
		executor: executor,
		lookup: func(name string) (string, error) {
			if path := paths[name]; path != "" {
				return path, nil
			}
			return "", errors.New("not found")
		},
		home:                   func() (string, error) { return "/test-home", nil },
		goos:                   "linux",
		copilotDirectInstalled: func() bool { return false },
	}
}

func TestExistingUniquePathsCollapsesRepeatedAndSymlinkedInstallations(t *testing.T) {
	root := t.TempDir()
	plugin := filepath.Join(root, "plugin")
	if err := os.Mkdir(plugin, 0o700); err != nil {
		t.Fatal(err)
	}
	candidates := []string{plugin, plugin, filepath.Join(root, "missing")}
	if runtime.GOOS != "windows" {
		alias := filepath.Join(root, "plugin-alias")
		if err := os.Symlink(plugin, alias); err != nil {
			t.Fatal(err)
		}
		candidates = append(candidates, alias)
	}
	paths := existingUniquePaths(candidates)
	if len(paths) != 1 {
		t.Fatalf("unique plugin paths = %#v, want one installation", paths)
	}
}

func TestDetectHarnessReportsDistinctDuplicateInstallations(t *testing.T) {
	root := t.TempDir()
	first := filepath.Join(root, "first")
	second := filepath.Join(root, "second")
	for _, path := range []string{first, second} {
		if err := os.Mkdir(path, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	state := detectHarness("test", "missing-failure-memory-test-command", []string{
		first,
		second,
	})
	if !state.Installed || state.PluginCount != 2 || !state.DuplicateInstallation {
		t.Fatalf("duplicate installation was not reported: %#v", state)
	}
}

func TestNormalizeHarnessesSupportsAliasesDeduplicatesAndKeepsStableOrder(t *testing.T) {
	harnesses, auto, err := normalizeHarnesses([]string{"copilot,codex", "claude", "codex"})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"codex", "claude-code", "copilot-cli"}
	if auto || !reflect.DeepEqual(harnesses, want) {
		t.Fatalf("normalizeHarnesses() = %#v, %v; want %#v, false", harnesses, auto, want)
	}
}

func TestNormalizeHarnessesRejectsAutoCombinedWithNamedHarness(t *testing.T) {
	_, _, err := normalizeHarnesses([]string{"auto", "codex"})
	if err == nil || !strings.Contains(err.Error(), "cannot be combined") {
		t.Fatalf("normalizeHarnesses() error = %v", err)
	}
}

func TestDefaultRuntimePathAcceptsOnlyAnAbsoluteOverride(t *testing.T) {
	expected := filepath.Join(t.TempDir(), "managed", "bin", "failure-memory")
	t.Setenv("FAILURE_MEMORY_RUNTIME_PATH", expected)
	path, err := DefaultRuntimePath()
	if err != nil || path != filepath.Clean(expected) {
		t.Fatalf("DefaultRuntimePath() = %q, %v", path, err)
	}
	t.Setenv("FAILURE_MEMORY_RUNTIME_PATH", "relative/failure-memory")
	if _, err := DefaultRuntimePath(); err == nil {
		t.Fatal("relative runtime override was accepted")
	}
}

func TestInstallPluginsAutoTargetsOnlyDetectedHarnesses(t *testing.T) {
	codexAdd := "/tools/codex plugin marketplace add " + MarketplaceSource + " --ref main --json"
	executor := &fakeExecutor{
		failures: map[string]error{},
		outputs:  map[string]string{codexAdd: `{"alreadyAdded":false}`},
	}
	results, err := installPlugins(context.Background(), nil, "/runtime/failure-memory", fakeEnvironment(executor, map[string]string{
		"codex":   "/tools/codex",
		"copilot": "/tools/copilot",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if len(results) != 2 || results[0].Harness != "codex" || results[1].Harness != "copilot-cli" {
		t.Fatalf("results = %#v", results)
	}
	want := []recordedCommand{
		{name: "/tools/codex", args: []string{"plugin", "marketplace", "add", MarketplaceSource, "--ref", "main", "--json"}},
		{name: "/tools/codex", args: []string{"plugin", "add", PluginID, "--json"}},
		{name: "/tools/codex", args: []string{"mcp", "remove", MarketplaceName}},
		{name: "/tools/codex", args: []string{
			"mcp", "add", "--env", "FAILURE_MEMORY_HARNESS=codex", MarketplaceName,
			"--", "/runtime/failure-memory", "mcp", "--stdio",
		}},
		{name: "/tools/copilot", args: []string{"plugin", "marketplace", "add", MarketplaceSource}},
		{name: "/tools/copilot", args: []string{"plugin", "install", PluginID}},
		{name: "/tools/copilot", args: []string{"mcp", "remove", MarketplaceName}},
		{name: "/tools/copilot", args: []string{
			"mcp", "add", MarketplaceName, "--env", "FAILURE_MEMORY_HARNESS=copilot-cli",
			"--timeout", "10000", "--", "/runtime/failure-memory", "mcp", "--stdio",
		}},
	}
	if !reflect.DeepEqual(executor.commands, want) {
		t.Fatalf("commands = %#v, want %#v", executor.commands, want)
	}
}

func TestInstallCopilotToleratesAlreadyRegisteredMarketplace(t *testing.T) {
	add := "/tools/copilot plugin marketplace add " + MarketplaceSource
	executor := &fakeExecutor{
		failures: map[string]error{add: errors.New("exit status 1")},
		outputs:  map[string]string{add: `Marketplace "failure-memory" already registered`},
	}
	result, err := installHarnessPlugin(
		context.Background(), "copilot-cli", "/tools/copilot", "/runtime/failure-memory",
		fakeEnvironment(executor, nil),
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "installed_or_updated" || !result.MCPConfigured || len(executor.commands) != 5 {
		t.Fatalf("result = %#v, commands = %#v", result, executor.commands)
	}
}

func TestInstallCodexRefreshesAnExistingMarketplace(t *testing.T) {
	add := "/tools/codex plugin marketplace add " + MarketplaceSource + " --ref main --json"
	executor := &fakeExecutor{
		failures: map[string]error{},
		outputs:  map[string]string{add: `{"alreadyAdded":true}`},
	}
	result, err := installHarnessPlugin(
		context.Background(), "codex", "/tools/codex", "/runtime/failure-memory",
		fakeEnvironment(executor, nil),
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "installed_or_updated" || !result.MCPConfigured || len(executor.commands) != 5 {
		t.Fatalf("result = %#v, commands = %#v", result, executor.commands)
	}
	if !reflect.DeepEqual(executor.commands[1], recordedCommand{
		name: "/tools/codex",
		args: []string{"plugin", "marketplace", "upgrade", MarketplaceName, "--json"},
	}) {
		t.Fatalf("commands = %#v", executor.commands)
	}
}

func TestInstallPluginReportsUnexpectedMarketplaceFailure(t *testing.T) {
	add := "/tools/claude plugin marketplace add " + MarketplaceSource
	executor := &fakeExecutor{
		failures: map[string]error{add: errors.New("network unavailable")},
		outputs:  map[string]string{add: "network unavailable"},
	}
	result, err := installHarnessPlugin(
		context.Background(), "claude-code", "/tools/claude", "/runtime/failure-memory",
		fakeEnvironment(executor, nil),
	)
	if err == nil || result.Status != "failed" || len(executor.commands) != 1 {
		t.Fatalf("result = %#v, error = %v, commands = %#v", result, err, executor.commands)
	}
}

func TestCursorReturnsOneExplicitManualStep(t *testing.T) {
	executor := &fakeExecutor{failures: map[string]error{}, outputs: map[string]string{}}
	environment := fakeEnvironment(executor, nil)
	environment.home = func() (string, error) { return t.TempDir(), nil }
	result, err := installHarnessPlugin(
		context.Background(), "cursor", "/tools/cursor", "/runtime/failure-memory",
		environment,
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "manual_action_required" || !result.MCPConfigured || !strings.Contains(result.Message, "/add-plugin") {
		t.Fatalf("result = %#v", result)
	}
	if len(executor.commands) != 0 {
		t.Fatalf("Cursor must not run an invented installer command: %#v", executor.commands)
	}
}

func TestInstallCopilotMigratesDeprecatedDirectInstallation(t *testing.T) {
	executor := &fakeExecutor{failures: map[string]error{}, outputs: map[string]string{}}
	environment := fakeEnvironment(executor, nil)
	environment.copilotDirectInstalled = func() bool { return true }
	result, err := installHarnessPlugin(
		context.Background(), "copilot-cli", "/tools/copilot", "/runtime/failure-memory", environment,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(result.Message, "replaced") {
		t.Fatalf("result = %#v", result)
	}
	if len(executor.commands) != 5 || !reflect.DeepEqual(executor.commands[0], recordedCommand{
		name: "/tools/copilot", args: []string{"plugin", "uninstall", "failure-memory"},
	}) {
		t.Fatalf("commands = %#v", executor.commands)
	}
}

func TestWriteCursorMCPPreservesUnrelatedServersAndUsesAbsoluteRuntime(t *testing.T) {
	home := t.TempDir()
	path := filepath.Join(home, ".cursor", "mcp.json")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`{
		"mcpServers": {
			"other": {"command": "/other/server", "args": []}
		},
		"unrelated": true
	}`), 0o600); err != nil {
		t.Fatal(err)
	}
	runtimePath := filepath.Join(home, "bin", "failure-memory")
	if err := writeCursorMCP(path, runtimePath); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var configuration map[string]any
	if err := json.Unmarshal(data, &configuration); err != nil {
		t.Fatal(err)
	}
	servers := configuration["mcpServers"].(map[string]any)
	if servers["other"] == nil || configuration["unrelated"] != true {
		t.Fatalf("unrelated Cursor configuration was changed: %#v", configuration)
	}
	failureMemory := servers[MarketplaceName].(map[string]any)
	if failureMemory["command"] != runtimePath {
		t.Fatalf("Failure Memory command = %#v", failureMemory["command"])
	}
}

func TestMCPProjectionToleratesAFirstInstallation(t *testing.T) {
	remove := "/tools/codex mcp remove " + MarketplaceName
	executor := &fakeExecutor{
		failures: map[string]error{remove: errors.New("exit status 1")},
		outputs:  map[string]string{remove: "MCP server not found"},
	}
	err := configureHarnessMCP(
		context.Background(),
		"codex",
		"/tools/codex",
		"/absolute/failure-memory",
		fakeEnvironment(executor, nil),
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(executor.commands) != 2 {
		t.Fatalf("MCP projection commands = %#v", executor.commands)
	}
}
