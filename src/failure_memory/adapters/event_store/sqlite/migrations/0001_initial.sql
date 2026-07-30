CREATE TABLE IF NOT EXISTS schema_migration (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE capture_attempt (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    summary TEXT NOT NULL,
    classification TEXT NOT NULL CHECK (classification IN (
        'requirement_update', 'requirement_clarification', 'preference_update',
        'real_failure', 'mixed', 'uncertain'
    )),
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject', 'defer')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    reason_codes_json TEXT NOT NULL,
    expectation_source TEXT NOT NULL,
    expectation_established_at TEXT,
    observed_outcome_at TEXT NOT NULL,
    failure_portion_summary TEXT,
    policy_version TEXT NOT NULL
) STRICT;

CREATE TABLE incident (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    capture_attempt_id TEXT NOT NULL UNIQUE
        REFERENCES capture_attempt(id),
    outcome_summary TEXT NOT NULL,
    expected_invariant TEXT NOT NULL,
    controllable_cause TEXT NOT NULL,
    material_impact TEXT NOT NULL,
    recurrence_risk TEXT NOT NULL
) STRICT;

CREATE TABLE lesson (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL
) STRICT;

CREATE TABLE lesson_version (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    lesson_id TEXT NOT NULL REFERENCES lesson(id),
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN (
        'proposed', 'verified', 'deprecated', 'superseded'
    )),
    signature TEXT NOT NULL,
    title TEXT NOT NULL,
    rule TEXT NOT NULL,
    prevention_action TEXT NOT NULL,
    verification_action TEXT NOT NULL,
    applicability TEXT NOT NULL,
    counterexamples TEXT NOT NULL,
    UNIQUE (lesson_id, version_number),
    UNIQUE (id, lesson_id)
) STRICT;

CREATE INDEX lesson_version_signature_idx ON lesson_version(signature);

CREATE TABLE lesson_head (
    lesson_id TEXT PRIMARY KEY REFERENCES lesson(id),
    lesson_version_id TEXT NOT NULL UNIQUE,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (lesson_version_id, lesson_id)
        REFERENCES lesson_version(id, lesson_id)
) STRICT;

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
        'same_cause_same_invariant', 'novel'
    )),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    UNIQUE (incident_id, lesson_id),
    FOREIGN KEY (lesson_version_id, lesson_id)
        REFERENCES lesson_version(id, lesson_id)
) STRICT;

CREATE TABLE adapter_profile (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    state TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL
) STRICT;

CREATE TABLE adapter_health_event (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    state TEXT NOT NULL,
    detail_json TEXT NOT NULL
) STRICT;

CREATE TRIGGER capture_attempt_no_update
BEFORE UPDATE ON capture_attempt
BEGIN SELECT RAISE(ABORT, 'append-only table: capture_attempt'); END;
CREATE TRIGGER capture_attempt_no_delete
BEFORE DELETE ON capture_attempt
BEGIN SELECT RAISE(ABORT, 'append-only table: capture_attempt'); END;

CREATE TRIGGER incident_no_update
BEFORE UPDATE ON incident
BEGIN SELECT RAISE(ABORT, 'append-only table: incident'); END;
CREATE TRIGGER incident_no_delete
BEFORE DELETE ON incident
BEGIN SELECT RAISE(ABORT, 'append-only table: incident'); END;

CREATE TRIGGER lesson_no_update
BEFORE UPDATE ON lesson
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson'); END;
CREATE TRIGGER lesson_no_delete
BEFORE DELETE ON lesson
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson'); END;

CREATE TRIGGER lesson_version_no_update
BEFORE UPDATE ON lesson_version
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_version'); END;
CREATE TRIGGER lesson_version_no_delete
BEFORE DELETE ON lesson_version
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_version'); END;

CREATE TRIGGER relation_no_update
BEFORE UPDATE ON incident_lesson_relation
BEGIN SELECT RAISE(ABORT, 'append-only table: incident_lesson_relation'); END;
CREATE TRIGGER relation_no_delete
BEFORE DELETE ON incident_lesson_relation
BEGIN SELECT RAISE(ABORT, 'append-only table: incident_lesson_relation'); END;
