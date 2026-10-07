// Package authorization evaluates tenant-scoped permissions over immutable
// Casbin snapshots. Only the identity Provider may supply corporate membership.
package authorization

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"time"
)

var (
	ErrUnauthenticated = errors.New("authentication required")
	ErrForbidden       = errors.New("permission denied")
	ErrNotFound        = errors.New("resource unavailable")
	ErrUnavailable     = errors.New("authorization temporarily unavailable")
	ErrInvalid         = errors.New("invalid authorization configuration")
	ErrConflict        = errors.New("configuration changed; reload before saving")
)

// Identity is backend-only. Provider implementations must verify signature,
// issuer, API audience, expiration and the corporate-to-application tenant map.
// Complete empty membership is valid; missing/truncated membership is not.
type Identity struct {
	SubjectID string
	Issuer    string
	TenantID  string
	// DisplayName and Email are directory attributes recorded in the user
	// registry so administrators can recognize people. They never take part in
	// an authorization decision.
	DisplayName        string
	Email              string
	GroupKeys          []string
	MembershipVersion  string
	MembershipComplete bool
	MembershipValidTo  time.Time
	AuthenticatedUntil time.Time
	// SiteAdministrator is verified by the Provider for this deployment's
	// organization. It only makes explicit site_configuration grants eligible;
	// it does not confer tenant data, graph, or access-policy permissions.
	SiteAdministrator bool
}

// Provider is the company integration seam. Never implement it by accepting
// identity/group headers or request-body claims without server verification.
// Token-only implementations must explicitly bound MembershipValidTo by the
// accepted token refresh/revocation window. Directory caches must expire stale.
type Provider interface {
	Authenticate(context.Context, *http.Request) (Identity, error)
	VerifyExternalGroup(context.Context, string, ExternalGroup) (ExternalGroup, error)
}

// UserDirectory is an optional Provider extension. It verifies one person
// against the corporate directory so an administrator can add them to a team
// before their first sign-in. Without it, people appear only after signing in.
type UserDirectory interface {
	VerifyUser(context.Context, string, User) (User, error)
}

// UnconfiguredProvider deliberately grants nothing until the company adapter
// is installed. Missing credentials are 401; a missing integration is 503.
type UnconfiguredProvider struct{}

func (UnconfiguredProvider) Authenticate(_ context.Context, r *http.Request) (Identity, error) {
	if r.Header.Get("Authorization") == "" && r.Header.Get("Cookie") == "" {
		return Identity{}, ErrUnauthenticated
	}
	return Identity{}, ErrUnavailable
}
func (UnconfiguredProvider) VerifyExternalGroup(context.Context, string, ExternalGroup) (ExternalGroup, error) {
	return ExternalGroup{}, ErrUnavailable
}

func stableKey(prefix string, tuple ...string) string {
	b, _ := json.Marshal(tuple)
	digest := sha256.Sum256(b)
	return prefix + hex.EncodeToString(digest[:])
}
func SubjectKey(issuer, subject string) string { return stableKey("user:", issuer, subject) }
func GroupKey(issuer, corporateTenant, externalID string) string {
	return stableKey("group:", issuer, corporateTenant, externalID)
}

// Principal is the resolved caller: roles from verified corporate groups and
// team memberships from the application's own registry.
type Principal struct {
	SubjectID, TenantID string
	RoleIDs             []string
	// Memberships maps membershipKey(tenant, team) to the caller's team role.
	Memberships map[string]string
}

// Resource describes the record a decision is about. Team and Teams are set by
// the organization store: Team when the record is a team, Teams when the record
// is owned by teams (a workspace's selected teams, a responsibility's team).
type Resource struct {
	Kind, ID, TenantID, OwnerID, Status, Version string
	Team                                         string
	Teams                                        []string
}
type Decision struct {
	Allowed        bool   `json:"allowed"`
	ReasonCode     string `json:"reasonCode"`
	PolicyRevision uint64 `json:"policyRevision"`
}
type Metadata struct {
	PolicyRevision    uint64    `json:"policyRevision"`
	ResourceVersion   string    `json:"resourceVersion,omitempty"`
	MembershipVersion string    `json:"membershipVersion"`
	ValidUntil        time.Time `json:"validUntil"`
}
type ExternalGroup struct {
	Key             string    `json:"key" gorm:"column:group_key;primaryKey"`
	Issuer          string    `json:"issuer"`
	CorporateTenant string    `json:"corporateTenant"`
	ExternalID      string    `json:"externalId"`
	DisplayName     string    `json:"displayName"`
	LastVerifiedAt  time.Time `json:"lastVerifiedAt"`
}

// User is a person known to the application. Rows are recorded when a verified
// identity signs in and when an administrator adds a directory-verified person.
type User struct {
	SubjectID   string    `json:"subjectId" gorm:"primaryKey"`
	TenantID    string    `json:"tenantId"`
	Issuer      string    `json:"issuer"`
	ExternalID  string    `json:"externalId"`
	DisplayName string    `json:"displayName"`
	Email       string    `json:"email"`
	Status      string    `json:"status"`
	Source      string    `json:"source"`
	FirstSeenAt time.Time `json:"firstSeenAt"`
	LastSeenAt  time.Time `json:"lastSeenAt"`
}

const (
	UserActive   = "active"
	UserDisabled = "disabled"
	UserSignedIn = "sign_in"
	UserInvited  = "invited"
	MemberRole   = "member"
	LeadRole     = "lead"
)

// Membership records that a person belongs to a team. Leads may manage their
// own team's roster; membership alone grants entry, not administration.
type Membership struct {
	TenantID  string    `json:"tenantId"`
	TeamID    string    `json:"teamId"`
	SubjectID string    `json:"subjectId"`
	Role      string    `json:"role"`
	AddedBy   string    `json:"addedBy"`
	AddedAt   time.Time `json:"addedAt"`
}

type Permission struct {
	Kind        string   `json:"kind"`
	Action      string   `json:"action"`
	Description string   `json:"description"`
	Collection  bool     `json:"collection"`
	Conditions  []string `json:"conditions"`
}

// Catalog is executable/versioned. Changes need a matching catalog migration.
// Repositories have no user owner or workflow status, so accept only tenant.
var catalog = []Permission{
	{"site_configuration", "read", "View organization configuration across this site", true, []string{"tenant"}},
	{"site_configuration", "write", "Manage tenants, platforms and teams across this site", true, []string{"tenant"}},
	{"authorization_config", "read", "View access management", true, []string{"tenant"}},
	{"authorization_config", "write", "Change access management", true, []string{"tenant"}},
	{"organization_collection", "list", "Browse organization", true, []string{"tenant"}},
	{"organization_collection", "create", "Create organization records", true, []string{"tenant"}},
	{"organization", "read", "Read organization records", false, []string{"tenant", "owner", "owner_draft", "tenant_draft"}},
	{"organization", "enter", "Enter a team's working context", false, []string{"tenant", "owner"}},
	{"organization", "manage_members", "Manage a team's members", false, []string{"tenant", "owner"}},
	{"organization", "update", "Edit organization records", false, []string{"tenant", "owner", "owner_draft", "tenant_draft"}},
	{"organization", "transition", "Change organization workflow state", false, []string{"tenant", "owner"}},
	{"organization", "archive", "Archive organization records", false, []string{"tenant", "owner"}},
	{"team_collection", "enter", "Open a team workspace", true, []string{"tenant"}},
	{"user_directory", "read", "Find people in the user directory", true, []string{"tenant"}},
	{"user_directory", "invite", "Add verified people to the user directory", true, []string{"tenant"}},
	{"repository_collection", "list", "Browse registered repositories", true, []string{"tenant"}},
	{"repository_collection", "register", "Register repositories", true, []string{"tenant"}},
	{"repository", "read", "Read repository graphs and runs", false, []string{"tenant"}},
	{"repository", "update", "Change repository access settings", false, []string{"tenant"}},
	{"repository", "ingest", "Submit or retry repository ingestion", false, []string{"tenant"}},
	{"ingestion_collection", "list", "Browse ingestion activity", true, []string{"tenant"}},
	{"ingestion_collection", "create", "Submit ingestion", true, []string{"tenant"}},
	{"operations", "read", "View worker configuration", true, []string{"tenant"}},
	{"github", "connect", "Connect a GitHub account", true, []string{"tenant"}},
	{"assistant", "use", "Use the code assistant", true, []string{"tenant"}},
}

func Catalog() []Permission {
	out := append([]Permission(nil), catalog...)
	for i := range out {
		out[i].Conditions = append([]string(nil), out[i].Conditions...)
	}
	return out
}
func permission(kind, action string) (Permission, bool) {
	for _, p := range catalog {
		if p.Kind == kind && p.Action == action {
			return p, true
		}
	}
	return Permission{}, false
}
