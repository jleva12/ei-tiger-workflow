package authorization

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

type memoryStore struct {
	mu       sync.Mutex
	snapshot *Snapshot
	revision uint64
	fail     bool
}

func (s *memoryStore) Revision(context.Context) (uint64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fail {
		return 0, ErrUnavailable
	}
	return s.revision, nil
}
func (s *memoryStore) Load(context.Context) (*Snapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fail || s.snapshot.Revision != s.revision {
		return nil, ErrUnavailable
	}
	return s.snapshot, nil
}
func fixture(t testing.TB, template string) (*Service, *memoryStore, Identity) {
	t.Helper()
	g := ExternalGroup{Issuer: "issuer", CorporateTenant: "corp", ExternalID: "ops", LastVerifiedAt: time.Now()}
	g.Key = GroupKey(g.Issuer, g.CorporateTenant, g.ExternalID)
	c := Configuration{Revision: 1, Roles: []Named{{Key: "role:editor"}}, PermissionGroups: []Named{{Key: "permgroup:editor"}}, ExternalGroups: []ExternalGroup{g}, Rules: []Rule{{"g", []string{g.Key, "role:editor", "tenant-a"}}, {"g", []string{"role:editor", "permgroup:editor", "tenant-a"}}, {"p", []string{"permgroup:editor", "tenant-a", "organization", "read", template}}, {"p", []string{"permgroup:editor", "tenant-a", "organization", "update", template}}, {"p", []string{"permgroup:editor", "tenant-a", "organization_collection", "list", "tenant"}}}}
	snapshot, err := BuildSnapshot(c)
	if err != nil {
		t.Fatal(err)
	}
	store := &memoryStore{snapshot: snapshot, revision: 1}
	service, err := NewService(store, DefaultOptions(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = service.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	i := Identity{SubjectID: "user:alice", Issuer: g.Issuer, TenantID: "tenant-a", GroupKeys: []string{g.Key}, MembershipVersion: "1", MembershipComplete: true, MembershipValidTo: time.Now().Add(time.Minute), AuthenticatedUntil: time.Now().Add(time.Minute)}
	return service, store, i
}
func TestConditionsAndTenantIsolation(t *testing.T) {
	for _, template := range []string{"tenant", "owner", "owner_draft", "tenant_draft"} {
		t.Run(template, func(t *testing.T) {
			service, _, i := fixture(t, template)
			ctx, err := service.Pin(context.Background(), i)
			if err != nil {
				t.Fatal(err)
			}
			for _, tenant := range []string{"tenant-a", "tenant-b", ""} {
				for _, owner := range []string{"user:alice", "user:bob", ""} {
					for _, status := range []string{"draft", "active", ""} {
						r := Resource{Kind: "organization", TenantID: tenant, OwnerID: owner, Status: status}
						d, err := Check(ctx, r, "update")
						if err != nil {
							t.Fatal(err)
						}
						want := matches(template, Principal{SubjectID: i.SubjectID, TenantID: i.TenantID}, r)
						if d.Allowed != want {
							t.Fatalf("%+v: %v want %v", r, d, want)
						}
						scope, err := ReadScope(ctx, "organization", "update")
						if err != nil || scope.Allows(r) != want {
							t.Fatalf("scope differs: %+v %v", scope, err)
						}
					}
				}
			}
			i.TenantID = "tenant-b"
			other, err := service.Pin(context.Background(), i)
			if err != nil {
				t.Fatal(err)
			}
			d, err := Check(other, Resource{Kind: "organization", TenantID: "tenant-b", OwnerID: i.SubjectID, Status: "draft"}, "update")
			if err != nil || d.Allowed {
				t.Fatalf("cross-tenant roles: %+v %v", d, err)
			}
		})
	}
}
func TestMembershipAndMissingIdentity(t *testing.T) {
	service, _, i := fixture(t, "tenant")
	if _, err := Check(context.Background(), Resource{}, "read"); !errors.Is(err, ErrUnauthenticated) {
		t.Fatal(err)
	}
	for _, change := range []func(*Identity){func(i *Identity) { i.MembershipComplete = false }, func(i *Identity) { i.MembershipValidTo = time.Now().Add(-time.Second) }, func(i *Identity) { i.MembershipVersion = "" }} {
		bad := i
		change(&bad)
		if _, err := service.Pin(context.Background(), bad); !errors.Is(err, ErrUnavailable) {
			t.Fatal(err)
		}
	}
	i.GroupKeys = []string{}
	ctx, err := service.Pin(context.Background(), i)
	if err != nil {
		t.Fatal(err)
	}
	d, err := Check(ctx, Resource{Kind: "organization", TenantID: i.TenantID}, "read")
	if err != nil || d.Allowed {
		t.Fatal(d, err)
	}
	if GroupKey("a", "b", "c") == GroupKey("a", "bc", "") || GroupKey("a", "b", "c") == GroupKey("other", "b", "c") {
		t.Fatal("group identity collision")
	}
}
func TestDecoratorsFailClosed(t *testing.T) {
	service, _, i := fixture(t, "owner_draft")
	ctx, _ := service.Pin(context.Background(), i)
	for _, test := range []struct {
		name   string
		wrap   func(http.HandlerFunc) http.HandlerFunc
		ctx    context.Context
		status int
	}{
		{"unauthenticated", service.RequireRole("role:editor"), context.Background(), 401},
		{"role", service.RequireRole("role:editor"), ctx, 204},
		{"permission-group", service.RequireRole("permgroup:editor"), ctx, 204},
		{"any", service.RequireAnyRole("role:missing", "role:editor"), ctx, 204},
		{"all", service.RequireAllRoles("role:missing", "role:editor"), ctx, 403},
		{"capability", service.RequirePermission("organization_collection", "list"), ctx, 204},
		{"hidden", service.RequireResource("organization", "update", func(context.Context, *http.Request) (Resource, error) {
			return Resource{Kind: "organization", TenantID: i.TenantID, OwnerID: "someone-else", Status: "draft"}, nil
		}), ctx, 404},
		{"resolver-error", service.RequireResource("organization", "read", func(context.Context, *http.Request) (Resource, error) { return Resource{}, ErrUnavailable }), ctx, 503},
	} {
		t.Run(test.name, func(t *testing.T) {
			called := false
			handler := test.wrap(func(w http.ResponseWriter, r *http.Request) { called = true; w.WriteHeader(204) })
			w := httptest.NewRecorder()
			handler(w, httptest.NewRequest("GET", "/", nil).WithContext(test.ctx))
			if w.Code != test.status || called != (test.status == 204) {
				t.Fatalf("status=%d called=%v", w.Code, called)
			}
		})
	}
	for _, build := range []func(){func() { service.RequireAnyRole() }, func() { service.RequireAllRoles() }, func() { service.RequirePermission("organization", "update") }, func() { service.RequirePermission("unknown", "read") }} {
		func() {
			defer func() {
				if recover() == nil {
					t.Error("invalid registration did not panic")
				}
			}()
			build()
		}()
	}
}
func TestFreshnessAndPinnedRevision(t *testing.T) {
	service, store, i := fixture(t, "tenant")
	ctx, _ := service.Pin(context.Background(), i)
	store.mu.Lock()
	store.fail = true
	store.mu.Unlock()
	if service.Refresh(context.Background()) == nil {
		t.Fatal("outage not reported")
	}
	if _, err := Check(ctx, Resource{Kind: "organization", TenantID: i.TenantID}, "read"); err != nil {
		t.Fatal("healthy bounded snapshot should work", err)
	}
	service.ObserveRevision(2)
	if _, err := Check(ctx, Resource{Kind: "organization", TenantID: i.TenantID}, "read"); !errors.Is(err, ErrUnavailable) {
		t.Fatal("known stale snapshot allowed", err)
	}
	service, _, i = fixture(t, "tenant")
	old := *service.current.Load()
	old.ConfirmedAt = time.Now().Add(-time.Minute)
	service.current.Store(&old)
	if _, err := service.Pin(context.Background(), i); !errors.Is(err, ErrUnavailable) {
		t.Fatal("expired snapshot", err)
	}
}
func TestConcurrentReadersAndRefresh(t *testing.T) {
	service, _, i := fixture(t, "tenant")
	var wg sync.WaitGroup
	for n := 0; n < 20; n++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 30; j++ {
				if err := service.Refresh(context.Background()); err != nil {
					t.Error(err)
				}
				ctx, err := service.Pin(context.Background(), i)
				if err != nil {
					t.Error(err)
					return
				}
				d, err := Check(ctx, Resource{Kind: "organization", TenantID: i.TenantID}, "read")
				if err != nil || !d.Allowed {
					t.Error(d, err)
				}
			}
		}()
	}
	wg.Wait()
}
func TestGrantUnionAndUnknownConfiguration(t *testing.T) {
	service, store, i := fixture(t, "owner_draft")
	// Build a new unpublished fixture; never mutate an instance already in use.
	rules, _ := store.snapshot.enforcer.GetPolicy()
	roles, _ := store.snapshot.enforcer.GetGroupingPolicy()
	e, _ := enforcer(nil)
	for _, r := range rules {
		_, _ = e.AddPolicy(r)
	}
	for _, r := range roles {
		_, _ = e.AddGroupingPolicy(r)
	}
	_, _ = e.AddPolicy("permgroup:editor", "tenant-a", "organization", "update", "tenant")
	store.snapshot = &Snapshot{enforcer: e, Revision: 2, ModelVersion: ModelVersion, ConfirmedAt: time.Now()}
	store.revision = 2
	if err := service.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, _ := service.Pin(context.Background(), i)
	d, err := Check(ctx, Resource{Kind: "organization", TenantID: i.TenantID, OwnerID: "other", Status: "active"}, "update")
	if err != nil || !d.Allowed {
		t.Fatal(d, err)
	}
	if _, err = Check(ctx, Resource{Kind: "organization", TenantID: i.TenantID}, "unknown"); !errors.Is(err, ErrInvalid) {
		t.Fatal(err)
	}
	if matches("unregistered", Principal{SubjectID: i.SubjectID, TenantID: i.TenantID}, Resource{TenantID: i.TenantID}) {
		t.Fatal("unknown condition")
	}
}

func BenchmarkLocalDecision(b *testing.B) {
	service, _, identity := fixture(b, "owner_draft")
	ctx, err := service.Pin(context.Background(), identity)
	if err != nil {
		b.Fatal(err)
	}
	resource := Resource{Kind: "organization", TenantID: identity.TenantID, OwnerID: identity.SubjectID, Status: "draft"}
	b.ResetTimer()
	for n := 0; n < b.N; n++ {
		decision, err := Check(ctx, resource, "update")
		if err != nil || !decision.Allowed {
			b.Fatal(decision, err)
		}
	}
}
