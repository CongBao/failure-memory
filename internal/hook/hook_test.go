package hook

import (
	"strings"
	"testing"
)

func TestPromptHookOnlyInjectsForLikelyCorrection(t *testing.T) {
	ordinary, emit, err := Run(
		"codex",
		"user-prompt-submit",
		strings.NewReader(`{"prompt":"Please add YAML output too."}`),
	)
	if err != nil || emit || ordinary.HookSpecificOutput != nil {
		t.Fatalf("ordinary prompt emitted guidance: %#v, %v, %v", ordinary, emit, err)
	}
	corrective, emit, err := Run(
		"codex",
		"user-prompt-submit",
		strings.NewReader(`{"prompt":"你刚才没有做到之前明确要求的兼容性检查，又失败了。"}`),
	)
	if err != nil || !emit || corrective.HookSpecificOutput == nil {
		t.Fatalf("correction missed: %#v, %v, %v", corrective, emit, err)
	}
	if !strings.Contains(corrective.HookSpecificOutput.AdditionalContext, "retry once only") ||
		!strings.Contains(corrective.HookSpecificOutput.AdditionalContext, "never after an ambiguous") {
		t.Fatalf("correction guidance lacks bounded retry safety: %#v", corrective)
	}
}

func TestMalformedHookInputFailsOpen(t *testing.T) {
	_, emit, err := Run("claude-code", "user-prompt-submit", strings.NewReader("{"))
	if err != nil || emit {
		t.Fatalf("malformed hook must fail open: emit=%v err=%v", emit, err)
	}
}
