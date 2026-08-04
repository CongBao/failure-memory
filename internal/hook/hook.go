// Package hook implements bounded, non-persistent harness hooks. Hooks never
// open the memory store and never call a network or model.
package hook

import (
	"encoding/json"
	"errors"
	"io"
	"strings"
	"unicode"
)

const MaxInputBytes = 1 << 20

const sessionGuidance = "Failure Memory: recall once before risky recurring work; record only evidence-backed prior-invariant failures. New requirements, clarifications, preferences, and ordinary refinement are not failures."

const correctionGuidance = "This message may challenge an earlier outcome. Use record-agent-failure only for an evidenced prior-invariant failure and separate new requirements. Normally call once; retry once only after an explicit retryable or pre-execution schema-validation response, never after an ambiguous failure."

type Output struct {
	HookSpecificOutput *HookSpecificOutput `json:"hookSpecificOutput,omitempty"`
	AdditionalContext  string              `json:"additionalContext,omitempty"`
	AdditionalContext2 string              `json:"additional_context,omitempty"`
	ModifiedPrompt     string              `json:"modifiedTransformedPrompt,omitempty"`
}

type HookSpecificOutput struct {
	HookEventName     string `json:"hookEventName"`
	AdditionalContext string `json:"additionalContext"`
}

func Run(harness, event string, reader io.Reader) (Output, bool, error) {
	if !supportedHarness(harness) {
		return Output{}, false, errors.New("unsupported harness")
	}
	if event != "session-start" && event != "user-prompt-submit" &&
		event != "user-prompt-transformed" {
		return Output{}, false, errors.New("unsupported hook event")
	}
	payload, err := readObject(reader)
	if err != nil {
		// A hook must fail open; malformed host input should never block work.
		return Output{}, false, nil
	}
	guidance := sessionGuidance
	eventName := "SessionStart"
	if event == "user-prompt-submit" {
		prompt := promptText(payload)
		if !looksCorrective(prompt) {
			return Output{}, false, nil
		}
		guidance = correctionGuidance
		eventName = "UserPromptSubmit"
	}
	if event == "user-prompt-transformed" {
		if harness != "copilot-cli" && harness != "copilot-vscode" {
			return Output{}, false, nil
		}
		prompt := promptText(payload)
		if !looksCorrective(prompt) {
			return Output{}, false, nil
		}
		transformed, _ := payload["transformedPrompt"].(string)
		if transformed == "" || len(transformed) > 64<<10 {
			return Output{}, false, nil
		}
		return Output{
			ModifiedPrompt: correctionGuidance + "\n\n" + transformed,
		}, true, nil
	}
	switch harness {
	case "codex", "claude-code":
		return Output{
			HookSpecificOutput: &HookSpecificOutput{
				HookEventName:     eventName,
				AdditionalContext: guidance,
			},
		}, true, nil
	case "copilot-cli", "copilot-vscode":
		return Output{AdditionalContext: guidance}, true, nil
	default:
		return Output{AdditionalContext2: guidance}, true, nil
	}
}

func readObject(reader io.Reader) (map[string]any, error) {
	decoder := json.NewDecoder(io.LimitReader(reader, MaxInputBytes+1))
	var value map[string]any
	if err := decoder.Decode(&value); err != nil {
		if errors.Is(err, io.EOF) {
			return map[string]any{}, nil
		}
		return nil, err
	}
	return value, nil
}

func promptText(value map[string]any) string {
	keys := []string{
		"prompt", "user_prompt", "userPrompt", "message", "text", "input",
		"transformedPrompt",
	}
	var parts []string
	for _, key := range keys {
		if text, ok := value[key].(string); ok {
			parts = append(parts, text)
		}
	}
	if len(parts) == 0 {
		// Some hosts nest the user message one level under event/input.
		for _, nestedKey := range []string{"event", "payload", "request"} {
			nested, ok := value[nestedKey].(map[string]any)
			if ok {
				for _, key := range keys {
					if text, ok := nested[key].(string); ok {
						parts = append(parts, text)
					}
				}
			}
		}
	}
	return strings.Join(parts, "\n")
}

func looksCorrective(prompt string) bool {
	normalized := strings.ToLower(strings.TrimSpace(prompt))
	if normalized == "" {
		return false
	}
	// Require both a correction/failure signal and a reference to an earlier
	// agent outcome. This intentionally favors false negatives over injecting
	// failure-review instructions into ordinary prompts.
	correctionSignals := []string{
		"failed", "failure", "wrong", "incorrect", "mistake", "missed",
		"ignored", "didn't", "did not", "not working", "broke", "regression",
		"失败", "错误", "错了", "没做到", "没有做到", "没能", "遗漏", "忽略",
		"又出现", "返工", "不对", "有问题",
	}
	priorSignals := []string{
		"you ", "your ", "agent", "previous", "earlier", "again", "still",
		"last time", "before", "already", "prior",
		"你", "之前", "上次", "刚才", "再次", "又", "仍然", "已经", "原来",
	}
	return containsAny(normalized, correctionSignals) && containsAny(normalized, priorSignals)
}

func containsAny(value string, values []string) bool {
	for _, candidate := range values {
		if strings.Contains(value, candidate) {
			return true
		}
	}
	return false
}

func supportedHarness(value string) bool {
	switch strings.Map(func(r rune) rune {
		if unicode.IsSpace(r) {
			return -1
		}
		return unicode.ToLower(r)
	}, value) {
	case "codex", "claude-code", "copilot-cli", "copilot-vscode", "cursor", "generic":
		return true
	default:
		return false
	}
}
