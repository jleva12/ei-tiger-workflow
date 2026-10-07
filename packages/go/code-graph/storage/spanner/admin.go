package spannerstore

import (
	"context"

	"cloud.google.com/go/spanner"
	"ei-aitiger-codegraph/authorization"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

type RepositorySummary struct {
	Repository    deployment.Repository   `json:"repository"`
	Graph         graph.RepositoryState   `json:"graph"`
	Permissions   map[string]bool         `json:"permissions,omitempty"`
	Authorization *authorization.Metadata `json:"authorization,omitempty"`
}
type RepositoryPage struct {
	Items      []RepositorySummary `json:"items"`
	NextCursor string              `json:"next_cursor,omitempty"`
}

func (s *Store) ListRepositories(ctx context.Context, limit int, cursor string) (RepositoryPage, error) {
	out := RepositoryPage{Items: []RepositorySummary{}}
	if limit < 1 || limit > 200 {
		return out, deployment.ErrInvalidRequest
	}
	scope := s.cursors.scope("admin-repositories", authorizationCursor(ctx))
	after, err := s.cursors.decode(scope, 0, cursor)
	if err != nil {
		return out, err
	}
	predicate, params, err := repositoryPredicate(ctx, "p")
	if err != nil {
		return out, err
	}
	params["after"] = after
	params["limit"] = int64(limit + 1)
	it := s.client.Single().Query(ctx, spanner.Statement{SQL: "SELECT RepositoryID, Revision, Payload, LiveGeneration, LiveCommit, LiveRunID FROM CGRepositories p WHERE (" + predicate + ") AND RepositoryID>@after ORDER BY RepositoryID LIMIT @limit", Params: params})
	defer it.Stop()
	for {
		row, err := nextRow(it)
		if err != nil {
			return out, err
		}
		if row == nil {
			return out, nil
		}
		if len(out.Items) == limit {
			out.NextCursor = s.cursors.encode(scope, 0, out.Items[limit-1].Repository.RepositoryID)
			return out, nil
		}
		var r repoRow
		if err = row.Columns(&r.ID, &r.Revision, &r.Payload, &r.LiveGeneration, &r.LiveCommit, &r.LiveRunID); err != nil {
			return out, err
		}
		repo, err := r.repository()
		if err != nil {
			return out, err
		}
		state, err := r.state()
		if err != nil {
			return out, err
		}
		item := RepositorySummary{Repository: repo, Graph: state}
		if authorization.HasContext(ctx) {
			resource, err := repositoryResource(ctx, s.client.Single(), repo)
			if err != nil {
				return out, err
			}
			permissions, metadata, err := authorization.ResourcePermissions(ctx, resource, "update", "ingest")
			if err != nil {
				return out, err
			}
			item.Permissions = permissions
			item.Authorization = &metadata
		}
		out.Items = append(out.Items, item)
	}
}
