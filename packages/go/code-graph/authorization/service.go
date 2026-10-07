package authorization

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/casbin/casbin/v3"
)

type Snapshot struct {
	enforcer *casbin.SyncedEnforcer
	// members indexes team membership by subject, then by tenant and team.
	members      map[string]map[string]string
	Revision     uint64
	ModelVersion string
	ConfirmedAt  time.Time
}

func membershipKey(tenant, team string) string { return tenant + "\x00" + team }
func membershipIndex(members []Membership) map[string]map[string]string {
	out := map[string]map[string]string{}
	for _, m := range members {
		if out[m.SubjectID] == nil {
			out[m.SubjectID] = map[string]string{}
		}
		out[m.SubjectID][membershipKey(m.TenantID, m.TeamID)] = m.Role
	}
	return out
}

type SnapshotStore interface {
	Revision(context.Context) (uint64, error)
	Load(context.Context) (*Snapshot, error)
}
type Options struct{ PollInterval, MaxAge, RequestTimeout time.Duration }

func DefaultOptions() Options { return Options{5 * time.Second, 30 * time.Second, 25 * time.Second} }

type decisionStat struct {
	count   atomic.Uint64
	nanos   atomic.Uint64
	buckets [7]atomic.Uint64
}

var latencyBounds = [...]time.Duration{100 * time.Microsecond, 500 * time.Microsecond, time.Millisecond, 5 * time.Millisecond, 10 * time.Millisecond, 50 * time.Millisecond, time.Second}

type Service struct {
	decisionStats          sync.Map
	MembershipFailures     atomic.Uint64
	ConfigurationConflicts atomic.Uint64
	store                  SnapshotStore
	options                Options
	current                atomic.Pointer[Snapshot]
	known                  atomic.Uint64
	reload                 sync.Mutex
	logger                 *slog.Logger
	signIns                signIns
	Decisions              atomic.Uint64
	Denials                atomic.Uint64
	EvaluationErrors       atomic.Uint64
	ReloadFailures         atomic.Uint64
	OutboxBacklog          atomic.Int64
}

func NewService(store SnapshotStore, options Options, logger *slog.Logger) (*Service, error) {
	if store == nil || options.PollInterval <= 0 || options.MaxAge < options.PollInterval || options.RequestTimeout <= 0 || options.RequestTimeout > options.MaxAge {
		return nil, ErrInvalid
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &Service{store: store, options: options, logger: logger}, nil
}

// Refresh confirms freshness even if the revision did not change. A known newer
// revision immediately makes the old snapshot ineligible if rebuilding fails.
func (s *Service) Refresh(ctx context.Context) error {
	s.reload.Lock()
	defer s.reload.Unlock()
	if outbox, ok := s.store.(interface {
		OutboxBacklog(context.Context) (int64, error)
	}); ok {
		if pending, err := outbox.OutboxBacklog(ctx); err == nil {
			s.OutboxBacklog.Store(pending)
		}
	}
	revision, err := s.store.Revision(ctx)
	if err != nil {
		s.ReloadFailures.Add(1)
		return ErrUnavailable
	}
	if revision > s.known.Load() {
		s.ObserveRevision(revision)
	}
	old := s.current.Load()
	if old != nil && revision < old.Revision {
		return ErrUnavailable
	}
	if old != nil && revision == old.Revision {
		copy := *old
		copy.ConfirmedAt = time.Now()
		s.current.Store(&copy)
		return nil
	}
	next, err := s.store.Load(ctx)
	if err != nil || next == nil || next.ModelVersion != ModelVersion || next.Revision < s.known.Load() {
		s.ReloadFailures.Add(1)
		return ErrUnavailable
	}
	s.ObserveRevision(next.Revision)
	s.current.Store(next)
	return nil
}
func (s *Service) Run(ctx context.Context) {
	for {
		timer := time.NewTimer(s.options.PollInterval * time.Duration(900+rand.IntN(201)) / 1000)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
		check, cancel := context.WithTimeout(ctx, s.options.PollInterval)
		if err := s.Refresh(check); err != nil {
			s.logger.WarnContext(ctx, "authorization refresh failed")
		}
		cancel()
	}
}
func (s *Service) Ready() error {
	p := s.current.Load()
	if p == nil || p.Revision < s.known.Load() || time.Since(p.ConfirmedAt) >= s.options.MaxAge {
		return ErrUnavailable
	}
	return nil
}
func (s *Service) AppliedRevision() uint64 {
	if p := s.current.Load(); p != nil {
		return p.Revision
	}
	return 0
}
func (s *Service) ObserveRevision(revision uint64) {
	for {
		old := s.known.Load()
		if revision <= old || s.known.CompareAndSwap(old, revision) {
			return
		}
	}
}

type execution struct {
	identity  Identity
	principal Principal
	snapshot  *Snapshot
	deadline  time.Time
	service   *Service
}
type executionKey struct{}

func resolve(snapshot *Snapshot, identity Identity) (Principal, error) {
	p := Principal{SubjectID: identity.SubjectID, TenantID: identity.TenantID}
	for _, group := range identity.GroupKeys {
		if !strings.HasPrefix(group, "group:") {
			return p, ErrUnavailable
		}
		roles, err := snapshot.enforcer.GetImplicitRolesForUser(group, identity.TenantID)
		if err != nil {
			return p, ErrUnavailable
		}
		p.RoleIDs = append(p.RoleIDs, roles...)
	}
	slices.Sort(p.RoleIDs)
	p.RoleIDs = slices.Compact(p.RoleIDs)
	p.Memberships = map[string]string{}
	for key, role := range snapshot.members[identity.SubjectID] {
		p.Memberships[key] = role
	}
	return p, nil
}
func validateIdentity(i Identity, now time.Time) error {
	if i.SubjectID == "" || i.Issuer == "" || i.TenantID == "" || !now.Before(i.AuthenticatedUntil) {
		return ErrUnauthenticated
	}
	if !i.MembershipComplete || i.MembershipVersion == "" || !now.Before(i.MembershipValidTo) || len(i.GroupKeys) > 10000 {
		return ErrUnavailable
	}
	return nil
}

// Pin is also the service/job entry point. The caller must obtain identity from
// Provider; the returned context expires and is never an authorization token.
func (s *Service) Pin(ctx context.Context, i Identity) (context.Context, error) {
	now := time.Now()
	if err := validateIdentity(i, now); err != nil {
		if errors.Is(err, ErrUnavailable) {
			s.MembershipFailures.Add(1)
		}
		return nil, err
	}
	if err := s.Ready(); err != nil {
		return nil, err
	}
	snapshot := s.current.Load()
	p, err := resolve(snapshot, i)
	if err != nil {
		return nil, err
	}
	deadline := now.Add(s.options.RequestTimeout)
	for _, t := range []time.Time{snapshot.ConfirmedAt.Add(s.options.MaxAge), i.MembershipValidTo, i.AuthenticatedUntil} {
		if t.Before(deadline) {
			deadline = t
		}
	}
	if t, ok := ctx.Deadline(); ok && t.Before(deadline) {
		deadline = t
	}
	i.GroupKeys = append([]string(nil), i.GroupKeys...)
	return context.WithValue(ctx, executionKey{}, &execution{i, p, snapshot, deadline, s}), nil
}
func operation(ctx context.Context) (*execution, error) {
	x, ok := ctx.Value(executionKey{}).(*execution)
	if !ok {
		return nil, ErrUnauthenticated
	}
	if ctx.Err() != nil || !time.Now().Before(x.deadline) || x.snapshot.Revision < x.service.known.Load() {
		return nil, ErrUnavailable
	}
	if err := validateIdentity(x.identity, time.Now()); err != nil {
		return nil, err
	}
	return x, nil
}
func HasContext(ctx context.Context) bool { _, ok := ctx.Value(executionKey{}).(*execution); return ok }
func CurrentIdentity(ctx context.Context) (Identity, error) {
	x, err := operation(ctx)
	if err != nil {
		return Identity{}, err
	}
	i := x.identity
	i.GroupKeys = append([]string(nil), i.GroupKeys...)
	return i, nil
}
func Check(ctx context.Context, resource Resource, action string) (Decision, error) {
	x, err := operation(ctx)
	if err != nil {
		return Decision{ReasonCode: "unavailable"}, err
	}
	d := Decision{PolicyRevision: x.snapshot.Revision, ReasonCode: "denied"}
	if _, ok := permission(resource.Kind, action); !ok {
		return d, ErrInvalid
	}
	if resource.Kind == "site_configuration" && !x.identity.SiteAdministrator {
		return d, nil
	}
	// Site configuration is a separate, explicitly granted authority. Never
	// extend this to repository, assistant, or tenant access-policy operations.
	if resource.TenantID != "" {
		if siteAction := siteConfigurationAction(resource.Kind, action); siteAction != "" {
			allowed, err := CanAdministerSite(ctx, siteAction)
			if err != nil {
				return d, err
			}
			if allowed {
				d.Allowed, d.ReasonCode = true, "site_configuration"
				return d, nil
			}
		}
	}
	// Team membership is the application's own registry: it opens the team
	// workspace and team-owned records without a policy grant, and never
	// extends to repositories, ingestion, the assistant or access policy.
	if resource.Kind == "team_collection" && action == "enter" && resource.TenantID == x.principal.TenantID && len(x.principal.Memberships) > 0 {
		d.Allowed, d.ReasonCode = true, "team_membership"
		return d, nil
	}
	if membershipAllows(x.principal, resource, action) {
		d.Allowed, d.ReasonCode = true, "team_membership"
		return d, nil
	}
	x.service.Decisions.Add(1)
	started := time.Now()
	allowed, err := x.snapshot.enforcer.Enforce(x.principal, x.principal.TenantID, resource, action)
	outcome := "denied"
	if allowed {
		outcome = "allowed"
	}
	if err != nil {
		outcome = "error"
	}
	stats, _ := x.service.decisionStats.LoadOrStore(fmt.Sprintf("kind=%q,action=%q,outcome=%q", resource.Kind, action, outcome), &decisionStat{})
	stat := stats.(*decisionStat)
	elapsed := time.Since(started)
	stat.count.Add(1)
	stat.nanos.Add(uint64(elapsed))
	for idx, bound := range latencyBounds {
		if elapsed <= bound {
			stat.buckets[idx].Add(1)
		}
	}
	if err != nil {
		x.service.EvaluationErrors.Add(1)
		x.service.logger.ErrorContext(ctx, "authorization evaluation failed", "actor", x.principal.SubjectID, "tenant", x.principal.TenantID, "kind", resource.Kind, "action", action, "resource", resource.ID, "revision", d.PolicyRevision, "request_id", RequestID(ctx))
		return d, ErrUnavailable
	}
	d.Allowed = allowed
	if allowed {
		d.ReasonCode = "allowed"
	} else {
		x.service.Denials.Add(1)
		x.service.logger.InfoContext(ctx, "authorization denied", "actor", x.principal.SubjectID, "tenant", x.principal.TenantID, "kind", resource.Kind, "action", action, "resource", resource.ID, "revision", d.PolicyRevision, "request_id", RequestID(ctx))
	}
	return d, nil
}
func Require(ctx context.Context, r Resource, action string) error {
	d, err := Check(ctx, r, action)
	if err != nil {
		return err
	}
	if !d.Allowed {
		return ErrForbidden
	}
	return nil
}
func RequireCapability(ctx context.Context, kind, action string) error {
	i, err := CurrentIdentity(ctx)
	if err != nil {
		return err
	}
	p, ok := permission(kind, action)
	if !ok || !p.Collection {
		return ErrInvalid
	}
	return Require(ctx, Resource{Kind: kind, TenantID: i.TenantID}, action)
}
func ReadScope(ctx context.Context, kind, action string) (Scope, error) {
	x, err := operation(ctx)
	if err != nil {
		return Scope{}, err
	}
	if _, ok := permission(kind, action); !ok {
		return Scope{}, ErrInvalid
	}
	scope := Scope{TenantID: x.principal.TenantID, SubjectID: x.principal.SubjectID, Templates: []string{}}
	if kind == "organization" && action == "read" {
		scope.SiteConfiguration, err = CanAdministerSite(ctx, "read")
		if err != nil {
			return Scope{}, err
		}
	}
	rules, err := x.snapshot.enforcer.GetPolicy()
	if err != nil {
		return Scope{}, ErrUnavailable
	}
	for _, r := range rules {
		if len(r) != 5 {
			return Scope{}, ErrUnavailable
		}
		if r[1] == scope.TenantID && r[2] == kind && r[3] == action && slices.Contains(x.principal.RoleIDs, r[0]) {
			if _, ok := conditions[r[4]]; !ok {
				return Scope{}, ErrUnavailable
			}
			scope.Templates = append(scope.Templates, r[4])
		}
	}
	slices.Sort(scope.Templates)
	scope.Templates = slices.Compact(scope.Templates)
	return scope, nil
}

// TeamMembership identifies an explicitly assigned team and its organization tenant.
type TeamMembership struct {
	TenantID string `json:"tenantId"`
	TeamID   string `json:"teamId"`
	Role     string `json:"role"`
}

// Memberships lists the caller's explicit memberships, sorted by tenant and team.
func Memberships(ctx context.Context) ([]TeamMembership, error) {
	x, err := operation(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]TeamMembership, 0, len(x.principal.Memberships))
	for key, role := range x.principal.Memberships {
		tenant, team, ok := strings.Cut(key, "\x00")
		if ok {
			out = append(out, TeamMembership{TenantID: tenant, TeamID: team, Role: role})
		}
	}
	slices.SortFunc(out, func(a, b TeamMembership) int {
		if order := strings.Compare(a.TenantID, b.TenantID); order != 0 {
			return order
		}
		return strings.Compare(a.TeamID, b.TeamID)
	})
	return out, nil
}

// MemberOf reports whether the caller belongs to every listed team in the
// specified organization tenant. An empty tenant or team list never matches.
func MemberOf(ctx context.Context, tenant string, teams ...string) bool {
	x, err := operation(ctx)
	if err != nil || tenant == "" || len(teams) == 0 {
		return false
	}
	for _, team := range teams {
		if _, ok := x.principal.Memberships[membershipKey(tenant, team)]; !ok {
			return false
		}
	}
	return true
}

// LeadsAnyTeam reports whether the caller leads at least one team.
func LeadsAnyTeam(ctx context.Context) bool {
	x, err := operation(ctx)
	if err != nil {
		return false
	}
	for _, role := range x.principal.Memberships {
		if role == LeadRole {
			return true
		}
	}
	return false
}

func ResourcePermissions(ctx context.Context, r Resource, actions ...string) (map[string]bool, Metadata, error) {
	if err := Require(ctx, r, "read"); err != nil {
		if errors.Is(err, ErrForbidden) {
			err = ErrNotFound
		}
		return nil, Metadata{}, err
	}
	x, err := operation(ctx)
	if err != nil {
		return nil, Metadata{}, err
	}
	out := map[string]bool{}
	for _, action := range actions {
		d, err := Check(ctx, r, action)
		if err != nil {
			return nil, Metadata{}, err
		}
		out[action] = d.Allowed
	}
	return out, Metadata{x.snapshot.Revision, r.Version, x.identity.MembershipVersion, x.deadline}, nil
}
