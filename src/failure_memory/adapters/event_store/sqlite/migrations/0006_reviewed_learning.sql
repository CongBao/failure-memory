CREATE TABLE lesson_generalization_proposal_review (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    source_harness TEXT NOT NULL,
    workspace_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT,
    provenance TEXT NOT NULL,
    redaction_state TEXT NOT NULL,
    proposal_id TEXT NOT NULL REFERENCES lesson_generalization_proposal(id),
    prior_review_id TEXT UNIQUE REFERENCES lesson_generalization_proposal_review(id),
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject', 'defer')),
    rationale_code TEXT NOT NULL,
    generalized_expected_invariant TEXT,
    generalized_controllable_cause TEXT,
    resulting_lesson_version_id TEXT REFERENCES lesson_version(id),
    CHECK (
        (
            decision = 'accept'
            AND (
                (
                    generalized_expected_invariant IS NULL
                    AND generalized_controllable_cause IS NULL
                    AND resulting_lesson_version_id IS NULL
                )
                OR (
                    generalized_expected_invariant IS NOT NULL
                    AND generalized_controllable_cause IS NOT NULL
                    AND resulting_lesson_version_id IS NOT NULL
                )
            )
        )
        OR (
            decision IN ('reject', 'defer')
            AND generalized_expected_invariant IS NULL
            AND generalized_controllable_cause IS NULL
            AND resulting_lesson_version_id IS NULL
        )
    )
) STRICT;

CREATE UNIQUE INDEX proposal_review_initial_idx
ON lesson_generalization_proposal_review(proposal_id)
WHERE prior_review_id IS NULL;

CREATE UNIQUE INDEX proposal_review_terminal_idx
ON lesson_generalization_proposal_review(proposal_id)
WHERE decision IN ('accept', 'reject');

CREATE INDEX proposal_review_proposal_idx
ON lesson_generalization_proposal_review(proposal_id, created_at, id);

CREATE TABLE lesson_generalization_source (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    review_id TEXT NOT NULL REFERENCES lesson_generalization_proposal_review(id),
    lesson_version_id TEXT NOT NULL REFERENCES lesson_version(id),
    relation TEXT NOT NULL CHECK (relation IN ('supporting', 'counterexample')),
    UNIQUE (review_id, lesson_version_id, relation)
) STRICT;

CREATE INDEX lesson_generalization_source_version_idx
ON lesson_generalization_source(lesson_version_id, review_id);

CREATE TABLE learning_evaluation_run (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state = 'shadow'),
    corpus_name TEXT NOT NULL,
    corpus_version TEXT NOT NULL,
    corpus_fingerprint TEXT NOT NULL,
    baseline_policy TEXT NOT NULL,
    candidate_policy TEXT NOT NULL,
    case_count INTEGER NOT NULL CHECK (case_count >= 0),
    negative_case_count INTEGER NOT NULL CHECK (negative_case_count >= 0),
    metrics_json TEXT NOT NULL,
    thresholds_json TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    production_activated INTEGER NOT NULL CHECK (production_activated = 0)
) STRICT;

ALTER TABLE recall_candidate
ADD COLUMN cluster_review_id TEXT REFERENCES lesson_generalization_proposal_review(id);

ALTER TABLE recall_candidate
ADD COLUMN cluster_key TEXT;

ALTER TABLE recall_candidate
ADD COLUMN cluster_supporting_lesson_version_ids_json TEXT;

CREATE TRIGGER proposal_review_no_update
BEFORE UPDATE ON lesson_generalization_proposal_review
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_generalization_proposal_review'); END;
CREATE TRIGGER proposal_review_no_delete
BEFORE DELETE ON lesson_generalization_proposal_review
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_generalization_proposal_review'); END;

CREATE TRIGGER generalization_source_no_update
BEFORE UPDATE ON lesson_generalization_source
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_generalization_source'); END;
CREATE TRIGGER generalization_source_no_delete
BEFORE DELETE ON lesson_generalization_source
BEGIN SELECT RAISE(ABORT, 'append-only table: lesson_generalization_source'); END;

CREATE TRIGGER learning_evaluation_run_no_update
BEFORE UPDATE ON learning_evaluation_run
BEGIN SELECT RAISE(ABORT, 'append-only table: learning_evaluation_run'); END;
CREATE TRIGGER learning_evaluation_run_no_delete
BEFORE DELETE ON learning_evaluation_run
BEGIN SELECT RAISE(ABORT, 'append-only table: learning_evaluation_run'); END;
