CREATE TABLE source_store_import (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_store_id TEXT NOT NULL,
    source_content_fingerprint TEXT NOT NULL,
    source_schema_version INTEGER NOT NULL CHECK (source_schema_version > 0),
    source_fingerprint_domain TEXT NOT NULL,
    imported_counts_json TEXT NOT NULL,
    skipped_counts_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state = 'completed'),
    UNIQUE (source_store_id, source_content_fingerprint)
) STRICT;

CREATE TABLE recall_miss_event (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    recall_attempt_id TEXT NOT NULL REFERENCES recall_attempt(id),
    relevant_lesson_version_id TEXT NOT NULL REFERENCES lesson_version(id),
    detail_code TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
) STRICT;

CREATE INDEX recall_miss_attempt_idx ON recall_miss_event(recall_attempt_id);
CREATE INDEX recall_miss_lesson_idx ON recall_miss_event(relevant_lesson_version_id);

CREATE TABLE lesson_lifecycle_event (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    lesson_id TEXT NOT NULL REFERENCES lesson(id),
    prior_version_id TEXT NOT NULL REFERENCES lesson_version(id),
    new_version_id TEXT NOT NULL UNIQUE REFERENCES lesson_version(id),
    from_state TEXT NOT NULL CHECK (from_state IN (
        'proposed', 'verified', 'deprecated', 'superseded'
    )),
    to_state TEXT NOT NULL CHECK (to_state IN (
        'verified', 'deprecated', 'superseded'
    )),
    rationale_code TEXT NOT NULL
) STRICT;

CREATE TABLE ranking_experiment (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state = 'shadow'),
    baseline_policy TEXT NOT NULL,
    candidate_policy TEXT NOT NULL,
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    labeled_selection_count INTEGER NOT NULL CHECK (labeled_selection_count >= 0),
    changed_top_rank_count INTEGER NOT NULL CHECK (changed_top_rank_count >= 0),
    metrics_json TEXT NOT NULL
) STRICT;

CREATE TABLE lesson_cluster_run (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state = 'proposed'),
    retrieval_profile_id TEXT NOT NULL REFERENCES retrieval_profile_snapshot(id),
    distance_threshold REAL NOT NULL CHECK (
        distance_threshold >= 0 AND distance_threshold <= 2
    ),
    lesson_count INTEGER NOT NULL CHECK (lesson_count >= 0),
    cluster_count INTEGER NOT NULL CHECK (cluster_count >= 0)
) STRICT;

CREATE TABLE lesson_cluster_member (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    cluster_run_id TEXT NOT NULL REFERENCES lesson_cluster_run(id),
    cluster_key TEXT NOT NULL,
    lesson_version_id TEXT NOT NULL REFERENCES lesson_version(id),
    UNIQUE (cluster_run_id, cluster_key, lesson_version_id)
) STRICT;

CREATE TABLE lesson_generalization_proposal (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    cluster_run_id TEXT NOT NULL REFERENCES lesson_cluster_run(id),
    cluster_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'proposed'),
    supporting_lesson_version_ids_json TEXT NOT NULL,
    counterexample_lesson_version_ids_json TEXT NOT NULL,
    UNIQUE (cluster_run_id, cluster_key)
) STRICT;

CREATE TRIGGER source_store_import_no_update
BEFORE UPDATE ON source_store_import
BEGIN SELECT RAISE(ABORT, 'append-only table: source_store_import'); END;
CREATE TRIGGER source_store_import_no_delete
BEFORE DELETE ON source_store_import
BEGIN SELECT RAISE(ABORT, 'append-only table: source_store_import'); END;

CREATE TRIGGER recall_miss_event_no_update
BEFORE UPDATE ON recall_miss_event
BEGIN SELECT RAISE(ABORT, 'append-only table: recall_miss_event'); END;
CREATE TRIGGER recall_miss_event_no_delete
BEFORE DELETE ON recall_miss_event
BEGIN SELECT RAISE(ABORT, 'append-only table: recall_miss_event'); END;

CREATE TRIGGER lesson_lifecycle_event_no_update
BEFORE UPDATE ON lesson_lifecycle_event
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_lifecycle_event'); END;
CREATE TRIGGER lesson_lifecycle_event_no_delete
BEFORE DELETE ON lesson_lifecycle_event
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_lifecycle_event'); END;

CREATE TRIGGER ranking_experiment_no_update
BEFORE UPDATE ON ranking_experiment
BEGIN SELECT RAISE(ABORT, 'append-only table: ranking_experiment'); END;
CREATE TRIGGER ranking_experiment_no_delete
BEFORE DELETE ON ranking_experiment
BEGIN SELECT RAISE(ABORT, 'append-only table: ranking_experiment'); END;

CREATE TRIGGER lesson_cluster_run_no_update
BEFORE UPDATE ON lesson_cluster_run
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_cluster_run'); END;
CREATE TRIGGER lesson_cluster_run_no_delete
BEFORE DELETE ON lesson_cluster_run
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_cluster_run'); END;

CREATE TRIGGER lesson_cluster_member_no_update
BEFORE UPDATE ON lesson_cluster_member
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_cluster_member'); END;
CREATE TRIGGER lesson_cluster_member_no_delete
BEFORE DELETE ON lesson_cluster_member
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_cluster_member'); END;

CREATE TRIGGER lesson_generalization_proposal_no_update
BEFORE UPDATE ON lesson_generalization_proposal
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_generalization_proposal'); END;
CREATE TRIGGER lesson_generalization_proposal_no_delete
BEFORE DELETE ON lesson_generalization_proposal
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_generalization_proposal'); END;
