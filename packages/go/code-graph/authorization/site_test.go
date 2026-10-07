package authorization

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestSiteConfigurationRequiresVerifiedIdentityAndExplicitGrant(t *testing.T) {
	for _, verified := range []bool{false, true} {
		for _, granted := range []bool{false, true} {
			service, store, identity := fixture(t, "tenant")
			// Build a new immutable snapshot rather than changing a published one.
			e, err := enforcer(nil)
			if err != nil {
				t.Fatal(err)
			}
			rules, _ := store.snapshot.enforcer.GetPolicy()
			groups, _ := store.snapshot.enforcer.GetGroupingPolicy()
			if _, err = e.AddPolicies(rules); err != nil {
				t.Fatal(err)
			}
			if _, err = e.AddGroupingPolicies(groups); err != nil {
				t.Fatal(err)
			}
			if granted {
				for _, action := range []string{"read", "write"} {
					if _, err = e.AddPolicy("permgroup:editor", "tenant-a", "site_configuration", action, "tenant"); err != nil {
						t.Fatal(err)
					}
				}
			}
			store.snapshot = &Snapshot{enforcer: e, Revision: 2, ModelVersion: ModelVersion, ConfirmedAt: time.Now()}
			store.revision = 2
			if err = service.Refresh(context.Background()); err != nil {
				t.Fatal(err)
			}
			identity.SiteAdministrator = verified
			ctx, err := service.Pin(context.Background(), identity)
			if err != nil {
				t.Fatal(err)
			}
			for _, action := range []string{"read", "update", "archive", "transition"} {
				d, err := Check(ctx, Resource{Kind: "organization", TenantID: "tenant-b"}, action)
				if err != nil || d.Allowed != (verified && granted) {
					t.Fatalf("verified=%v granted=%v action=%s: %+v %v", verified, granted, action, d, err)
				}
			}
			for _, target := range []struct{ kind, action string }{{"repository", "read"}, {"assistant", "use"}, {"authorization_config", "write"}, {"organization", "enter"}} {
				d, err := Check(ctx, Resource{Kind: target.kind, TenantID: "tenant-b"}, target.action)
				if err != nil || d.Allowed {
					t.Fatalf("site config escaped to %s: %+v %v", target.kind, d, err)
				}
			}
			scope, err := ReadScope(ctx, "organization", "read")
			if err != nil || scope.Allows(Resource{Kind: "organization", TenantID: "tenant-b"}) != (verified && granted) {
				t.Fatal("list scope differs from object decisions", scope, err)
			}
			if scope.Allows(Resource{Kind: "repository", TenantID: "tenant-b"}) {
				t.Fatal("scope leaked repository")
			}
			service.ObserveRevision(3)
			if _, err = CanAdministerSite(ctx, "read"); !errors.Is(err, ErrUnavailable) {
				t.Fatal("stale site authority accepted", err)
			}
		}
	}
}
