package model

import "time"

type Classification string

const (
	RequirementUpdate        Classification = "requirement_update"
	RequirementClarification Classification = "requirement_clarification"
	PreferenceUpdate         Classification = "preference_update"
	RealFailure              Classification = "real_failure"
	Mixed                    Classification = "mixed"
	Uncertain                Classification = "uncertain"
)

type Decision string

const (
	Accept Decision = "accept"
	Reject Decision = "reject"
	Defer  Decision = "defer"
)

type RememberStatus string

const (
	Recorded   RememberStatus = "recorded"
	NotFailure RememberStatus = "not_failure"
	Deferred   RememberStatus = "deferred"
)

type ExpectationEvidence struct {
	Invariant string `json:"invariant" jsonschema:"The invariant that existed before the outcome."`
	Source    string `json:"source" jsonschema:"Where the prior invariant was established."`
	Evidence  string `json:"evidence" jsonschema:"Compact evidence that the invariant predated the outcome."`
}

type ObservedEvidence struct {
	Outcome        string `json:"outcome" jsonschema:"The inspectable mismatch."`
	Impact         string `json:"impact" jsonschema:"Material impact of the mismatch."`
	RecurrenceRisk string `json:"recurrence_risk,omitempty" jsonschema:"Why the mismatch may recur."`
}

type CauseEvidence struct {
	Layer             string `json:"layer" jsonschema:"One of skill_instruction, agent_instruction, project_instruction, system_instruction, hook_policy, plugin_manifest, tool_contract, application_logic, adapter_runtime, schema_migration, test_evaluation_gap, harness_limitation, model_behavior, external_dependency, or unknown."`
	FailureMode       string `json:"failure_mode" jsonschema:"One of missing, ambiguous, conflicting, not_loaded, not_triggered, ignored, incorrectly_implemented, insufficient_validation, uninspectable, or unknown."`
	Component         string `json:"component" jsonschema:"Specific controllable component."`
	Evidence          string `json:"evidence" jsonschema:"Evidence supporting this cause rather than speculation."`
	RecommendedChange string `json:"recommended_change" jsonschema:"Where and how to fix the cause."`
	Verification      string `json:"verification" jsonschema:"How to verify the repair."`
	Confidence        string `json:"confidence,omitempty" jsonschema:"low, medium, high, or unknown."`
}

type LessonEvidence struct {
	Rule            string `json:"rule" jsonschema:"Durable rule learned from this failure."`
	Prevention      string `json:"prevention" jsonschema:"Concrete prevention action."`
	Verification    string `json:"verification" jsonschema:"Evidence that proves the prevention was applied."`
	Title           string `json:"title,omitempty" jsonschema:"Short lesson title."`
	Applicability   string `json:"applicability,omitempty" jsonschema:"Situations where the lesson applies."`
	Counterexamples string `json:"counterexamples,omitempty" jsonschema:"Situations where the lesson should not be applied."`
}

type RememberInput struct {
	Summary        string               `json:"summary" jsonschema:"Compact summary without raw prompts or secrets."`
	Classification Classification       `json:"classification" jsonschema:"requirement_update, requirement_clarification, preference_update, real_failure, mixed, or uncertain."`
	FailurePortion string               `json:"failure_portion,omitempty" jsonschema:"For mixed feedback, only the prior-invariant mismatch."`
	Expectation    *ExpectationEvidence `json:"expectation,omitempty"`
	Observed       *ObservedEvidence    `json:"observed,omitempty"`
	Cause          *CauseEvidence       `json:"cause,omitempty"`
	Lesson         *LessonEvidence      `json:"lesson,omitempty"`
	PriorRecallID  string               `json:"prior_recall_id,omitempty" jsonschema:"A prior recall attempt this failure demonstrates did not prevent recurrence."`
}

type RememberResult struct {
	OperationID        string         `json:"operation_id"`
	Status             RememberStatus `json:"status"`
	Decision           Decision       `json:"decision"`
	ReasonCodes        []string       `json:"reason_codes"`
	Deduplication      string         `json:"deduplication_status"`
	SemanticStatus     string         `json:"semantic_status"`
	TotalLatencyMS     int64          `json:"total_latency_ms"`
	CaptureEventID     string         `json:"capture_event_id"`
	IncidentID         string         `json:"incident_id,omitempty"`
	LessonID           string         `json:"lesson_id,omitempty"`
	LessonVersionID    string         `json:"lesson_version_id,omitempty"`
	RepairID           string         `json:"repair_recommendation_id,omitempty"`
	GeneralizationHint string         `json:"generalization_hint,omitempty"`
}

type RecallInput struct {
	Text              string `json:"text" jsonschema:"Compact task evidence, not a raw prompt."`
	ExpectedInvariant string `json:"expected_invariant,omitempty"`
	ControllableCause string `json:"controllable_cause,omitempty"`
	PreventionAction  string `json:"prevention_action,omitempty"`
	Component         string `json:"component,omitempty"`
	Mode              string `json:"mode,omitempty" jsonschema:"auto, exact, lexical, semantic, or hybrid."`
	TopK              int    `json:"top_k,omitempty" jsonschema:"Number of lessons to return, from 1 to 3."`
}

type Lesson struct {
	LessonID         string   `json:"lesson_id"`
	LessonVersionID  string   `json:"lesson_version_id"`
	Title            string   `json:"title"`
	Rule             string   `json:"rule"`
	Prevention       string   `json:"prevention"`
	Verification     string   `json:"verification"`
	Applicability    string   `json:"applicability,omitempty"`
	Counterexamples  string   `json:"counterexamples,omitempty"`
	CauseLayer       string   `json:"cause_layer"`
	FailureMode      string   `json:"failure_mode"`
	Component        string   `json:"component"`
	Score            float64  `json:"score"`
	RetrievalReasons []string `json:"retrieval_reasons"`
}

type RecallResult struct {
	AttemptID      string   `json:"attempt_id"`
	Mode           string   `json:"mode"`
	SemanticStatus string   `json:"semantic_status"`
	Lessons        []Lesson `json:"lessons"`
	TotalLatencyMS int64    `json:"total_latency_ms"`
	CandidateCount int      `json:"candidate_count"`
	StoreScope     string   `json:"store_scope"`
}

type RecallOutcomeInput struct {
	RecallAttemptID string  `json:"recall_attempt_id"`
	LessonVersionID string  `json:"lesson_version_id,omitempty"`
	Outcome         string  `json:"outcome" jsonschema:"useful, not_useful, false_positive, prevented_recurrence, contradicted_current_task, stale, ignored, or unknown."`
	DetailCode      string  `json:"detail_code,omitempty"`
	Confidence      float64 `json:"confidence,omitempty"`
}

type RepairOutcomeInput struct {
	RepairRecommendationID string `json:"repair_recommendation_id"`
	Outcome                string `json:"outcome" jsonschema:"applied, rejected, partially_applied, verified_effective, verified_ineffective, recurrence_observed, or superseded."`
	DetailCode             string `json:"detail_code,omitempty"`
	Evidence               string `json:"evidence,omitempty"`
	Confidence             string `json:"confidence,omitempty"`
}

type OutcomeResult struct {
	EventID string `json:"event_id"`
	Status  string `json:"status"`
}

type Cluster struct {
	Key              string   `json:"key"`
	LessonVersionIDs []string `json:"lesson_version_ids"`
}

type ClusterRunResult struct {
	RunID             string    `json:"run_id"`
	Profile           string    `json:"retrieval_profile"`
	Semantic          bool      `json:"semantic"`
	DistanceThreshold float64   `json:"distance_threshold"`
	LessonCount       int       `json:"lesson_count"`
	Clusters          []Cluster `json:"clusters"`
	ProposalCount     int       `json:"proposal_count"`
}

type GeneralizationReviewInput struct {
	RunID                    string   `json:"run_id"`
	ClusterKey               string   `json:"cluster_key"`
	Decision                 string   `json:"decision" jsonschema:"accept, reject, or defer."`
	RationaleCode            string   `json:"rationale_code"`
	SupportingLessonVersions []string `json:"supporting_lesson_version_ids"`
	CounterexampleVersions   []string `json:"counterexample_lesson_version_ids,omitempty"`
}

type LegacyCapture struct {
	CaptureID             string
	CreatedAt             string
	SourceHarness         string
	Summary               string
	Classification        string
	Decision              string
	ReasonCodesJSON       string
	ExpectationSource     string
	ExpectationEvidence   string
	FailurePortion        string
	IncidentID            string
	Outcome               string
	ExpectedInvariant     string
	ControllableCause     string
	MaterialImpact        string
	RecurrenceRisk        string
	LessonID              string
	LessonVersionID       string
	LessonSignature       string
	LessonTitle           string
	LessonRule            string
	LessonPrevention      string
	LessonVerification    string
	LessonApplicability   string
	LessonCounterexamples string
	LessonState           string
	CauseLayer            string
	FailureMode           string
	Component             string
	CauseEvidence         string
	CauseConfidence       string
	RepairID              string
	RepairTargetLayer     string
	RepairTarget          string
	RecommendedChange     string
	RepairVerification    string
	RepairRationale       string
	RepairConfidence      string
}

type LegacyImport struct {
	SourceIdentity string
	SourceSHA256   string
	Captures       []LegacyCapture
}

type LegacyImportResult struct {
	ImportID      string `json:"import_id,omitempty"`
	Status        string `json:"status"`
	CaptureCount  int    `json:"capture_count"`
	IncidentCount int    `json:"incident_count"`
	LessonCount   int    `json:"lesson_count"`
	RepairCount   int    `json:"repair_count"`
}

type LessonDocument struct {
	LessonID        string
	LessonVersionID string
	Signature       string
	Title           string
	Rule            string
	Prevention      string
	Verification    string
	Applicability   string
	Counterexamples string
	CauseLayer      string
	FailureMode     string
	Component       string
	Document        string
	CreatedAt       time.Time
}

type Context struct {
	Harness              string
	WorkspaceFingerprint string
	SessionFingerprint   string
	Transport            string
}
