CREATE TABLE store_schema_capability (
    migration_version INTEGER PRIMARY KEY,
    schema_kind TEXT NOT NULL CHECK (schema_kind = 'additive'),
    minimum_reader_migration INTEGER NOT NULL CHECK (minimum_reader_migration > 0),
    minimum_writer_migration INTEGER NOT NULL CHECK (minimum_writer_migration > 0),
    description TEXT NOT NULL
) STRICT;

INSERT INTO store_schema_capability(
    migration_version, schema_kind, minimum_reader_migration,
    minimum_writer_migration, description
) VALUES (
    7, 'additive', 7, 7,
    'Adds optional causal assessments, repair recommendations, and repair outcome events.'
);

CREATE TABLE failure_causal_assessment (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    capture_attempt_id TEXT NOT NULL REFERENCES capture_attempt(id),
    state TEXT NOT NULL CHECK (state IN ('supported', 'partial', 'unknown')),
    unknown_reason TEXT,
    policy_version TEXT NOT NULL,
    CHECK (
        (state = 'unknown' AND unknown_reason IS NOT NULL)
        OR (state != 'unknown' AND unknown_reason IS NULL)
    )
) STRICT;

CREATE INDEX failure_causal_assessment_capture_idx
ON failure_causal_assessment(capture_attempt_id, created_at);

CREATE TABLE failure_causal_factor (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    assessment_id TEXT NOT NULL REFERENCES failure_causal_assessment(id),
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 4),
    role TEXT NOT NULL CHECK (role IN ('primary', 'contributing')),
    layer TEXT NOT NULL CHECK (layer IN (
        'skill_instruction', 'agent_instruction', 'project_instruction',
        'system_instruction', 'hook_policy', 'plugin_manifest', 'tool_contract',
        'application_logic', 'adapter_runtime', 'schema_migration',
        'test_evaluation_gap', 'harness_limitation', 'model_behavior',
        'external_dependency', 'unknown'
    )),
    failure_mode TEXT NOT NULL CHECK (failure_mode IN (
        'missing', 'ambiguous', 'conflicting', 'not_loaded', 'not_triggered',
        'ignored', 'incorrectly_implemented', 'insufficient_validation',
        'uninspectable', 'unknown'
    )),
    component_reference TEXT NOT NULL,
    evidence_summary TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low', 'unknown')),
    UNIQUE (assessment_id, ordinal)
) STRICT;

CREATE UNIQUE INDEX failure_causal_factor_primary_idx
ON failure_causal_factor(assessment_id)
WHERE role = 'primary';

CREATE TABLE failure_repair_recommendation (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    assessment_id TEXT NOT NULL REFERENCES failure_causal_assessment(id),
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 3),
    target_layer TEXT NOT NULL CHECK (target_layer IN (
        'skill_instruction', 'agent_instruction', 'project_instruction',
        'system_instruction', 'hook_policy', 'plugin_manifest', 'tool_contract',
        'application_logic', 'adapter_runtime', 'schema_migration',
        'test_evaluation_gap', 'harness_limitation', 'model_behavior',
        'external_dependency', 'unknown'
    )),
    target_reference TEXT NOT NULL,
    recommended_change TEXT NOT NULL,
    verification_action TEXT NOT NULL,
    rationale TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low', 'unknown')),
    UNIQUE (assessment_id, ordinal)
) STRICT;

CREATE TABLE failure_causal_review_relation (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    assessment_id TEXT NOT NULL REFERENCES failure_causal_assessment(id),
    review_id TEXT NOT NULL UNIQUE REFERENCES failure_generalization_review(id),
    UNIQUE (assessment_id, review_id)
) STRICT;

CREATE TABLE failure_causal_incident_relation (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    assessment_id TEXT NOT NULL UNIQUE REFERENCES failure_causal_assessment(id),
    incident_id TEXT NOT NULL UNIQUE REFERENCES incident(id)
) STRICT;

CREATE TABLE failure_repair_outcome_event (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    recommendation_id TEXT NOT NULL REFERENCES failure_repair_recommendation(id),
    outcome TEXT NOT NULL CHECK (outcome IN (
        'applied', 'rejected', 'partially_applied', 'verified_effective',
        'verified_ineffective', 'recurrence_observed', 'superseded'
    )),
    detail_code TEXT NOT NULL,
    evidence_summary TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low', 'unknown'))
) STRICT;

CREATE INDEX failure_repair_outcome_recommendation_idx
ON failure_repair_outcome_event(recommendation_id, created_at);

CREATE TRIGGER store_schema_capability_no_update
BEFORE UPDATE ON store_schema_capability
BEGIN SELECT RAISE(ABORT, 'append-only table: store_schema_capability'); END;
CREATE TRIGGER store_schema_capability_no_delete
BEFORE DELETE ON store_schema_capability
BEGIN SELECT RAISE(ABORT, 'append-only table: store_schema_capability'); END;

CREATE TRIGGER failure_causal_assessment_no_update
BEFORE UPDATE ON failure_causal_assessment
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_causal_assessment'); END;
CREATE TRIGGER failure_causal_assessment_no_delete
BEFORE DELETE ON failure_causal_assessment
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_causal_assessment'); END;

CREATE TRIGGER failure_causal_factor_no_update
BEFORE UPDATE ON failure_causal_factor
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_causal_factor'); END;
CREATE TRIGGER failure_causal_factor_no_delete
BEFORE DELETE ON failure_causal_factor
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_causal_factor'); END;

CREATE TRIGGER failure_repair_recommendation_no_update
BEFORE UPDATE ON failure_repair_recommendation
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_repair_recommendation'); END;
CREATE TRIGGER failure_repair_recommendation_no_delete
BEFORE DELETE ON failure_repair_recommendation
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_repair_recommendation'); END;

CREATE TRIGGER failure_causal_review_relation_no_update
BEFORE UPDATE ON failure_causal_review_relation
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_causal_review_relation'); END;
CREATE TRIGGER failure_causal_review_relation_no_delete
BEFORE DELETE ON failure_causal_review_relation
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_causal_review_relation'); END;

CREATE TRIGGER failure_causal_incident_relation_no_update
BEFORE UPDATE ON failure_causal_incident_relation
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_causal_incident_relation'); END;
CREATE TRIGGER failure_causal_incident_relation_no_delete
BEFORE DELETE ON failure_causal_incident_relation
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_causal_incident_relation'); END;

CREATE TRIGGER failure_repair_outcome_event_no_update
BEFORE UPDATE ON failure_repair_outcome_event
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_repair_outcome_event'); END;
CREATE TRIGGER failure_repair_outcome_event_no_delete
BEFORE DELETE ON failure_repair_outcome_event
BEGIN SELECT RAISE(ABORT, 'append-only table: failure_repair_outcome_event'); END;
