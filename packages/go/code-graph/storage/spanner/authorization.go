package spannerstore

import (
	"context"
	"errors"
	"slices"
	"strconv"

	"cloud.google.com/go/spanner"
	"ei-aitiger-codegraph/authorization"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/organization"
)

// OrganizationResource carries the record's team ownership so membership
// decisions can apply: a team is its own Team; responsibilities belong to their
// team; a selected-teams workspace belongs to every selected team.
func OrganizationResource(e organization.Entity) authorization.Resource {
	r := authorization.Resource{Kind: "organization", ID: e.ID, TenantID: e.TenantID, OwnerID: e.OwnerID, Status: e.Status, Version: strconv.FormatInt(e.Revision, 10)}
	switch e.Kind {
	case organization.Team:
		r.Team = e.ID
	case organization.Responsibility:
		r.Teams = []string{e.ParentID}
	case organization.Workspace:
		if e.ScopeMode == organization.SelectedTeams {
			r.Teams = slices.Clone(e.TeamIDs)
		}
	}
	return r
}

// MemberOwned reports whether the caller belongs to every team that would own
// this record, which lets team members create their team's workspaces and
// code ownership without an organization-wide create grant.
func MemberOwned(ctx context.Context, e organization.Entity) bool {
	switch e.Kind {
	case organization.Responsibility:
		return e.ParentID != "" && authorization.MemberOf(ctx, e.TenantID, e.ParentID)
	case organization.Workspace:
		return e.ScopeMode == organization.SelectedTeams && authorization.MemberOf(ctx, e.TenantID, e.TeamIDs...)
	}
	return false
}

// Repository tenancy is immutable in registration payloads. Legacy registrations
// inherit an existing unique organization association; unassigned rows are
// invisible until an operator explicitly assigns them during rollout.
func repositoryTenantSQL(alias string) string {
	return "COALESCE(NULLIF(JSON_VALUE(CAST(" + alias + ".Payload AS STRING), '$.tenant_id'), ''), (SELECT e.TenantID FROM CGOrganizationEntities e WHERE e.GraphRepositoryID=" + alias + ".RepositoryID))"
}
func repositoryResource(ctx context.Context, t reader, repo deployment.Repository) (authorization.Resource, error) {
	tenant := repo.TenantID
	if tenant == "" {
		it := t.Query(ctx, spanner.Statement{SQL: "SELECT TenantID FROM CGOrganizationEntities WHERE GraphRepositoryID=@repo", Params: map[string]any{"repo": repo.RepositoryID}})
		defer it.Stop()
		row, err := nextRow(it)
		if err != nil {
			return authorization.Resource{}, err
		}
		if row != nil {
			if err = row.Columns(&tenant); err != nil {
				return authorization.Resource{}, err
			}
		}
	}
	return authorization.Resource{Kind: "repository", ID: repo.RepositoryID, TenantID: tenant, Version: strconv.FormatUint(repo.Revision, 10)}, nil
}
func authorizeRepository(ctx context.Context, t reader, repo deployment.Repository, action string) error {
	resource, err := repositoryResource(ctx, t, repo)
	if err != nil {
		return err
	}
	if err = authorization.Require(ctx, resource, "read"); err != nil {
		if errors.Is(err, authorization.ErrForbidden) {
			return authorization.ErrNotFound
		}
		return err
	}
	if action != "read" {
		return authorization.Require(ctx, resource, action)
	}
	return nil
}
func (s *Store) AuthorizeRepository(ctx context.Context, id, action string) error {
	row, err := readRepo(ctx, s.client.Single(), id)
	if err != nil {
		if errors.Is(err, deployment.ErrNotFound) {
			return authorization.ErrNotFound
		}
		return err
	}
	repo, err := row.repository()
	if err != nil {
		return err
	}
	return authorizeRepository(ctx, s.client.Single(), repo, action)
}
func (s *Store) RepositoryResource(ctx context.Context, id string) (authorization.Resource, error) {
	row, err := readRepo(ctx, s.client.Single(), id)
	if err != nil {
		return authorization.Resource{}, err
	}
	repo, err := row.repository()
	if err != nil {
		return authorization.Resource{}, err
	}
	return repositoryResource(ctx, s.client.Single(), repo)
}
func repositoryPredicate(ctx context.Context, alias string) (string, map[string]any, error) {
	if !authorization.HasContext(ctx) {
		return "TRUE", map[string]any{}, nil
	}
	scope, err := authorization.ReadScope(ctx, "repository", "read")
	if err != nil {
		return "FALSE", nil, err
	}
	return scope.SQL(authorization.Columns{Tenant: repositoryTenantSQL(alias)})
}
func authorizationCursor(ctx context.Context) string {
	i, err := authorization.CurrentIdentity(ctx)
	if err != nil {
		return ""
	}
	return i.SubjectID + ":" + i.TenantID + ":" + i.MembershipVersion + ":" + strconv.FormatBool(i.SiteAdministrator)
}
func addParams(target, source map[string]any) {
	for k, v := range source {
		target[k] = v
	}
}
