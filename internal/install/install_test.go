package install

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

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
