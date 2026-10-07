package authorization

import (
	"context"
	"errors"
	"fmt"
	"net/http/httptest"
	"sync"
	"testing"

	"gorm.io/gorm"
)

func TestLocalDevelopmentIdentityAndDirectoryBoundary(t *testing.T) {
	provider := LocalDevelopmentProvider{TenantID: "tenant-a"}
	for _, authority := range []string{"localhost:18030", "127.0.0.1:18091", "[::1]:18091", "mcp:8765"} {
		request := httptest.NewRequest("GET", "http://"+authority+"/v1/me/authorization", nil)
		request.Header.Set("X-Tenant-ID", "tenant-b")
		request.Header.Set("X-User-ID", "another-user")
		identity, err := provider.Authenticate(context.Background(), request)
		if err != nil || identity.TenantID != "tenant-a" || identity.SubjectID != SubjectKey(LocalDevelopmentIssuer, "local-admin") || !identity.MembershipComplete {
			t.Fatal(identity, err)
		}
	}
	for _, authority := range []string{"example.com", "localhost.evil.test", "192.168.1.20", ""} {
		request := httptest.NewRequest("GET", "/", nil)
		request.Host = authority
		if _, err := provider.Authenticate(context.Background(), request); !errors.Is(err, ErrUnauthenticated) {
			t.Fatal("external host accepted", authority, err)
		}
	}
	if _, err := (LocalDevelopmentProvider{}).Identity(); err == nil {
		t.Fatal("missing tenant accepted")
	}
	group := LocalDevelopmentGroup()
	if _, err := provider.VerifyExternalGroup(context.Background(), "tenant-a", group); err != nil {
		t.Fatal(err)
	}
	if _, err := provider.VerifyExternalGroup(context.Background(), "tenant-b", group); err == nil {
		t.Fatal("cross-tenant group verified")
	}
	group.Issuer = "https://corporate.example"
	if _, err := provider.VerifyExternalGroup(context.Background(), "tenant-a", group); err == nil {
		t.Fatal("real directory identity fabricated")
	}
}

func TestMySQLLocalAdministratorSeed(t *testing.T) {
	store, _ := mysqlStore(t)
	ctx := context.Background()
	provider := LocalDevelopmentProvider{TenantID: "tenant-a"}
	if err := store.db.Exec("CREATE TRIGGER reject_local_seed BEFORE INSERT ON authz_outbox FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='test rollback'").Error; err != nil {
		t.Fatal(err)
	}
	if _, err := store.SeedLocalAdministrator(ctx, provider); err == nil {
		t.Fatal("expected atomic rollback")
	}
	state, err := store.Configuration(ctx, "tenant-a")
	if err != nil || len(state.Roles) != 0 || len(state.Rules) != 0 || state.Revision != 1 {
		t.Fatal("partial local seed", state, err)
	}
	if err := store.db.Exec("DROP TRIGGER reject_local_seed").Error; err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for range 2 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if revision, err := store.SeedLocalAdministrator(ctx, provider); err != nil || revision != 2 {
				t.Error("concurrent seed", revision, err)
			}
		}()
	}
	wg.Wait()
	service, err := NewService(store, DefaultOptions(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = service.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	identity, _ := provider.Identity()
	execution, err := service.Pin(ctx, identity)
	if err != nil {
		t.Fatal(err)
	}
	for _, permission := range Catalog() {
		resource := Resource{Kind: permission.Kind, TenantID: identity.TenantID, OwnerID: "someone-else", Status: "active"}
		if err := Require(execution, resource, permission.Action); err != nil {
			t.Fatalf("catalog permission missing: %+v: %v", permission, err)
		}
		resource.TenantID = "tenant-b"
		if err := Require(execution, resource, permission.Action); siteConfigurationAction(permission.Kind, permission.Action) != "" {
			if err != nil {
				t.Fatal("site configuration unavailable", permission, err)
			}
		} else if !errors.Is(err, ErrForbidden) {
			t.Fatal("cross-tenant data bypass", permission, err)
		}
	}
	grants := []Grant{}
	for _, permission := range Catalog() {
		if permission.Kind != "assistant" && permission.Kind != "site_configuration" {
			grants = append(grants, Grant{permission.Kind, permission.Action, "tenant"})
		}
	}
	committed, err := store.Apply(execution, provider, Command{Operation: "group.grants", Key: localSiteAdminPermissions, ExpectedRevision: 2, Grants: grants})
	if err != nil {
		t.Fatal(err)
	}
	if repeated, err := store.SeedLocalAdministrator(ctx, provider); err != nil || repeated != committed {
		t.Fatal("startup rewrote policies", repeated, err)
	}
	if err := service.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	execution, err = service.Pin(ctx, identity)
	if err != nil {
		t.Fatal(err)
	}
	if err := RequireCapability(execution, "assistant", "use"); !errors.Is(err, ErrForbidden) {
		t.Fatal("seed resurrected removed permission", err)
	}
	if allowed, err := CanAdministerSite(execution, "read"); err != nil || allowed {
		t.Fatal("seed resurrected removed site permission", allowed, err)
	}
	for _, url := range []string{"http://mcp:8765/mcp", "http://127.0.0.1:18765/mcp"} {
		if err := provider.AuthorizeOutbound(execution, httptest.NewRequest("POST", url, nil)); err != nil {
			t.Fatal(err)
		}
	}
	if err := provider.AuthorizeOutbound(execution, httptest.NewRequest("POST", "http://external.example/mcp", nil)); err == nil {
		t.Fatal("external delegation accepted")
	}
}

func TestMySQLLocalSiteConfigurationUpgrade(t *testing.T) {
	for _, retainOrganizationGrants := range []bool{false, true} {
		t.Run(fmt.Sprint(retainOrganizationGrants), func(t *testing.T) {
			store, _ := mysqlStore(t)
			ctx := context.Background()
			provider := LocalDevelopmentProvider{TenantID: "tenant-a"}
			identity, _ := provider.Identity()
			if _, err := store.SeedLocalAdministrator(ctx, provider); err != nil {
				t.Fatal(err)
			}
			// Reconstruct the pre-upgrade local fixture with any earlier revocations.
			grants := []Grant{}
			for _, permission := range Catalog() {
				if permission.Kind == "site_configuration" || permission.Action == "enter" || (!retainOrganizationGrants && (permission.Kind == "organization" || permission.Kind == "organization_collection")) {
					continue
				}
				grants = append(grants, Grant{permission.Kind, permission.Action, "tenant"})
			}
			if err := store.db.Transaction(func(tx *gorm.DB) error {
				if err := applyCommand(tx, identity.TenantID, Command{Operation: "group.grants", Key: localSiteAdminPermissions, Grants: grants}); err != nil {
					return err
				}
				return tx.Exec("DELETE FROM authz_requests WHERE request_key IN (?,?)", "local-site-configuration-v2", "local-site-configuration-v3").Error
			}); err != nil {
				t.Fatal(err)
			}
			before, err := store.Configuration(ctx, identity.TenantID)
			if err != nil {
				t.Fatal(err)
			}
			revision, err := store.SeedLocalAdministrator(ctx, provider)
			if err != nil {
				t.Fatal(err)
			}
			wantRevision := before.Revision
			if retainOrganizationGrants {
				wantRevision++
			}
			if revision != wantRevision {
				t.Fatal("unexpected migration revision", revision, wantRevision)
			}
			after, err := store.Configuration(ctx, identity.TenantID)
			if err != nil {
				t.Fatal(err)
			}
			// Site read/write, organization enter and the team workspace capability
			// are added; membership and directory grants were already present.
			wantRules := len(before.Rules)
			if retainOrganizationGrants {
				wantRules += 4
			}
			if len(after.Rules) != wantRules || len(after.Roles) != len(before.Roles) || len(after.ExternalGroups) != len(before.ExternalGroups) {
				t.Fatal("migration changed prior configuration", before, after)
			}
			if repeated, err := store.SeedLocalAdministrator(ctx, provider); err != nil || repeated != revision {
				t.Fatal("migration was not idempotent", repeated, err)
			}
		})
	}
}
