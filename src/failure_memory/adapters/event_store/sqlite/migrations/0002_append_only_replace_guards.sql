CREATE TRIGGER capture_attempt_no_replace
BEFORE INSERT ON capture_attempt
WHEN EXISTS (
    SELECT 1 FROM capture_attempt WHERE id = NEW.id
)
BEGIN SELECT RAISE(ABORT, 'append-only table: capture_attempt'); END;

CREATE TRIGGER incident_no_replace
BEFORE INSERT ON incident
WHEN EXISTS (
    SELECT 1
    FROM incident
    WHERE id = NEW.id OR capture_attempt_id = NEW.capture_attempt_id
)
BEGIN SELECT RAISE(ABORT, 'append-only table: incident'); END;

CREATE TRIGGER lesson_no_replace
BEFORE INSERT ON lesson
WHEN EXISTS (
    SELECT 1 FROM lesson WHERE id = NEW.id
)
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson'); END;

CREATE TRIGGER lesson_version_no_replace
BEFORE INSERT ON lesson_version
WHEN EXISTS (
    SELECT 1
    FROM lesson_version
    WHERE id = NEW.id
       OR (lesson_id = NEW.lesson_id AND version_number = NEW.version_number)
)
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_version'); END;

CREATE TRIGGER relation_no_replace
BEFORE INSERT ON incident_lesson_relation
WHEN EXISTS (
    SELECT 1
    FROM incident_lesson_relation
    WHERE id = NEW.id
       OR (incident_id = NEW.incident_id AND lesson_id = NEW.lesson_id)
)
BEGIN SELECT RAISE(ABORT, 'append-only table: incident_lesson_relation'); END;
