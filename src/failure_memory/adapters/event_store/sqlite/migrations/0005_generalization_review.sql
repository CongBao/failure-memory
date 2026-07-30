DROP TRIGGER relation_no_update;
DROP TRIGGER relation_no_delete;

ALTER TABLE incident_lesson_relation RENAME TO incident_lesson_relation_v1;

CREATE TABLE incident_lesson_relation (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES incident(id),
    lesson_id TEXT NOT NULL REFERENCES lesson(id),
    lesson_version_id TEXT NOT NULL,
    relation_type TEXT NOT NULL CHECK (relation_type IN (
        'same_cause_same_invariant', 'reviewed_reuse',
        'reviewed_generalization', 'novel'
    )),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    UNIQUE (incident_id, lesson_id),
    FOREIGN KEY (lesson_version_id, lesson_id)
        REFERENCES lesson_version(id, lesson_id)
) STRICT;

INSERT INTO incident_lesson_relation
SELECT * FROM incident_lesson_relation_v1;

DROP TABLE incident_lesson_relation_v1;

CREATE TRIGGER relation_no_update
BEFORE UPDATE ON incident_lesson_relation
BEGIN SELECT RAISE(ABORT, 'append-only table: incident_lesson_relation'); END;
CREATE TRIGGER relation_no_delete
BEFORE DELETE ON incident_lesson_relation
BEGIN SELECT RAISE(ABORT, 'append-only table: incident_lesson_relation'); END;

CREATE TABLE lesson_signature_alias (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    signature TEXT NOT NULL UNIQUE,
    lesson_id TEXT NOT NULL REFERENCES lesson(id),
    source_lesson_version_id TEXT NOT NULL,
    FOREIGN KEY (source_lesson_version_id, lesson_id)
        REFERENCES lesson_version(id, lesson_id)
) STRICT;

INSERT INTO lesson_signature_alias(
    id, schema_version, created_at, source_harness, workspace_fingerprint,
    session_fingerprint, provenance, redaction_state, signature, lesson_id,
    source_lesson_version_id
)
SELECT
    'lsa_' || version.id, 1, version.created_at, version.source_harness,
    version.workspace_fingerprint, version.session_fingerprint, version.provenance,
    version.redaction_state, version.signature, version.lesson_id, version.id
FROM lesson_head AS head
JOIN lesson_version AS version ON version.id = head.lesson_version_id;

CREATE TABLE failure_generalization_review (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    capture_attempt_id TEXT NOT NULL REFERENCES capture_attempt(id),
    proposed_signature TEXT NOT NULL,
    recommendation TEXT NOT NULL CHECK (recommendation IN (
        'reuse_exact', 'review_related', 'create_distinct'
    )),
    retrieval_profile TEXT NOT NULL,
    candidate_lesson_version_ids_json TEXT NOT NULL,
    automatic_merge INTEGER NOT NULL CHECK (automatic_merge = 0)
) STRICT;

CREATE INDEX failure_generalization_review_capture_idx
ON failure_generalization_review(capture_attempt_id);

CREATE TABLE failure_generalization_decision_event (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    review_id TEXT NOT NULL UNIQUE REFERENCES failure_generalization_review(id),
    disposition TEXT NOT NULL CHECK (disposition IN (
        'reuse_existing', 'generalize_existing', 'create_distinct'
    )),
    rationale_code TEXT NOT NULL,
    target_lesson_version_id TEXT REFERENCES lesson_version(id),
    resulting_lesson_version_id TEXT NOT NULL REFERENCES lesson_version(id)
) STRICT;

CREATE TRIGGER lesson_signature_alias_no_update
BEFORE UPDATE ON lesson_signature_alias
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_signature_alias'); END;
CREATE TRIGGER lesson_signature_alias_no_delete
BEFORE DELETE ON lesson_signature_alias
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_signature_alias'); END;

CREATE TRIGGER failure_generalization_review_no_update
BEFORE UPDATE ON failure_generalization_review
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_generalization_review'); END;
CREATE TRIGGER failure_generalization_review_no_delete
BEFORE DELETE ON failure_generalization_review
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_generalization_review'); END;

CREATE TRIGGER failure_generalization_decision_no_update
BEFORE UPDATE ON failure_generalization_decision_event
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_generalization_decision_event'); END;
CREATE TRIGGER failure_generalization_decision_no_delete
BEFORE DELETE ON failure_generalization_decision_event
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_generalization_decision_event'); END;
