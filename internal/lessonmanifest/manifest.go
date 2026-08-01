// Package lessonmanifest computes a deterministic digest for the authoritative
// lesson projection and its derived retrieval copy.
package lessonmanifest

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"sort"

	"github.com/CongBao/failure-memory/internal/model"
)

func Digest(lessons []model.LessonDocument) string {
	ordered := append([]model.LessonDocument(nil), lessons...)
	sort.Slice(ordered, func(left, right int) bool {
		return ordered[left].LessonVersionID < ordered[right].LessonVersionID
	})
	hash := sha256.New()
	var size [8]byte
	for _, lesson := range ordered {
		for _, value := range []string{
			lesson.LessonID,
			lesson.LessonVersionID,
			lesson.Signature,
			lesson.Title,
			lesson.Rule,
			lesson.Prevention,
			lesson.Verification,
			lesson.Applicability,
			lesson.Counterexamples,
			lesson.CauseLayer,
			lesson.FailureMode,
			lesson.Component,
			lesson.Document,
		} {
			binary.BigEndian.PutUint64(size[:], uint64(len(value)))
			_, _ = hash.Write(size[:])
			_, _ = hash.Write([]byte(value))
		}
	}
	return hex.EncodeToString(hash.Sum(nil))
}
