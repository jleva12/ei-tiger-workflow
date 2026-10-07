//go:build integration

package spannerstore

import (
	"context"
	"errors"
	"testing"
	"time"

	"ei-aitiger-codegraph/authorization"
	"ei-aitiger-codegraph/authorization/authtest"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/organization"
)

func TestAuthorizationQueriesAndMutationRaces(t *testing.T) {
	maintenance := context.Background()
	tenant := organization.Entity{ID: "authz-tenant", TenantID: "authz-tenant", Kind: organization.Tenant, Name: "Authorization tenant", Slug: "authz-tenant", Status: organization.Active}
	tenant, err := store.SaveOrganizationEntity(maintenance, tenant, false)
	if err != nil {
		t.Fatal(err)
	}
	platform := organization.Entity{ID: "authz-platform", TenantID: tenant.ID, PlatformID: "authz-platform", ParentID: tenant.ID, Kind: organization.Platform, Name: "Platform", Slug: "platform", Status: organization.Active}
	platform, err = store.SaveOrganizationEntity(maintenance, platform, false)
	if err != nil {
		t.Fatal(err)
	}
	service, provider := authtest.Service(t, tenant.ID)
	ctx, err := service.Pin(maintenance, provider.Identity)
	if err != nil {
		t.Fatal(err)
	}
	mine := organization.Entity{ID: "authz-mine", TenantID: tenant.ID, PlatformID: platform.ID, ParentID: platform.ID, Kind: organization.Workspace, ScopeMode: organization.EntirePlatform, Name: "Mine", Slug: "mine", Status: organization.Draft}
	mine, err = store.SaveOrganizationEntity(ctx, mine, false)
	if err != nil {
		t.Fatal(err)
	}
	if mine.OwnerID != provider.Identity.SubjectID {
		t.Fatal("owner not server assigned")
	}
	other := mine
	other.ID = "authz-other"
	other.Revision = 0
	other.Slug = "other"
	other.OwnerID = "someone-else"
	other, err = store.SaveOrganizationEntity(maintenance, other, false)
	if err != nil {
		t.Fatal(err)
	}
	// A narrow read/update grant must filter in SQL, before paging.
	g := authorization.ExternalGroup{Issuer: "issuer", CorporateTenant: "corp", ExternalID: "editors", LastVerifiedAt: time.Now()}
	g.Key = authorization.GroupKey(g.Issuer, g.CorporateTenant, g.ExternalID)
	snapshot, err := authorization.BuildSnapshot(authorization.Configuration{Revision: 2, Roles: []authorization.Named{{Key: "role:editor"}}, PermissionGroups: []authorization.Named{{Key: "permgroup:editor"}}, ExternalGroups: []authorization.ExternalGroup{g}, Rules: []authorization.Rule{
		{Type: "g", Values: []string{g.Key, "role:editor", tenant.ID}}, {Type: "g", Values: []string{"role:editor", "permgroup:editor", tenant.ID}},
		{Type: "p", Values: []string{"permgroup:editor", tenant.ID, "organization", "read", "owner_draft"}}, {Type: "p", Values: []string{"permgroup:editor", tenant.ID, "organization", "update", "owner_draft"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	narrow, err := authorization.NewService(authtest.Store{Snapshot: snapshot}, authorization.DefaultOptions(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = narrow.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	identity := provider.Identity
	identity.GroupKeys = []string{g.Key}
	limited, err := narrow.Pin(maintenance, identity)
	if err != nil {
		t.Fatal(err)
	}
	page, err := store.ListOrganizationEntities(limited, "", 1, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Items) != 1 || page.Items[0].ID != mine.ID || page.NextCursor != "" {
		t.Fatalf("unauthorized page rows: %+v", page)
	}
	stale := mine
	mine.Name = "Concurrent edit"
	if _, err = store.SaveOrganizationEntity(maintenance, mine, false); err != nil {
		t.Fatal(err)
	}
	stale.Name = "stale mutation"
	if _, err = store.SaveOrganizationEntity(limited, stale, false); !errors.Is(err, organization.ErrConflict) {
		t.Fatal("stale authorized mutation committed", err)
	}
	if _, err = store.SaveOrganizationEntity(limited, other, false); !errors.Is(err, authorization.ErrNotFound) {
		t.Fatal("nonowner mutation", err)
	}
	// Repository visibility and counts share the same SQL tenant predicate.
	repo, err := store.PutRepository(ctx, deployment.Repository{SchemaVersion: deployment.SchemaVersion, RepositoryID: "authz-owned-repo", GitHubURL: "https://github.com/acme/authz-owned-repo", IntegrationID: "test"})
	if err != nil {
		t.Fatal(err)
	}
	if repo.TenantID != tenant.ID {
		t.Fatal("repository tenant not assigned")
	}
	_, err = store.PutRepository(maintenance, deployment.Repository{TenantID: "different-tenant", SchemaVersion: deployment.SchemaVersion, RepositoryID: "authz-other-repo", GitHubURL: "https://github.com/acme/authz-other-repo", IntegrationID: "test"})
	if err != nil {
		t.Fatal(err)
	}
	repos, err := store.ListRepositories(ctx, 1, "")
	if err != nil || len(repos.Items) != 1 || repos.Items[0].Repository.RepositoryID != repo.RepositoryID || repos.NextCursor != "" {
		t.Fatalf("scoped repositories: %+v %v", repos, err)
	}
	if _, err = store.GetRepository(ctx, "authz-other-repo"); !errors.Is(err, authorization.ErrNotFound) {
		t.Fatal("cross-tenant repository", err)
	}
}

func TestSiteConfigurationAcrossTenants(t *testing.T) {
	service, provider := authtest.Service(t, "site-home")
	provider.Identity.SiteAdministrator = true
	ctx, err := service.Pin(context.Background(), provider.Identity)
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"site-home", "site-second"} {
		tenant := organization.Entity{ID: id, TenantID: id, Kind: organization.Tenant, Name: id, Slug: id, Status: organization.Active}
		if _, err := store.SaveOrganizationEntity(ctx, tenant, false); err != nil {
			t.Fatal("site tenant creation", err)
		}
		platform := organization.Entity{ID: id + "-platform", TenantID: id, PlatformID: id + "-platform", ParentID: id, Kind: organization.Platform, Name: "Platform", Slug: "platform", Status: organization.Active}
		if _, err := store.SaveOrganizationEntity(ctx, platform, false); err != nil {
			t.Fatal("site platform creation", err)
		}
	}
	seen := map[string]bool{}
	cursor := ""
	for {
		page, err := store.ListOrganizationEntities(ctx, "", 1, cursor)
		if err != nil {
			t.Fatal(err)
		}
		for _, item := range page.Items {
			seen[item.ID] = true
		}
		cursor = page.NextCursor
		if cursor == "" {
			break
		}
	}
	for _, id := range []string{"site-home", "site-second", "site-second-platform"} {
		if !seen[id] {
			t.Fatal("cross-tenant pagination omitted", id)
		}
	}
	provider.Identity.SiteAdministrator = false
	limited, err := service.Pin(context.Background(), provider.Identity)
	if err != nil {
		t.Fatal(err)
	}
	page, err := store.ListOrganizationEntities(limited, "", 100, "")
	if err != nil || len(page.Items) != 2 {
		t.Fatal("normal tenant list", page, err)
	}
	for _, item := range page.Items {
		if item.TenantID != "site-home" {
			t.Fatal("cross-tenant list leak", item)
		}
	}
	if _, err := store.SaveOrganizationEntity(limited, organization.Entity{ID: "site-third", TenantID: "site-third", Kind: organization.Tenant, Name: "Third", Slug: "site-third", Status: organization.Active}, false); !errors.Is(err, authorization.ErrNotFound) {
		t.Fatal("ordinary tenant created another tenant", err)
	}
	foreign := deployment.Repository{SchemaVersion: deployment.SchemaVersion, RepositoryID: "site-foreign-repo", TenantID: "site-second", GitHubURL: "https://github.com/acme/site-foreign", IntegrationID: "test"}
	if _, err := store.PutRepository(context.Background(), foreign); err != nil {
		t.Fatal(err)
	}
	if _, err := store.GetRepository(ctx, foreign.RepositoryID); !errors.Is(err, authorization.ErrNotFound) {
		t.Fatal("site permission exposed graph repository", err)
	}
}
