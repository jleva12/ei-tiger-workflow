//go:build integration

package spannerstore

import (
	"context"
	"errors"
	"testing"

	"ei-aitiger-codegraph/pkg/organization"
)

func TestOrganizationPersistenceAndBoundaries(t *testing.T) {
	ctx := context.Background()
	save := func(e organization.Entity) organization.Entity {
		t.Helper()
		got, err := store.SaveOrganizationEntity(ctx, e, false)
		if err != nil {
			t.Fatal(err)
		}
		return got
	}
	tenant := save(organization.Entity{ID: "org-test-tenant", TenantID: "org-test-tenant", Kind: organization.Tenant, Name: "USP", Slug: "org-test-usp", Status: organization.Active})
	platform := save(organization.Entity{ID: "org-test-platform", TenantID: tenant.ID, PlatformID: "org-test-platform", ParentID: tenant.ID, Kind: organization.Platform, Name: "Cirrus", Slug: "cirrus", Status: organization.Active})
	team := save(organization.Entity{ID: "org-test-team", TenantID: tenant.ID, PlatformID: platform.ID, ParentID: platform.ID, Kind: organization.Team, Name: "Core", Slug: "core", Status: organization.Active})
	registered := register(t, "organization-repo")
	repo := save(organization.Entity{ID: "org-test-repo", TenantID: tenant.ID, PlatformID: platform.ID, ParentID: platform.ID, Kind: organization.Repository, Name: "Code", Slug: "code", Status: organization.Active, GraphRepositoryID: registered.RepositoryID, RemoteURL: registered.GitHubURL})
	assignment := save(organization.Entity{ID: "org-test-assignment", TenantID: tenant.ID, PlatformID: platform.ID, ParentID: team.ID, Kind: organization.Responsibility, Name: "Services", Slug: "services", Status: organization.Active, RepositoryID: repo.ID, ScopeKind: organization.Paths, Paths: []string{"services/"}})
	workspace := save(organization.Entity{ID: "org-test-workspace", TenantID: tenant.ID, PlatformID: platform.ID, ParentID: platform.ID, Kind: organization.Workspace, Name: "Development", Slug: "development", Status: organization.Active, ScopeMode: organization.SelectedTeams, TeamIDs: []string{team.ID}})
	workspace.Documents = []organization.WorkspaceDocument{{ID: "feature-plan", Kind: "feature", Title: "Repository filters", Body: "Filter search results by repository.", Status: "in_review"}}
	workspace = save(workspace)
	snapshot, err := store.OrganizationTenant(ctx, tenant.ID)
	if err != nil || len(snapshot) != 6 {
		t.Fatalf("persisted snapshot %d: %v", len(snapshot), err)
	}
	loaded, _ := organization.Find(snapshot, workspace.ID)
	if len(loaded.Documents) != 1 || loaded.Documents[0].Body != workspace.Documents[0].Body || loaded.Documents[0].UpdatedAt.IsZero() {
		t.Fatalf("workspace documents were not persisted: %+v", loaded)
	}
	resolved, err := organization.Resolve(snapshot, organization.ScopeRequest{TenantID: tenant.ID, PlatformID: platform.ID, Mode: workspace.ScopeMode, TeamIDs: workspace.TeamIDs})
	if err != nil || len(resolved.Repositories) != 1 || resolved.Repositories[0].Paths[0] != "services/" {
		t.Fatalf("persisted scope: %+v %v", resolved, err)
	}
	seen := map[string]bool{}
	cursor := ""
	for {
		page, err := store.ListOrganizationEntities(ctx, tenant.ID, 2, cursor)
		if err != nil {
			t.Fatal(err)
		}
		for _, e := range page.Items {
			if seen[e.ID] {
				t.Fatal("duplicate cursor item")
			}
			seen[e.ID] = true
		}
		cursor = page.NextCursor
		if cursor == "" {
			break
		}
	}
	if len(seen) != 6 {
		t.Fatalf("pagination: %d", len(seen))
	}
	page, _ := store.ListOrganizationEntities(ctx, tenant.ID, 1, "")
	if _, err = store.ListOrganizationEntities(ctx, "other-tenant", 1, page.NextCursor); err == nil {
		t.Fatal("cursor accepted across tenants")
	}
	old := team
	team.Name = "Core services"
	updated := save(team)
	if updated.Revision != 2 || !updated.CreatedAt.Equal(old.CreatedAt) {
		t.Fatal("update metadata")
	}
	if _, err = store.SaveOrganizationEntity(ctx, old, false); !errors.Is(err, organization.ErrConflict) {
		t.Fatalf("stale revision: %v", err)
	}
	assignment.Status = organization.Archived
	if _, err = store.SaveOrganizationEntity(ctx, assignment, false); !errors.Is(err, organization.ErrConflict) {
		t.Fatalf("archive live workspace dependency: %v", err)
	}
	workspace.Status = organization.Archived
	save(workspace)
	save(assignment)
	another := tenant
	another.ID = "org-test-other"
	another.TenantID = another.ID
	another.Revision = 0
	if _, err = store.SaveOrganizationEntity(ctx, another, false); !errors.Is(err, organization.ErrConflict) {
		t.Fatalf("global tenant slug: %v", err)
	}
	another.Slug = "org-test-other"
	another = save(another)
	otherPlatform := platform
	otherPlatform.ID = "org-test-other-platform"
	otherPlatform.PlatformID = otherPlatform.ID
	otherPlatform.ParentID = another.ID
	otherPlatform.TenantID = another.ID
	otherPlatform.Revision = 0
	otherPlatform = save(otherPlatform)
	otherRepo := repo
	otherRepo.ID = "org-test-other-repo"
	otherRepo.TenantID = another.ID
	otherRepo.PlatformID = otherPlatform.ID
	otherRepo.ParentID = otherPlatform.ID
	otherRepo.Revision = 0
	if _, err = store.SaveOrganizationEntity(ctx, otherRepo, false); !errors.Is(err, organization.ErrConflict) {
		t.Fatalf("graph registration shared across tenants: %v", err)
	}
	bad := old
	bad.ID = "org-test-cross-team"
	bad.Revision = 0
	bad.PlatformID = otherPlatform.ID
	bad.ParentID = otherPlatform.ID
	if _, err = store.SaveOrganizationEntity(ctx, bad, false); !errors.Is(err, organization.ErrInvalid) {
		t.Fatalf("cross-tenant parent: %v", err)
	}
}
