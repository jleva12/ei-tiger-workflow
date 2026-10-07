package ir

type Severity string

const (
	SeverityInfo    Severity = "info"
	SeverityWarning Severity = "warning"
	SeverityError   Severity = "error"
)

type Diagnostic struct {
	Code     string   `json:"code"`
	Severity Severity `json:"severity"`
	Message  string   `json:"message"`
	Span     *Span    `json:"span,omitempty"`
	Feature  string   `json:"feature,omitempty"`
}

type ExtractionStatus string

const (
	ExtractionComplete ExtractionStatus = "complete"
	ExtractionPartial  ExtractionStatus = "partial"
	ExtractionFailed   ExtractionStatus = "failed"
)

// Complete is relative to the producer's declared extraction feature set.
// It is not a guarantee that every possible Java/runtime dependency was found.
type ExtractionCoverage struct {
	Status     ExtractionStatus `json:"status"`
	FeatureSet string           `json:"feature_set"`
	Issues     []CoverageIssue  `json:"issues,omitempty"`
}

type CoverageReason string

const (
	CoverageUnsupported CoverageReason = "unsupported"
	CoverageParseError  CoverageReason = "parse_error"
	CoverageLimit       CoverageReason = "resource_limit"
	CoverageSkipped     CoverageReason = "skipped"
)

// Count is nil when loss cannot be counted, distinct from a known zero.
// This records extraction losses; binding and graph-projection losses belong
// in their respective later-stage results.
type CoverageIssue struct {
	Feature string         `json:"feature"`
	Reason  CoverageReason `json:"reason"`
	Message string         `json:"message"`
	Span    *Span          `json:"span,omitempty"`
	Count   *uint64        `json:"count,omitempty"`
}
