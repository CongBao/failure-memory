package redact

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestTextTruncationPreservesUTF8(t *testing.T) {
	value := strings.Repeat("甲", 2000)
	result := Text(value)
	if len(result) > 4000 {
		t.Fatalf("redacted text has %d bytes, want at most 4000", len(result))
	}
	if !utf8.ValidString(result) {
		t.Fatal("redacted text is not valid UTF-8")
	}
}
