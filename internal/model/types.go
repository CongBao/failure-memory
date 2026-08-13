package model

import (
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
)

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

type CauseLayer string

type FailureMode string

type Confidence string

type MemoryTargetType string

type MemoryOutcome string

var classificationValues = []string{
	string(RequirementUpdate),
	string(RequirementClarification),
	string(PreferenceUpdate),
	string(RealFailure),
	string(Mixed),
	string(Uncertain),
}

var causeLayerValues = []string{
	"skill_instruction",
	"agent_instruction",
	"project_instruction",
	"system_instruction",
	"hook_policy",
	"plugin_manifest",
	"tool_contract",
	"application_logic",
	"adapter_runtime",
	"schema_migration",
	"test_evaluation_gap",
	"harness_limitation",
	"model_behavior",
	"external_dependency",
	"unknown",
}

var failureModeValues = []string{
	"missing",
	"ambiguous",
	"conflicting",
	"not_loaded",
	"not_triggered",
	"ignored",
	"incorrectly_implemented",
	"insufficient_validation",
	"uninspectable",
	"unknown",
}

var confidenceValues = []string{"low", "medium", "high", "unknown"}

var memoryTargetTypeValues = []string{"recall", "repair", "lesson"}

var memoryOutcomeValues = []string{
	"applied", "not_applicable", "already_known", "contradicted",
	"prevented_recurrence", "failed_to_prevent", "ignored", "unknown",
	"partially_applied", "rejected", "verified_effective",
	"verified_ineffective", "recurrence_observed", "superseded",
	"confirmed", "false_positive", "stale", "needs_generalization",
}

func ClassificationValues() []string {
	return append([]string(nil), classificationValues...)
}

func CauseLayerValues() []string {
	return append([]string(nil), causeLayerValues...)
}

func FailureModeValues() []string {
	return append([]string(nil), failureModeValues...)
}

func ConfidenceValues() []string {
	return append([]string(nil), confidenceValues...)
}

func MemoryTargetTypeValues() []string {
	return append([]string(nil), memoryTargetTypeValues...)
}

func MemoryOutcomeValues() []string {
	return append([]string(nil), memoryOutcomeValues...)
}

func (c *Confidence) UnmarshalJSON(data []byte) error {
	var text string
	if err := json.Unmarshal(data, &text); err == nil {
		text = strings.ToLower(strings.TrimSpace(text))
		if text == "" {
			*c = ""
			return nil
		}
		if contains(confidenceValues, text) {
			*c = Confidence(text)
			return nil
		}
		value, parseErr := strconv.ParseFloat(text, 64)
		if parseErr != nil {
			return fmt.Errorf("confidence must be low, medium, high, unknown, or a number from 0 to 1")
		}
		return c.setNumeric(value)
	}
	var value float64
	if err := json.Unmarshal(data, &value); err != nil {
		return errors.New("confidence must be a string or number from 0 to 1")
	}
	return c.setNumeric(value)
}

func (c *Confidence) setNumeric(value float64) error {
	if value < 0 || value > 1 {
		return errors.New("numeric confidence must be between 0 and 1")
	}
	switch {
	case value >= 0.8:
		*c = "high"
	case value >= 0.5:
		*c = "medium"
	default:
		*c = "low"
	}
	return nil
}

func contains(values []string, value string) bool {
	for _, candidate := range values {
		if value == candidate {
			return true
		}
	}
	return false
}

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
	Layer             CauseLayer  `json:"layer" jsonschema:"Canonical controllable cause layer."`
	FailureMode       FailureMode `json:"failure_mode" jsonschema:"Canonical failure mode."`
	Component         string      `json:"component" jsonschema:"Specific controllable component."`
	Evidence          string      `json:"evidence" jsonschema:"Evidence supporting this cause rather than speculation."`
	RecommendedChange string      `json:"recommended_change" jsonschema:"Where and how to fix the cause."`
	Verification      string      `json:"verification" jsonschema:"How to verify the repair."`
	Confidence        Confidence  `json:"confidence,omitempty" jsonschema:"Prefer low, medium, high, or unknown. Numeric 0 to 1 is also accepted and normalized."`
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
	CorrectionOf   string               `json:"correction_of_capture_event_id,omitempty" jsonschema:"Set only for the one correction retry requested by a prior retryable response."`
}

type RememberCorrection struct {
	CorrectionOfCaptureEventID string              `json:"correction_of_capture_event_id"`
	AllowedValues              map[string][]string `json:"allowed_values"`
}

type RememberResult struct {
	OperationID        string              `json:"operation_id"`
	Status             RememberStatus      `json:"status"`
	Decision           Decision            `json:"decision"`
	ReasonCodes        []string            `json:"reason_codes"`
	Deduplication      string              `json:"deduplication_status"`
	SemanticStatus     string              `json:"semantic_status"`
	TotalLatencyMS     int64               `json:"total_latency_ms"`
	CaptureEventID     string              `json:"capture_event_id"`
	IncidentID         string              `json:"incident_id,omitempty"`
	LessonID           string              `json:"lesson_id,omitempty"`
	LessonVersionID    string              `json:"lesson_version_id,omitempty"`
	RepairID           string              `json:"repair_recommendation_id,omitempty"`
	GeneralizationHint string              `json:"generalization_hint,omitempty"`
	Retryable          bool                `json:"retryable,omitempty"`
	Correction         *RememberCorrection `json:"correction,omitempty"`
}

type RecallInput struct {
	Text              string            `json:"text" jsonschema:"Compact task evidence, not a raw prompt."`
	ExpectedInvariant string            `json:"expected_invariant,omitempty"`
	ControllableCause string            `json:"controllable_cause,omitempty"`
	PreventionAction  string            `json:"prevention_action,omitempty"`
	Component         string            `json:"component,omitempty"`
	Mode              string            `json:"mode,omitempty" jsonschema:"auto, exact, lexical, semantic, or hybrid."`
	TopK              int               `json:"top_k,omitempty" jsonschema:"Maximum lessons to return, from 1 to 3."`
	MinRelevance      float64           `json:"min_relevance,omitempty" jsonschema:"Minimum calibrated relevance from 0 to 1; zero or omitted uses the retrieval profile default."`
	Representatives   map[string]string `json:"-"`
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
	RelevanceScore   float64  `json:"relevance_score"`
	RetrievalReasons []string `json:"retrieval_reasons"`
}

type RecallResult struct {
	AttemptID              string   `json:"attempt_id"`
	Mode                   string   `json:"mode"`
	SemanticStatus         string   `json:"semantic_status"`
	Lessons                []Lesson `json:"lessons"`
	TotalLatencyMS         int64    `json:"total_latency_ms"`
	CandidateCount         int      `json:"candidate_count"`
	RetrievedCount         int      `json:"retrieved_count"`
	FilteredBelowThreshold int      `json:"filtered_below_threshold"`
	CollapsedByCluster     int      `json:"collapsed_by_cluster"`
	TrimmedByAdaptiveLimit int      `json:"trimmed_by_adaptive_limit"`
	AppliedTopK            int      `json:"applied_top_k"`
	AppliedMinRelevance    float64  `json:"applied_min_relevance"`
	AbstentionReason       string   `json:"abstention_reason,omitempty"`
	StoreScope             string   `json:"store_scope"`
}

type OutcomeResult struct {
	EventID         string `json:"event_id"`
	Status          string `json:"status"`
	Duplicate       bool   `json:"duplicate,omitempty"`
	LessonID        string `json:"lesson_id,omitempty"`
	LessonVersionID string `json:"lesson_version_id,omitempty"`
	RetrievalStatus string `json:"retrieval_status,omitempty"`
}

type MemoryOutcomeInput struct {
	TargetType       MemoryTargetType `json:"target_type" jsonschema:"recall, repair, or lesson."`
	TargetID         string           `json:"target_id" jsonschema:"Recall attempt, repair recommendation, or lesson version identifier."`
	Outcome          MemoryOutcome    `json:"outcome" jsonschema:"Evidence-bounded outcome for the selected target type."`
	LessonVersionIDs []string         `json:"lesson_version_ids,omitempty"`
	EvidenceCode     string           `json:"evidence_code" jsonschema:"Compact code for the evidence supporting this outcome."`
	Confidence       float64          `json:"confidence,omitempty"`
	IdempotencyKey   string           `json:"idempotency_key,omitempty" jsonschema:"Optional retry key; a deterministic key is derived when omitted."`
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
	RunID                    string                  `json:"run_id"`
	ClusterKey               string                  `json:"cluster_key"`
	Decision                 string                  `json:"decision" jsonschema:"accept, reject, or defer."`
	RationaleCode            string                  `json:"rationale_code"`
	SupportingLessonVersions []string                `json:"supporting_lesson_version_ids"`
	CounterexampleVersions   []string                `json:"counterexample_lesson_version_ids,omitempty"`
	GeneralizedLesson        *GeneralizedLessonInput `json:"generalized_lesson,omitempty"`
}

type GeneralizedLessonInput struct {
	Title           string      `json:"title"`
	Rule            string      `json:"rule"`
	Prevention      string      `json:"prevention"`
	Verification    string      `json:"verification"`
	Applicability   string      `json:"applicability,omitempty"`
	Counterexamples string      `json:"counterexamples,omitempty"`
	CauseLayer      CauseLayer  `json:"cause_layer"`
	FailureMode     FailureMode `json:"failure_mode"`
	Component       string      `json:"component"`
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
