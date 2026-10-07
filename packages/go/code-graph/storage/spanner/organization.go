package spannerstore

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"

	"cloud.google.com/go/spanner"
	"google.golang.org/api/iterator"

	"ei-aitiger-codegraph/authorization"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/organization"
)

type OrganizationPage struct {
	Items      []organization.Entity `json:"items"`
	NextCursor string                `json:"next_cursor,omitempty"`
}

// ListOrganizationEntities is an operator administration listing. Tenant is a
// query scope, not authentication. Cursors are bound to that scope.
func (s *Store) ListOrganizationEntities(ctx context.Context, tenant string, limit int, cursor string) (OrganizationPage, error) {
	out := OrganizationPage{Items: []organization.Entity{}}
	if tenant != "" && !organization.ValidID(tenant) {
		return out, organization.ErrInvalid
	}
	if limit <= 0 || limit > s.limits.MaxPageSize {
		limit = s.limits.MaxPageSize
	}
	predicate, authParams := "TRUE", map[string]any{}
	if authorization.HasContext(ctx) {
		readScope, err := authorization.ReadScope(ctx, "organization", "read")
		if err != nil {
			return out, err
		}
		if !readScope.SiteConfiguration && tenant != "" && tenant != readScope.TenantID {
			return out, authorization.ErrNotFound
		}
		if !readScope.SiteConfiguration {
			tenant = readScope.TenantID
		}
		predicate, authParams, err = readScope.SQL(authorization.Columns{Tenant: "TenantID", Owner: "JSON_VALUE(CAST(Payload AS STRING), '$.owner_id')", Status: "JSON_VALUE(CAST(Payload AS STRING), '$.status')"})
		if err != nil {
			return out, err
		}
	}
	scope := s.cursors.scope("organization", tenant, authorizationCursor(ctx))
	position, err := s.cursors.decode(scope, 0, cursor)
	if err != nil {
		return out, err
	}
	lastTenant, lastID, _ := strings.Cut(position, "|")
	stmt := spanner.Statement{SQL: `SELECT TenantID, EntityID, Payload FROM CGOrganizationEntities
WHERE ` + predicate + ` AND (@tenant='' OR TenantID=@tenant) AND (TenantID>@lastTenant OR (TenantID=@lastTenant AND EntityID>@lastID))
ORDER BY TenantID, EntityID LIMIT @limit`, Params: map[string]any{"tenant": tenant, "lastTenant": lastTenant, "lastID": lastID, "limit": int64(limit + 1)}}
	addParams(stmt.Params, authParams)
	it := s.client.Single().Query(ctx, stmt)
	defer it.Stop()
	for {
		row, err := it.Next()
		if err == iterator.Done {
			break
		}
		if err != nil {
			return out, err
		}
		if len(out.Items) == limit {
			out.NextCursor = s.cursors.encode(scope, 0, lastTenant+"|"+lastID)
			break
		}
		var b []byte
		if err = row.Columns(&lastTenant, &lastID, &b); err != nil {
			return out, err
		}
		var e organization.Entity
		if err = json.Unmarshal(b, &e); err != nil {
			return out, err
		}
		out.Items = append(out.Items, e)
	}
	return out, nil
}

// organizationTenant reads the tenant's configuration under one transaction so
// validation, scope resolution and archive dependencies cannot race each other.
func organizationTenant(ctx context.Context, t reader, tenant string) ([]organization.Entity, error) {
	if !organization.ValidID(tenant) {
		return nil, organization.ErrInvalid
	}
	const maxEntities = 10000
	it := t.Query(ctx, spanner.Statement{SQL: "SELECT Payload FROM CGOrganizationEntities WHERE TenantID=@tenant LIMIT @limit", Params: map[string]any{"tenant": tenant, "limit": int64(maxEntities + 1)}})
	defer it.Stop()
	out := []organization.Entity{}
	for {
		row, err := it.Next()
		if err == iterator.Done {
			break
		}
		if err != nil {
			return nil, err
		}
		if len(out) == maxEntities {
			return nil, fmt.Errorf("%w: tenant configuration exceeds the supported 10,000 record limit", organization.ErrInvalid)
		}
		var b []byte
		if err = row.Column(0, &b); err != nil {
			return nil, err
		}
		var e organization.Entity
		if err = json.Unmarshal(b, &e); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, nil
}

func (s *Store) OrganizationTenant(ctx context.Context, tenant string) ([]organization.Entity, error) {
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	return organizationTenant(ctx, t, tenant)
}

func (s *Store) SaveOrganizationEntity(ctx context.Context, e organization.Entity, acknowledgeOverlap bool) (organization.Entity, error) {
	if err := e.Validate(); err != nil {
		return organization.Entity{}, err
	}
	var out organization.Entity
	err := s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		records, err := organizationTenant(ctx, t, e.TenantID)
		if err != nil {
			return err
		}
		if authorization.HasContext(ctx) {
			identity, err := authorization.CurrentIdentity(ctx)
			if err != nil {
				return err
			}
			siteWrite, err := authorization.CanAdministerSite(ctx, "write")
			if err != nil {
				return err
			}
			if e.TenantID != identity.TenantID && !siteWrite && !MemberOwned(ctx, e) {
				return authorization.ErrNotFound
			}
			old, exists := organization.Find(records, e.ID)
			if exists {
				if err = authorization.Require(ctx, OrganizationResource(old), "read"); err != nil {
					if errors.Is(err, authorization.ErrForbidden) {
						return authorization.ErrNotFound
					}
					return err
				}
				if old.Revision != e.Revision {
					return organization.ErrConflict
				}
				comparison := e
				comparison.Status = old.Status
				comparison.CreatedAt = old.CreatedAt
				comparison.UpdatedAt = old.UpdatedAt
				if e.Status != old.Status && !reflect.DeepEqual(comparison, old) {
					if err = authorization.Require(ctx, OrganizationResource(old), "update"); err != nil {
						return err
					}
				}
				action := "update"
				if e.Status != old.Status {
					action = "archive"
					if e.Status != organization.Archived {
						action = "transition"
					}
				}
				if err = authorization.Require(ctx, OrganizationResource(old), action); err != nil {
					return err
				}
				if e.OwnerID != old.OwnerID {
					return authorization.ErrInvalid
				}
			} else {
				if !MemberOwned(ctx, e) {
					if err = authorization.RequireCapability(ctx, "organization_collection", "create"); err != nil {
						return err
					}
				}
				if e.OwnerID != "" && e.OwnerID != identity.SubjectID {
					return authorization.ErrInvalid
				}
				e.OwnerID = identity.SubjectID
			}
		}
		if len(records) >= 10000 && e.Revision == 0 {
			return fmt.Errorf("%w: tenant configuration is limited to 10,000 records", organization.ErrInvalid)
		}
		timestamp, err := now(ctx, t)
		if err != nil {
			return err
		}
		out, err = organization.Prepare(records, e, acknowledgeOverlap, timestamp)
		if err != nil {
			return err
		}
		graphID := spanner.NullString{}
		if out.Kind == organization.Repository {
			row, err := readRepo(ctx, t, out.GraphRepositoryID)
			if err != nil {
				return err
			}
			registered, err := row.repository()
			if err != nil {
				return err
			}
			if authorization.HasContext(ctx) {
				if err = authorizeRepository(ctx, t, registered, "read"); err != nil {
					return err
				}
			}
			if registered.GitHubURL != out.RemoteURL {
				return fmt.Errorf("%w: Git URL does not match the registered repository", organization.ErrInvalid)
			}
			graphID = spanner.NullString{StringVal: out.GraphRepositoryID, Valid: true}
		}
		// Parent IDs are globally generated. Tenant slugs share the empty parent.
		slugKey := string(out.Kind) + "/" + out.ParentID + "/" + out.Slug
		return t.BufferWrite([]*spanner.Mutation{spanner.InsertOrUpdate("CGOrganizationEntities", []string{"TenantID", "EntityID", "Kind", "PlatformID", "ParentID", "SlugKey", "GraphRepositoryID", "Revision", "Payload"}, []any{out.TenantID, out.ID, string(out.Kind), out.PlatformID, out.ParentID, slugKey, graphID, out.Revision, payload(out)})})
	})
	if errors.Is(err, deployment.ErrConflictingReplay) {
		return organization.Entity{}, fmt.Errorf("%w: this slug or backing repository is already assigned; choose a different one", organization.ErrConflict)
	}
	return out, err
}
