package spannerstore

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"

	"ei-aitiger-codegraph/pkg/graph"
)

// MaxCrossLinks bounds one owner's set of cross-repository links.
const MaxCrossLinks = 2000

var crossLinkColumns = []string{"Owner", "LinkID", "Kind", "SourceRepositoryID", "SourceNodeID", "SourceQualifiedName", "TargetRepositoryID", "TargetNodeID", "TargetQualifiedName", "Payload", "UpdatedAt"}

func nullableText(s string) spanner.NullString {
	return spanner.NullString{StringVal: s, Valid: s != ""}
}

// ReplaceCrossLinks makes links the owner's whole set of cross-repository
// links, in one transaction: links it had and no longer lists are removed.
// An empty set removes them all. Links are kept beside the repositories'
// graphs, not in them, so replacing them never touches a generation.
func (s *Store) ReplaceCrossLinks(ctx context.Context, owner string, links []graph.CrossLink) error {
	if !graph.ValidCrossToken(owner) || len(links) > MaxCrossLinks {
		return fmt.Errorf("%w: cross link owner, or more than %d links", graph.ErrInvalid, MaxCrossLinks)
	}
	seen := map[string]bool{}
	ms := []*spanner.Mutation{spanner.Delete("CGCrossLinks", spanner.Key{owner}.AsPrefix())}
	for _, l := range links {
		if err := l.Validate(); err != nil {
			return err
		}
		if l.Owner != owner || seen[l.ID] {
			return fmt.Errorf("%w: cross link %s: another owner, or listed twice", graph.ErrInvalid, l.ID)
		}
		seen[l.ID] = true
		ms = append(ms, spanner.Insert("CGCrossLinks", crossLinkColumns, []any{
			owner, l.ID, l.Kind,
			l.Source.RepositoryID, l.Source.NodeID, nullableText(l.Source.QualifiedName),
			l.Target.RepositoryID, l.Target.NodeID, nullableText(l.Target.QualifiedName),
			payload(l), spanner.CommitTimestamp,
		}))
	}
	return s.tx(ctx, func(_ context.Context, t *spanner.ReadWriteTransaction) error {
		return t.BufferWrite(ms)
	})
}

// CrossLinks returns the cross-repository links touching a repository on
// the query's side: their source there (Outgoing), their target (Incoming)
// or either (Both), narrowed to the query's nodes by ID or qualified name.
// A database without the table, one made before cross-repository links,
// has none.
func (s *Store) CrossLinks(ctx context.Context, q graph.CrossLinkQuery) ([]graph.CrossLink, error) {
	if err := q.Validate(); err != nil {
		return nil, err
	}
	var out []graph.CrossLink
	seen := map[string]bool{}
	for _, dir := range []graph.Direction{graph.Outgoing, graph.Incoming} {
		if q.Direction != graph.Both && q.Direction != dir {
			continue
		}
		side := "Source"
		if dir == graph.Incoming {
			side = "Target"
		}
		sql := "SELECT Owner, LinkID, Payload FROM CGCrossLinks@{FORCE_INDEX=CGCrossLinksBy" + side + "} WHERE " + side + "RepositoryID=@repo"
		params := map[string]any{"repo": q.RepositoryID}
		var match []string
		if len(q.NodeIDs) > 0 {
			match = append(match, side+"NodeID IN UNNEST(@ids)")
			params["ids"] = q.NodeIDs
		}
		if len(q.QualifiedNames) > 0 {
			match = append(match, side+"QualifiedName IN UNNEST(@names)")
			params["names"] = q.QualifiedNames
		}
		if len(match) > 0 {
			sql += " AND (" + strings.Join(match, " OR ") + ")"
		}
		it := s.client.Single().Query(ctx, spanner.Statement{SQL: sql + " LIMIT @limit", Params: withLimit(params)})
		links, err := collectCrossLinks(it)
		if missingTable(err) {
			return nil, nil
		}
		if err != nil {
			return nil, err
		}
		for _, l := range links {
			if key := l.Owner + "\x00" + l.ID; !seen[key] {
				seen[key] = true
				out = append(out, l)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Owner != out[j].Owner {
			return out[i].Owner < out[j].Owner
		}
		return out[i].ID < out[j].ID
	})
	return out, nil
}

// OwnedCrossLinks returns an owner's links, by ID.
func (s *Store) OwnedCrossLinks(ctx context.Context, owner string) ([]graph.CrossLink, error) {
	if !graph.ValidCrossToken(owner) {
		return nil, fmt.Errorf("%w: cross link owner", graph.ErrInvalid)
	}
	it := s.client.Single().Read(ctx, "CGCrossLinks", spanner.Key{owner}.AsPrefix(), []string{"Owner", "LinkID", "Payload"})
	links, err := collectCrossLinks(it)
	if missingTable(err) {
		return nil, nil
	}
	return links, err
}

func withLimit(params map[string]any) map[string]any {
	// Every owner's whole set is at most MaxCrossLinks, and a repository is
	// on one side of few sets.
	params["limit"] = int64(10 * MaxCrossLinks)
	return params
}

func collectCrossLinks(it *spanner.RowIterator) ([]graph.CrossLink, error) {
	defer it.Stop()
	var out []graph.CrossLink
	for {
		row, err := nextRow(it)
		if err != nil {
			return nil, err
		}
		if row == nil {
			return out, nil
		}
		var owner, id string
		var b []byte
		if err := row.Columns(&owner, &id, &b); err != nil {
			return nil, err
		}
		var l graph.CrossLink
		if err := json.Unmarshal(b, &l); err != nil {
			return nil, fmt.Errorf("%w: cross link %s payload: %v", graph.ErrIntegrity, id, err)
		}
		if l.Owner != owner || l.ID != id {
			return nil, fmt.Errorf("%w: cross link %s payload key differs", graph.ErrIntegrity, id)
		}
		out = append(out, l)
	}
}

// missingTable reports a read or query of a table the database doesn't have:
// queries fail with InvalidArgument, reads with NotFound.
func missingTable(err error) bool {
	if err == nil {
		return false
	}
	code := spanner.ErrCode(err)
	return (code == codes.InvalidArgument || code == codes.NotFound) && strings.Contains(err.Error(), "Table not found")
}
