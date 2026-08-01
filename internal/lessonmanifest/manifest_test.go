package lessonmanifest

import (
	"testing"

	"github.com/CongBao/failure-memory/internal/model"
)

func TestDigestIsOrderIndependentAndFieldBoundarySafe(t *testing.T) {
	first := model.LessonDocument{LessonVersionID: "lesson-b", Title: "a\x00b"}
	second := model.LessonDocument{LessonVersionID: "lesson-a", Title: "stable"}
	if Digest([]model.LessonDocument{first, second}) != Digest([]model.LessonDocument{second, first}) {
		t.Fatal("manifest digest depends on query order")
	}
	withoutNull := first
	withoutNull.Title = "ab"
	if Digest([]model.LessonDocument{first}) == Digest([]model.LessonDocument{withoutNull}) {
		t.Fatal("manifest digest discarded a field byte")
	}
}
