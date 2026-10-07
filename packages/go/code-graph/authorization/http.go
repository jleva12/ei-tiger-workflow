package authorization

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"slices"
	"strings"
	"sync"
)

type requestIDKey struct{}

func RequestID(ctx context.Context) string { id, _ := ctx.Value(requestIDKey{}).(string); return id }
func ErrorStatus(err error) int {
	switch {
	case errors.Is(err, ErrUnauthenticated):
		return 401
	case errors.Is(err, ErrForbidden):
		return 403
	case errors.Is(err, ErrNotFound):
		return 404
	case errors.Is(err, ErrConflict):
		return 409
	case errors.Is(err, ErrInvalid):
		return 400
	default:
		return 503
	}
}
func WriteError(w http.ResponseWriter, r *http.Request, err error) {
	status := ErrorStatus(err)
	code := map[int]string{400: "invalid_request", 401: "unauthenticated", 403: "forbidden", 404: "not_found", 409: "revision_conflict", 503: "authorization_unavailable"}[status]
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "private, no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": http.StatusText(status), "code": code, "requestId": RequestID(r.Context())})
}
func JSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "private, no-store")
	_ = json.NewEncoder(w).Encode(v)
}

// Middleware authenticates before pinning. nil integrations always fail closed.
func (s *Service) Middleware(provider Provider, next http.Handler) http.Handler {
	if provider == nil {
		provider = UnconfiguredProvider{}
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := make([]byte, 16)
		_, _ = rand.Read(id)
		ctx := context.WithValue(r.Context(), requestIDKey{}, hex.EncodeToString(id))
		r = r.WithContext(ctx)
		w.Header().Set("X-Request-ID", RequestID(ctx))
		w.Header().Set("Cache-Control", "private, no-store")
		identity, err := provider.Authenticate(ctx, r)
		if err != nil {
			if s != nil && !errors.Is(err, ErrUnauthenticated) {
				s.MembershipFailures.Add(1)
			}
			WriteError(w, r, err)
			return
		}
		if s == nil {
			WriteError(w, r, ErrUnavailable)
			return
		}
		ctx, cancel := context.WithTimeout(ctx, s.options.RequestTimeout)
		defer cancel()
		ctx, err = s.Pin(ctx, identity)
		if err != nil {
			WriteError(w, r, err)
			return
		}
		x, err := operation(ctx)
		if err != nil {
			WriteError(w, r, err)
			return
		}
		w.Header().Set("X-Policy-Revision", jsonNumber(x.snapshot.Revision))
		s.recordSignIn(identity)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}
func jsonNumber(n uint64) string { b, _ := json.Marshal(n); return string(b) }
func (s *Service) RequirePermission(kind, action string) func(http.HandlerFunc) http.HandlerFunc {
	p, ok := permission(kind, action)
	if !ok || !p.Collection {
		panic("authorization: unknown or instance-only capability")
	}
	return func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if err := RequireCapability(r.Context(), kind, action); err != nil {
				WriteError(w, r, err)
				return
			}
			next(w, r)
		}
	}
}
func (s *Service) RequireRole(role string) func(http.HandlerFunc) http.HandlerFunc {
	return s.RequireAllRoles(role)
}
func (s *Service) RequireAnyRole(roles ...string) func(http.HandlerFunc) http.HandlerFunc {
	return rolesDecorator(false, roles)
}
func (s *Service) RequireAllRoles(roles ...string) func(http.HandlerFunc) http.HandlerFunc {
	return rolesDecorator(true, roles)
}
func rolesDecorator(all bool, roles []string) func(http.HandlerFunc) http.HandlerFunc {
	if len(roles) == 0 {
		panic("authorization: empty role check")
	}
	roles = append([]string(nil), roles...)
	for _, r := range roles {
		if !validKey(r, "role:") && !validKey(r, "permgroup:") {
			panic("authorization: invalid role key")
		}
	}
	return func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			x, err := operation(r.Context())
			if err != nil {
				WriteError(w, r, err)
				return
			}
			allowed := all
			for _, role := range roles {
				has := slices.Contains(x.principal.RoleIDs, role)
				if all {
					allowed = allowed && has
				} else {
					allowed = allowed || has
				}
			}
			if !allowed {
				WriteError(w, r, ErrForbidden)
				return
			}
			next(w, r)
		}
	}
}

type ResourceResolver func(context.Context, *http.Request) (Resource, error)
type resourceKey struct{}

func ResolvedResource(ctx context.Context) (Resource, bool) {
	r, ok := ctx.Value(resourceKey{}).(Resource)
	return r, ok
}
func (s *Service) RequireResource(kind, action string, resolve ResourceResolver) func(http.HandlerFunc) http.HandlerFunc {
	p, ok := permission(kind, action)
	if !ok || p.Collection || resolve == nil {
		panic("authorization: invalid resource registration")
	}
	return func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			obj, err := resolve(r.Context(), r)
			if err == nil && obj.Kind != kind {
				err = ErrUnavailable
			}
			if err == nil {
				err = Require(r.Context(), obj, "read")
			}
			if errors.Is(err, ErrForbidden) {
				err = ErrNotFound
			}
			if err == nil && action != "read" {
				err = Require(r.Context(), obj, action)
			}
			if err != nil {
				WriteError(w, r, err)
				return
			}
			next(w, r.WithContext(context.WithValue(r.Context(), resourceKey{}, obj)))
		}
	}
}

type Session struct {
	SubjectID    string           `json:"subjectId"`
	TenantID     string           `json:"tenantId"`
	Issuer       string           `json:"issuer"`
	DisplayName  string           `json:"displayName"`
	Email        string           `json:"email"`
	Roles        []string         `json:"roles"`
	Capabilities map[string]bool  `json:"capabilities"`
	Teams        []TeamMembership `json:"teams"`
	Metadata
}

func (s *Service) Session(w http.ResponseWriter, r *http.Request) {
	x, err := operation(r.Context())
	if err != nil {
		WriteError(w, r, err)
		return
	}
	teams, err := Memberships(r.Context())
	if err != nil {
		WriteError(w, r, err)
		return
	}
	out := Session{SubjectID: x.identity.SubjectID, TenantID: x.identity.TenantID, Issuer: x.identity.Issuer, DisplayName: x.identity.DisplayName, Email: x.identity.Email, Roles: []string{}, Capabilities: map[string]bool{}, Teams: teams, Metadata: Metadata{PolicyRevision: x.snapshot.Revision, MembershipVersion: x.identity.MembershipVersion, ValidUntil: x.deadline}}
	for _, role := range x.principal.RoleIDs {
		if strings.HasPrefix(role, "role:") {
			out.Roles = append(out.Roles, role)
		}
	}
	for _, p := range catalog {
		if !p.Collection {
			continue
		}
		d, err := Check(r.Context(), Resource{Kind: p.Kind, TenantID: x.identity.TenantID}, p.Action)
		if err != nil {
			WriteError(w, r, err)
			return
		}
		out.Capabilities[p.Kind+":"+p.Action] = d.Allowed
	}
	JSON(w, out)
}

type RoutePolicy struct{ Method, Path, Kind, Action, Classification string }

// Registry is an inspectable, deny-by-construction route inventory. Unknown
// classifications and permissions panic at registration rather than serving.
type Registry struct {
	mu     sync.Mutex
	routes []RoutePolicy
}

func (r *Registry) Add(p RoutePolicy) {
	if p.Method == "" || p.Path == "" {
		panic("authorization: incomplete route metadata")
	}
	switch p.Classification {
	case "public":
	case "session":
	case "capability", "resource":
		permission, ok := permission(p.Kind, p.Action)
		if !ok || permission.Collection != (p.Classification == "capability") {
			panic("authorization: invalid route policy")
		}
	default:
		panic("authorization: unclassified route")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.routes = append(r.routes, p)
}
func (r *Registry) Routes() []RoutePolicy {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]RoutePolicy(nil), r.routes...)
}
