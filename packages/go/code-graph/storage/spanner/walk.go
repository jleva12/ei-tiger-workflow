package spannerstore

import (
	"context"
	"fmt"
	"sort"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/graph"
)

// EdgeRef is an edge as the edge indexes hold it: identity, kind and both
// endpoints, without the payload. Walks that only need to know which nodes
// connect read these instead of full records.
type EdgeRef struct {
	ID       string `json:"id"`
	Kind     string `json:"kind"`
	SourceID string `json:"source_id"`
	TargetID string `json:"target_id"`
}

// walkChunk bounds the node ids one index query takes.
const walkChunk = 500

// DependencyEdges reads the edges open at generation that touch any of the
// nodes, in the given direction, restricted to kinds when given, from the
// edge indexes alone: one query per chunk of nodes rather than one per node,
// and no payload. Direction Outgoing reads edges whose source is a node,
// Incoming edges whose target is one, Both reads both sets. At most limit
// edges are returned in total; truncated reports whether that cut anything.
func (s *Store) DependencyEdges(ctx context.Context, repo string, nodeIDs []string, direction graph.Direction, kinds []string, generation uint64, limit int) (edges []EdgeRef, truncated bool, err error) {
	if !validRepositoryID(repo) || !validKinds(kinds) || len(nodeIDs) == 0 || limit < 1 {
		return nil, false, fmt.Errorf("%w: dependency edge query", graph.ErrInvalid)
	}
	for _, id := range nodeIDs {
		if !graph.ValidID(id) {
			return nil, false, fmt.Errorf("%w: node id %q", graph.ErrInvalid, id)
		}
	}
	switch direction {
	case graph.Outgoing, graph.Incoming, graph.Both:
	default:
		return nil, false, fmt.Errorf("%w: direction %q", graph.ErrInvalid, direction)
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen, _, err := s.generationFor(ctx, t, repo, generation)
	if err != nil {
		return nil, false, err
	}
	unique := make([]string, 0, len(nodeIDs))
	seen := map[string]bool{}
	for _, id := range nodeIDs {
		if !seen[id] {
			seen[id] = true
			unique = append(unique, id)
		}
	}
	for _, dir := range []graph.Direction{graph.Outgoing, graph.Incoming} {
		if direction != graph.Both && direction != dir {
			continue
		}
		index, column := "CGEdgesBySource", "SourceID"
		if dir == graph.Incoming {
			index, column = "CGEdgesByTarget", "TargetID"
		}
		for start := 0; start < len(unique) && !truncated; start += walkChunk {
			chunk := unique[start:min(start+walkChunk, len(unique))]
			remaining := limit - len(edges)
			if remaining <= 0 {
				truncated = true
				break
			}
			params := map[string]any{"repo": repo, "ids": chunk, "gen": int64(gen), "limit": int64(remaining + 1)}
			filter := ""
			if len(kinds) > 0 {
				filter = " AND Kind IN UNNEST(@kinds)"
				params["kinds"] = kinds
			}
			it := t.Query(ctx, spanner.Statement{
				SQL:    nullFilteredHint + "SELECT RecordID, Kind, SourceID, TargetID FROM CGRecords@{FORCE_INDEX=" + index + "} WHERE RepositoryID=@repo AND " + column + " IS NOT NULL AND " + column + " IN UNNEST(@ids) AND " + openPredicate("") + filter + " ORDER BY RecordID LIMIT @limit",
				Params: params,
			})
			rows, err := collectEdgeRefs(it)
			if err != nil {
				return nil, false, err
			}
			if len(rows) > remaining {
				rows = rows[:remaining]
				truncated = true
			}
			edges = append(edges, rows...)
		}
	}
	return edges, truncated, nil
}

func collectEdgeRefs(it *spanner.RowIterator) ([]EdgeRef, error) {
	defer it.Stop()
	var out []EdgeRef
	for {
		row, err := nextRow(it)
		if err != nil {
			return nil, err
		}
		if row == nil {
			return out, nil
		}
		var e EdgeRef
		var source, target spanner.NullString
		if err := row.Columns(&e.ID, &e.Kind, &source, &target); err != nil {
			return nil, err
		}
		e.SourceID, e.TargetID = source.StringVal, target.StringVal
		out = append(out, e)
	}
}

// Hub is a node ranked by the open edges pointing at it.
type Hub struct {
	NodeID   string           `json:"node_id"`
	InDegree int64            `json:"in_degree"`
	ByKind   map[string]int64 `json:"by_kind"`
}

// maxHubs bounds one hubs query.
const maxHubs = 200

// Hubs ranks the nodes of a repository by incoming edges of the given kinds
// (all semantic kinds when empty) at generation, aggregated on the target
// index without reading payloads, and returns the top limit with a per-kind
// breakdown. It scans every open edge of the repository once.
func (s *Store) Hubs(ctx context.Context, repo string, generation uint64, kinds []string, limit int) ([]Hub, error) {
	if !validRepositoryID(repo) || !validKinds(kinds) || limit < 1 || limit > maxHubs {
		return nil, fmt.Errorf("%w: hubs query", graph.ErrInvalid)
	}
	if len(kinds) == 0 {
		kinds = SemanticEdgeKinds()
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen, _, err := s.generationFor(ctx, t, repo, generation)
	if err != nil {
		return nil, err
	}
	it := t.Query(ctx, spanner.Statement{
		SQL: nullFilteredHint + `SELECT TargetID, SUM(n) AS total, ARRAY_AGG(STRUCT(Kind AS kind, n AS n)) AS kinds FROM (
  SELECT TargetID, Kind, COUNT(*) AS n FROM CGRecords@{FORCE_INDEX=CGEdgesByTarget}
  WHERE RepositoryID=@repo AND TargetID IS NOT NULL AND Kind IN UNNEST(@kinds) AND ` + openPredicate("") + `
  GROUP BY TargetID, Kind
) GROUP BY TargetID ORDER BY total DESC, TargetID LIMIT @limit`,
		Params: map[string]any{"repo": repo, "kinds": kinds, "gen": int64(gen), "limit": int64(limit)},
	})
	defer it.Stop()
	var out []Hub
	for {
		row, err := nextRow(it)
		if err != nil {
			return nil, err
		}
		if row == nil {
			break
		}
		var h Hub
		var parts []*struct {
			Kind string `spanner:"kind"`
			N    int64  `spanner:"n"`
		}
		if err := row.Columns(&h.NodeID, &h.InDegree, &parts); err != nil {
			return nil, err
		}
		h.ByKind = make(map[string]int64, len(parts))
		for _, p := range parts {
			if p != nil {
				h.ByKind[p.Kind] += p.N
			}
		}
		out = append(out, h)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].InDegree > out[j].InDegree })
	return out, nil
}
