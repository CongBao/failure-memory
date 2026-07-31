INSERT INTO store_schema_capability(
    migration_version, schema_kind, minimum_reader_migration,
    minimum_writer_migration, description
) VALUES (
    8, 'additive', 7, 8,
    'Adds compact expectation evidence and append-only fast-recording operation telemetry.'
);

ALTER TABLE capture_attempt ADD COLUMN expectation_preexisted INTEGER
    CHECK (expectation_preexisted IS NULL OR expectation_preexisted IN (0, 1));
ALTER TABLE capture_attempt ADD COLUMN expectation_evidence TEXT;

CREATE TABLE failure_recording_operation (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    transport TEXT NOT NULL CHECK (transport IN ('mcp', 'cli')),
    workflow_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('recorded', 'not_failure', 'deferred')),
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject', 'defer')),
    deduplication_status TEXT NOT NULL CHECK (deduplication_status IN (
        'not_run', 'exact_reuse', 'distinct', 'related_pending_generalization'
    )),
    semantic_status TEXT NOT NULL,
    total_latency_ms INTEGER NOT NULL CHECK (total_latency_ms >= 0),
    qualification_latency_ms INTEGER NOT NULL CHECK (qualification_latency_ms >= 0),
    causal_latency_ms INTEGER NOT NULL CHECK (causal_latency_ms >= 0),
    deduplication_latency_ms INTEGER NOT NULL CHECK (deduplication_latency_ms >= 0),
    persistence_latency_ms INTEGER NOT NULL CHECK (persistence_latency_ms >= 0),
    capture_attempt_id TEXT NOT NULL REFERENCES capture_attempt(id),
    incident_id TEXT REFERENCES incident(id),
    lesson_version_id TEXT REFERENCES lesson_version(id),
    error_code TEXT,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0)
) STRICT;

CREATE INDEX failure_recording_operation_created_idx
ON failure_recording_operation(created_at);

CREATE INDEX failure_recording_operation_harness_idx
ON failure_recording_operation(source_harness, created_at);

CREATE TRIGGER failure_recording_operation_no_update
BEFORE UPDATE ON failure_recording_operation
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_recording_operation'); END;

CREATE TRIGGER failure_recording_operation_no_delete
BEFORE DELETE ON failure_recording_operation
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_recording_operation'); END;
