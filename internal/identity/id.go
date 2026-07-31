package identity

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"strings"
	"time"
)

func New(prefix string) string {
	var random [10]byte
	if _, err := rand.Read(random[:]); err != nil {
		panic(fmt.Sprintf("cryptographic random source unavailable: %v", err))
	}
	clean := strings.Trim(strings.ToLower(prefix), "_- ")
	return fmt.Sprintf("%s_%x%s", clean, time.Now().UTC().UnixMilli(), hex.EncodeToString(random[:]))
}
