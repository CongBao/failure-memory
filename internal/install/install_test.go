package install

import (
	"context"
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

func TestInstallPluginsAutoTargetsOnlyDetectedHarnesses(t *testing.T) {
	codexAdd := "/tools/codex plugin marketplace add " + MarketplaceSource + " --ref main --json"
	executor := &fakeExecutor{
		failures: map[string]error{},
		outputs:  map[string]string{codexAdd: `{"alreadyAdded":false}`},
	}
	results, err := installPlugins(context.Background(), nil, fakeEnvironment(executor, map[string]string{
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
		{name: "/tools/copilot", args: []string{"plugin", "marketplace", "add", MarketplaceSource}},
		{name: "/tools/copilot", args: []string{"plugin", "install", PluginID}},
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
		context.Background(), "copilot-cli", "/tools/copilot",
		fakeEnvironment(executor, nil),
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "installed_or_updated" || len(executor.commands) != 3 {
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
		context.Background(), "codex", "/tools/codex",
		fakeEnvironment(executor, nil),
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "installed_or_updated" || len(executor.commands) != 3 {
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
		context.Background(), "claude-code", "/tools/claude",
		fakeEnvironment(executor, nil),
	)
	if err == nil || result.Status != "failed" || len(executor.commands) != 1 {
		t.Fatalf("result = %#v, error = %v, commands = %#v", result, err, executor.commands)
	}
}

func TestCursorReturnsOneExplicitManualStep(t *testing.T) {
	executor := &fakeExecutor{failures: map[string]error{}, outputs: map[string]string{}}
	result, err := installHarnessPlugin(
		context.Background(), "cursor", "/tools/cursor",
		fakeEnvironment(executor, nil),
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "manual_action_required" || !strings.Contains(result.Message, "/add-plugin") {
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
		context.Background(), "copilot-cli", "/tools/copilot", environment,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(result.Message, "replaced") {
		t.Fatalf("result = %#v", result)
	}
	if len(executor.commands) != 3 || !reflect.DeepEqual(executor.commands[0], recordedCommand{
		name: "/tools/copilot", args: []string{"plugin", "uninstall", "failure-memory"},
	}) {
		t.Fatalf("commands = %#v", executor.commands)
	}
}
