CREATE TABLE retrieval_profile_snapshot (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    backend TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    embedding_provider TEXT,
    embedding_model TEXT,
    embedding_revision TEXT,
    embedding_dimensions INTEGER,
    UNIQUE (config_fingerprint)
) STRICT;

CREATE TABLE recall_attempt (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    requested_mode TEXT NOT NULL CHECK (requested_mode IN (
        'auto', 'exact', 'lexical', 'semantic', 'hybrid'
    )),
    executed_mode TEXT NOT NULL CHECK (executed_mode IN (
        'auto', 'exact', 'lexical', 'semantic', 'hybrid'
    )),
    status TEXT NOT NULL CHECK (status IN (
        'ok', 'no_match', 'degraded', 'setup_required', 'insufficient_evidence'
    )),
    retrieval_profile_id TEXT NOT NULL REFERENCES retrieval_profile_snapshot(id),
    query_fingerprint TEXT NOT NULL,
    query_fields_json TEXT NOT NULL,
    top_k INTEGER NOT NULL CHECK (top_k BETWEEN 1 AND 5),
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0)
) STRICT;

CREATE INDEX recall_attempt_workspace_created_idx
ON recall_attempt(workspace_fingerprint, created_at);

CREATE TABLE recall_candidate (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    recall_attempt_id TEXT NOT NULL REFERENCES recall_attempt(id),
    lesson_version_id TEXT NOT NULL REFERENCES lesson_version(id),
    candidate_rank INTEGER NOT NULL CHECK (candidate_rank > 0),
    channels_json TEXT NOT NULL,
    exact_match INTEGER NOT NULL CHECK (exact_match IN (0, 1)),
    lexical_rank INTEGER CHECK (lexical_rank IS NULL OR lexical_rank > 0),
    semantic_rank INTEGER CHECK (semantic_rank IS NULL OR semantic_rank > 0),
    vector_distance REAL,
    fused_score REAL NOT NULL,
    eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
    eligibility_reason TEXT NOT NULL,
    UNIQUE (recall_attempt_id, lesson_version_id)
) STRICT;

CREATE TABLE recall_selection (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    recall_attempt_id TEXT NOT NULL REFERENCES recall_attempt(id),
    lesson_version_id TEXT NOT NULL REFERENCES lesson_version(id),
    selection_rank INTEGER NOT NULL CHECK (selection_rank > 0),
    selection_reason TEXT NOT NULL,
    UNIQUE (recall_attempt_id, lesson_version_id)
) STRICT;

CREATE TABLE recall_outcome_event (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    recall_attempt_id TEXT NOT NULL REFERENCES recall_attempt(id),
    lesson_version_id TEXT REFERENCES lesson_version(id),
    outcome TEXT NOT NULL CHECK (outcome IN (
        'useful', 'not_useful', 'false_positive', 'prevented_recurrence',
        'contradicted_current_task', 'stale', 'ignored', 'unknown'
    )),
    detail_code TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
) STRICT;

CREATE INDEX recall_outcome_attempt_idx ON recall_outcome_event(recall_attempt_id);
CREATE INDEX recall_outcome_lesson_idx ON recall_outcome_event(lesson_version_id);

CREATE TRIGGER retrieval_profile_snapshot_no_update
BEFORE UPDATE ON retrieval_profile_snapshot
BEGIN SELECT RAISE(ABORT, 'append-only table: retrieval_profile_snapshot'); END;
CREATE TRIGGER retrieval_profile_snapshot_no_delete
BEFORE DELETE ON retrieval_profile_snapshot
BEGIN SELECT RAISE(ABORT, 'append-only table: retrieval_profile_snapshot'); END;

CREATE TRIGGER recall_attempt_no_update
BEFORE UPDATE ON recall_attempt
BEGIN SELECT RAISE(ABORT, 'append-only table: recall_attempt'); END;
CREATE TRIGGER recall_attempt_no_delete
BEFORE DELETE ON recall_attempt
BEGIN SELECT RAISE(ABORT, 'append-only table: recall_attempt'); END;

CREATE TRIGGER recall_candidate_no_update
BEFORE UPDATE ON recall_candidate
BEGIN SELECT RAISE(ABORT, 'append-only table: recall_candidate'); END;
CREATE TRIGGER recall_candidate_no_delete
BEFORE DELETE ON recall_candidate
BEGIN SELECT RAISE(ABORT, 'append-only table: recall_candidate'); END;

CREATE TRIGGER recall_selection_no_update
BEFORE UPDATE ON recall_selection
BEGIN SELECT RAISE(ABORT, 'append-only table: recall_selection'); END;
CREATE TRIGGER recall_selection_no_delete
BEFORE DELETE ON recall_selection
BEGIN SELECT RAISE(ABORT, 'append-only table: recall_selection'); END;

CREATE TRIGGER recall_outcome_event_no_update
BEFORE UPDATE ON recall_outcome_event
BEGIN SELECT RAISE(ABORT, 'append-only table: recall_outcome_event'); END;
CREATE TRIGGER recall_outcome_event_no_delete
BEFORE DELETE ON recall_outcome_event
BEGIN SELECT RAISE(ABORT, 'append-only table: recall_outcome_event'); END;
