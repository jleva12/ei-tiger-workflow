package deployment

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"net/url"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

func textID(s string, max int) bool {
	return s != "" && len(s) <= max && utf8.ValidString(s) && strings.TrimSpace(s) == s &&
		strings.IndexFunc(s, unicode.IsControl) < 0
}

func lowerHex(s string, bytes int) bool {
	if len(s) != bytes*2 || s != strings.ToLower(s) {
		return false
	}
	_, err := hex.DecodeString(s)
	return err == nil
}

// ValidDigest reports whether s is a sha256:<lowercase hex> digest.
func ValidDigest(s string) bool {
	return strings.HasPrefix(s, "sha256:") && lowerHex(strings.TrimPrefix(s, "sha256:"), 32)
}

// ValidCommit reports whether s is a full lowercase SHA-1 or SHA-256 commit.
func ValidCommit(s string) bool { return lowerHex(s, 20) || lowerHex(s, 32) }

// ValidBranch accepts a literal Git branch name, never a revision expression.
func ValidBranch(branch string) bool {
	if branch == "" || len(branch) > 1024 || !utf8.ValidString(branch) || branch == "@" || strings.HasPrefix(branch, "-") || strings.HasSuffix(branch, ".") || strings.Contains(branch, "..") || strings.Contains(branch, "@{") {
		return false
	}
	for _, c := range branch {
		if c <= ' ' || c == 127 || strings.ContainsRune("~^:?*[\\", c) {
			return false
		}
	}
	for _, part := range strings.Split(branch, "/") {
		if part == "" || strings.HasPrefix(part, ".") || strings.HasSuffix(part, ".lock") {
			return false
		}
	}
	return true
}

func validTime(t time.Time) bool { return !t.IsZero() && t.Year() >= 1 && t.Year() <= 9999 }

func (r Request) Validate() error {
	// Legacy published payloads remain readable; new admissions require Branch.
	if r.Branch != "" && !ValidBranch(r.Branch) {
		return fmt.Errorf("%w: invalid branch", ErrInvalidRequest)
	}
	if r.SchemaVersion != SchemaVersion || !textID(r.RepositoryID, 256) || !textID(r.DeploymentID, 256) ||
		r.DeploymentSequence == 0 || r.DeploymentSequence > math.MaxInt64 || !ValidCommit(r.TargetCommitSHA) ||
		!validTime(r.DeployedAt) || !ValidDigest(r.AnalysisConfigDigest) ||
		(r.TriggerKind != TriggerDeployment && r.TriggerKind != TriggerAnalysisRefresh) {
		return fmt.Errorf("%w: deployment identity, exact commit, sequence, time, configuration and trigger required", ErrInvalidRequest)
	}
	if r.RequestedBy != "" && !textID(r.RequestedBy, 255) {
		return fmt.Errorf("%w: requested_by", ErrInvalidRequest)
	}
	return nil
}

func (k RunKey) Validate() error {
	if !textID(k.RepositoryID, 256) || !textID(k.RunID, 128) {
		return fmt.Errorf("%w: run key", ErrInvalidRequest)
	}
	return nil
}

func (f Fence) Validate() error {
	if err := f.Key.Validate(); err != nil {
		return err
	}
	if f.Token == 0 {
		return fmt.Errorf("%w: positive fence required", ErrInvalidRequest)
	}
	return nil
}

func (l Lease) Validate() error {
	if err := l.Fence.Validate(); err != nil {
		return err
	}
	if !textID(l.OwnerID, 128) || !validTime(l.ExpiresAt) {
		return fmt.Errorf("%w: lease owner and expiry required", ErrInvalidRequest)
	}
	return nil
}

func (r Run) Validate() error {
	if r.SchemaVersion != SchemaVersion {
		return fmt.Errorf("%w: run schema version", ErrInvalidRequest)
	}
	if err := r.Key.Validate(); err != nil {
		return err
	}
	if err := r.Request.Validate(); err != nil {
		return err
	}
	if r.Request.RepositoryID != r.Key.RepositoryID {
		return fmt.Errorf("%w: run repository mismatch", ErrInvalidRequest)
	}
	if err := r.Phase.Validate(); err != nil {
		return err
	}
	if !validTime(r.AcceptedAt) || !validTime(r.UpdatedAt) || r.Revision > math.MaxInt64 || r.Generation > math.MaxInt64 || r.Attempts > math.MaxInt64 {
		return fmt.Errorf("%w: run times or counters", ErrInvalidRequest)
	}
	if r.BaselineCommit != "" && !ValidCommit(r.BaselineCommit) {
		return fmt.Errorf("%w: baseline commit", ErrInvalidRequest)
	}
	if (r.BaselineCommit == "") != (r.BaselineGeneration == 0) {
		return fmt.Errorf("%w: baseline commit and generation must agree", ErrInvalidRequest)
	}
	if r.ErrorCode != "" && !textID(r.ErrorCode, 128) {
		return fmt.Errorf("%w: error code", ErrInvalidRequest)
	}
	if len(r.ErrorMessage) > MaxErrorMessageBytes || !utf8.ValidString(r.ErrorMessage) {
		return fmt.Errorf("%w: error message", ErrInvalidRequest)
	}
	if len(r.WarningMessage) > MaxErrorMessageBytes || !utf8.ValidString(r.WarningMessage) {
		return fmt.Errorf("%w: warning message", ErrInvalidRequest)
	}
	if r.Index != nil {
		return r.Index.Validate()
	}
	return nil
}

// Validate checks an index state: a known status, valid times, a finished
// time only once the pass stopped, and a bounded error text.
func (s IndexState) Validate() error {
	switch s.Status {
	case IndexRunning, IndexComplete, IndexIncomplete:
	default:
		return fmt.Errorf("%w: index status", ErrInvalidRequest)
	}
	if !validTime(s.StartedAt) || s.Embedded > math.MaxInt64 || s.Requests > math.MaxInt64 || len(s.Error) > 512 {
		return fmt.Errorf("%w: index state", ErrInvalidRequest)
	}
	if s.FinishedAt != nil && (!validTime(*s.FinishedAt) || s.Status == IndexRunning) {
		return fmt.Errorf("%w: index finished time", ErrInvalidRequest)
	}
	if s.FinishedAt == nil && s.Status != IndexRunning {
		return fmt.Errorf("%w: finished index needs a finished time", ErrInvalidRequest)
	}
	return nil
}

func (r Repository) Validate() error {
	if r.Public && (r.GitHubRepositoryID != 0 || r.GitHubInstallationID != 0) {
		return fmt.Errorf("%w: public repositories must not carry GitHub App credentials", ErrInvalidRequest)
	}
	if r.GitHubRepositoryID < 0 || r.GitHubInstallationID < 0 || (r.GitHubRepositoryID == 0) != (r.GitHubInstallationID == 0) {
		return fmt.Errorf("%w: GitHub repository and installation IDs must be supplied together", ErrInvalidRequest)
	}
	if r.DefaultBranch != "" && !ValidBranch(r.DefaultBranch) {
		return fmt.Errorf("%w: invalid default branch", ErrInvalidRequest)
	}
	if r.LastIngestionCommit != "" && !ValidCommit(r.LastIngestionCommit) {
		return fmt.Errorf("%w: invalid last ingestion commit", ErrInvalidRequest)
	}
	if r.Branch != "" && !ValidBranch(r.Branch) {
		return fmt.Errorf("%w: invalid tracked branch", ErrInvalidRequest)
	}
	if r.SchemaVersion != SchemaVersion || !textID(r.RepositoryID, 256) || !textID(r.IntegrationID, 256) {
		return fmt.Errorf("%w: repository registration", ErrInvalidRequest)
	}
	u, err := url.Parse(r.GitHubURL)
	if err != nil || len(r.GitHubURL) > 2048 || u.Scheme != "https" || u.Host != "github.com" ||
		u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.RawPath != "" {
		return fmt.Errorf("%w: canonical GitHub locator required", ErrInvalidRequest)
	}
	parts := strings.Split(strings.TrimPrefix(u.Path, "/"), "/")
	if len(parts) != 2 {
		return fmt.Errorf("%w: GitHub owner and repository required", ErrInvalidRequest)
	}
	for _, part := range parts {
		if part == "" || part == "." || part == ".." || strings.IndexFunc(part, func(c rune) bool {
			return !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '_' || c == '.')
		}) >= 0 {
			return fmt.Errorf("%w: invalid GitHub locator", ErrInvalidRequest)
		}
	}
	return nil
}

func (a Admission) Validate() error {
	if !ValidBranch(a.Request.Branch) {
		return fmt.Errorf("%w: branch is required", ErrInvalidRequest)
	}
	if a.SubmissionID != "" && !textID(a.SubmissionID, 256) {
		return fmt.Errorf("%w: submission id", ErrInvalidRequest)
	}
	return a.Request.Validate()
}

// ValidateIngestion permits the store to assign omitted deployment metadata
// for a new ingestion. Explicit analysis refreshes retain their full identity.
// The persisted Request is still required to pass Validate.
func (a Admission) ValidateIngestion() error {
	if a.Request.TriggerKind == TriggerAnalysisRefresh {
		return a.Validate()
	}
	if a.Request.DeploymentID == "" {
		a.Request.DeploymentID = "generated"
	}
	if a.Request.DeploymentSequence == 0 {
		a.Request.DeploymentSequence = 1
	}
	if a.Request.DeployedAt.IsZero() {
		a.Request.DeployedAt = time.Unix(0, 0).UTC()
	}
	return a.Validate()
}

// SameDeployment compares immutable deployment fields only. Configuration and
// trigger belong to an analysis request, so an explicit refresh may vary them.
func (r Request) SameDeployment(other Request) bool {
	return r.SchemaVersion == other.SchemaVersion && r.RepositoryID == other.RepositoryID &&
		r.Branch == other.Branch &&
		r.DeploymentID == other.DeploymentID && r.DeploymentSequence == other.DeploymentSequence &&
		r.TargetCommitSHA == other.TargetCommitSHA && r.DeployedAt.Equal(other.DeployedAt)
}

func (g GenerationInputs) Validate() error {
	if g.SchemaVersion != SchemaVersion || g.ContextID == "" || len(g.ContextID) > 256 || !ValidDigest(g.AnalysisConfigDigest) {
		return fmt.Errorf("%w: generation inputs need a context, a configuration digest and the current schema", ErrInvalidRequest)
	}
	for id, digest := range g.SourceSets {
		if id == "" || len(id) > 256 || !ValidDigest(digest) {
			return fmt.Errorf("%w: generation inputs source set %q", ErrInvalidRequest, id)
		}
	}
	return nil
}

// SupersededBy reports whether a deployment at r may no longer publish once
// live is a deployment at live: its sequence is older, or the same sequence
// without being an analysis refresh of it. Sequence comes from the
// integration or automatic admission, so this is the only ordering that matters.
func (r Request) SupersededBy(live Request) bool {
	if r.DeploymentSequence < live.DeploymentSequence {
		return true
	}
	return r.DeploymentSequence == live.DeploymentSequence && r.TriggerKind != TriggerAnalysisRefresh
}

// IdentityDigest is the admission deduplication identity: identical analysis
// requests reuse their durable run even if a caller changes trigger wording.
func (r Request) IdentityDigest() (string, error) {
	if err := r.Validate(); err != nil {
		return "", err
	}
	canonical := struct {
		SchemaVersion        int       `json:"schema_version"`
		RepositoryID         string    `json:"repository_id"`
		Branch               string    `json:"branch,omitempty"`
		DeploymentID         string    `json:"deployment_id"`
		DeploymentSequence   uint64    `json:"deployment_sequence"`
		TargetCommitSHA      string    `json:"target_commit_sha"`
		DeployedAt           time.Time `json:"deployed_at"`
		AnalysisConfigDigest string    `json:"analysis_config_digest"`
	}{r.SchemaVersion, r.RepositoryID, r.Branch, r.DeploymentID, r.DeploymentSequence, r.TargetCommitSHA, r.DeployedAt.UTC(), r.AnalysisConfigDigest}
	data, err := json.Marshal(canonical)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}
