// Package deployment defines the ordered ingestion lifecycle: an admitted
// request for an exact commit, the run that executes it, the repository lease
// that serializes runs per repository, and the audit metrics a run leaves.
package deployment

import "time"

const SchemaVersion = 2

type TriggerKind string

const (
	TriggerDeployment      TriggerKind = "deployment"
	TriggerAnalysisRefresh TriggerKind = "analysis_refresh"
)

// Request is an immutable ingestion identity. Integrations supply their
// deployment sequence; manual ingestion assigns the next sequence atomically.
// AnalysisConfigDigest fingerprints the worker configuration that must
// execute it, so incompatible workers never claim it.
type Request struct {
	SchemaVersion        int         `json:"schema_version"`
	RepositoryID         string      `json:"repository_id"`
	Branch               string      `json:"branch,omitempty"`
	DeploymentID         string      `json:"deployment_id"`
	DeploymentSequence   uint64      `json:"deployment_sequence"`
	TargetCommitSHA      string      `json:"target_commit_sha"`
	DeployedAt           time.Time   `json:"deployed_at"`
	AnalysisConfigDigest string      `json:"analysis_config_digest"`
	TriggerKind          TriggerKind `json:"trigger_kind"`
	// RequestedBy is the Forge user who submitted the request. The worker
	// fetches the repository with that user's connected GitHub account. It is
	// attribution, not identity: IdentityDigest leaves it out, so the same
	// deployment submitted by two users is one run.
	RequestedBy string `json:"requested_by,omitempty"`
}

type RunKey struct {
	RepositoryID string `json:"repository_id"`
	RunID        string `json:"run_id"`
}

// Fence tokens increase monotonically per repository. Every durable write a
// run performs checks the current fence and lease with database time.
type Fence struct {
	Key   RunKey `json:"key"`
	Token uint64 `json:"token"`
}

type Lease struct {
	Fence     Fence     `json:"fence"`
	OwnerID   string    `json:"owner_id"`
	ExpiresAt time.Time `json:"expires_at"`
}

// Metrics is the bounded audit a run leaves behind. Durations are stage
// names to milliseconds.
type Metrics struct {
	Files         uint64 `json:"files"`
	AffectedFiles uint64 `json:"affected_files"`
	Contexts      uint64 `json:"contexts"`
	Symbols       uint64 `json:"symbols"`
	Lookups       uint64 `json:"lookups"`
	Resolved      uint64 `json:"resolved"`
	Unresolved    uint64 `json:"unresolved"`
	Ambiguous     uint64 `json:"ambiguous"`
	Unsupported   uint64 `json:"unsupported"`
	Nodes         uint64 `json:"nodes"`
	Edges         uint64 `json:"edges"`
	Added         uint64 `json:"added"`
	Updated       uint64 `json:"updated"`
	Retired       uint64 `json:"retired"`
	Reopened      uint64 `json:"reopened"`
	Embedded      uint64 `json:"embedded"`
	// InvalidatedContexts counts compilation contexts recomputed because
	// their build inputs changed without any source change.
	InvalidatedContexts uint64           `json:"invalidated_contexts,omitempty"`
	Durations           map[string]int64 `json:"durations,omitempty"`
}

// GenerationInputs records what a published generation was computed from:
// the sealed build context, the analysis configuration, and one digest per
// compilation context covering everything attribution depends on besides the
// source files themselves (dependencies and their content, the JDK, compiler
// settings, generated roots). The next run compares its own digests with the
// live generation's to find contexts whose inputs changed without a source
// change, such as a POM-only commit.
type GenerationInputs struct {
	SchemaVersion        int               `json:"schema_version"`
	ContextID            string            `json:"context_id"`
	AnalysisConfigDigest string            `json:"analysis_config_digest"`
	SourceSets           map[string]string `json:"source_sets"`
}

// Run is the operational record of one admitted request. Generation is
// assigned when loading starts and becomes live at graph publication.
// Baseline* capture the live state the run diffed against.
type Run struct {
	SchemaVersion      int        `json:"schema_version"`
	Key                RunKey     `json:"key"`
	Request            Request    `json:"request"`
	Phase              Phase      `json:"phase"`
	Revision           uint64     `json:"revision"`
	Generation         uint64     `json:"generation,omitempty"`
	BaselineGeneration uint64     `json:"baseline_generation,omitempty"`
	BaselineCommit     string     `json:"baseline_commit,omitempty"`
	AcceptedAt         time.Time  `json:"accepted_at"`
	UpdatedAt          time.Time  `json:"updated_at"`
	StartedAt          *time.Time `json:"started_at,omitempty"`
	FinishedAt         *time.Time `json:"finished_at,omitempty"`
	Attempts           uint64     `json:"attempts"`
	// FailureRetryable records whether the failure was transient. Nil means
	// not classified. The retry budget is the job's, in the queue (the Forge
	// admin API's code_ingestion_jobs), not the run's.
	FailureRetryable *bool  `json:"failure_retryable,omitempty"`
	ErrorCode        string `json:"error_code,omitempty"`
	// ErrorMessage says why the last attempt failed, for people reading the
	// run; see FailureMessage. Empty while running and after success.
	ErrorMessage string `json:"error_message,omitempty"`
	// WarningMessage says what the last attempt could not analyse although
	// it went on, such as sources the build failed to compile, for people
	// reading the run; bounded and masked like ErrorMessage. Empty when
	// nothing was left out.
	WarningMessage string   `json:"warning_message,omitempty"`
	Metrics        *Metrics `json:"metrics,omitempty"`
	// Index reports the embedding pass that follows publication; nil when
	// the worker has no embedding provider.
	Index *IndexState `json:"index,omitempty"`
}

// IndexStatus is the state of the semantic index pass of a published
// generation.
type IndexStatus string

const (
	IndexRunning    IndexStatus = "RUNNING"
	IndexComplete   IndexStatus = "COMPLETE"
	IndexIncomplete IndexStatus = "INCOMPLETE"
)

// IndexState describes the embedding pass of a published generation. The
// graph is live from Publish on, but ingestion only succeeds once the required
// index is COMPLETE. An INCOMPLETE pass fails the run; a retry resumes missing
// vectors on the same generation. Counts include completed earlier attempts.
type IndexState struct {
	Model      string      `json:"model,omitempty"`
	Dimensions int         `json:"dimensions,omitempty"`
	Status     IndexStatus `json:"status"`
	Embedded   uint64      `json:"embedded"`
	Requests   uint64      `json:"requests,omitempty"`
	StartedAt  time.Time   `json:"started_at"`
	FinishedAt *time.Time  `json:"finished_at,omitempty"`
	Error      string      `json:"error,omitempty"`
}

// Repository maps a stable internal identity to its canonical locator.
// Credentials are never registry data.
type Repository struct {
	TenantID      string `json:"tenant_id,omitempty"`
	SchemaVersion int    `json:"schema_version"`
	RepositoryID  string `json:"repository_id"`
	GitHubURL     string `json:"github_url"`
	// Public selects anonymous GitHub access; false preserves credentialed access.
	Public               bool   `json:"public,omitempty"`
	Branch               string `json:"branch,omitempty"`
	LastIngestionCommit  string `json:"last_ingestion_commit,omitempty"`
	GitHubRepositoryID   int64  `json:"github_repository_id,omitempty"`
	GitHubInstallationID int64  `json:"github_installation_id,omitempty"`
	DefaultBranch        string `json:"default_branch,omitempty"`
	IntegrationID        string `json:"integration_id"`
	Revision             uint64 `json:"revision"`
}

// Admission is the request plus the caller's correlation ID. Identical
// requests reuse their durable run.
type Admission struct {
	SubmissionID string  `json:"submission_id,omitempty"`
	Request      Request `json:"request"`
	// CheckedLiveCommit binds an upstream ancestry check to the publication
	// observed by the API. It is internal; clients cannot supply this proof.
	CheckedLiveCommit      *string `json:"-"`
	CheckedIngestionCommit *string `json:"-"`
}

type AdmissionResult struct {
	Run    Run  `json:"run"`
	Reused bool `json:"reused"`
}
