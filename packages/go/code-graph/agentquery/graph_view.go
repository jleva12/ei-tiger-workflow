package agentquery

import (
	"context"
	"fmt"
	"sort"
	"time"

	"ei-aitiger-codegraph/pkg/graph"
)

// GraphViewStore is the bounded read surface used by the visual explorer.
type GraphViewStore interface {
	State(context.Context, string) (graph.RepositoryState, error)
	GenerationCommit(context.Context, string, uint64) (string, error)
	ListNodes(context.Context, graph.ListQuery) (graph.NodePage, error)
	Neighbors(context.Context, graph.NeighborQuery) (graph.NeighborPage, error)
}

type GraphViewRequest struct {
	RepositoryID string
	Generation   uint64
	Kind         string
	Cursor       string
}

type GraphViewResult struct {
	RepositoryID string          `json:"repository_id"`
	Branch       string          `json:"branch,omitempty"`
	CommitSHA    string          `json:"commit_sha,omitempty"`
	Generation   uint64          `json:"generation"`
	Nodes        []graph.Version `json:"nodes"`
	Edges        []graph.Version `json:"edges"`
	NextCursor   string          `json:"next_cursor,omitempty"`
	Truncated    bool            `json:"truncated"`
}

// GraphView loads twelve seed nodes and one bounded neighborhood around each.
// The generation is resolved once, before any reads. Type counts in the UI
// describe this loaded sample, not totals for the repository. More seeds use
// NextCursor; individual nodes can be expanded with the neighbors endpoint.
func GraphView(ctx context.Context, store GraphViewStore, q GraphViewRequest) (GraphViewResult, error) {
	out := GraphViewResult{RepositoryID: q.RepositoryID, Nodes: []graph.Version{}, Edges: []graph.Version{}}
	if q.RepositoryID == "" || len(q.Kind) > 128 || len(q.Cursor) > 8192 {
		return out, fmt.Errorf("%w: repository, kind or cursor", graph.ErrInvalid)
	}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	state, err := store.State(ctx, q.RepositoryID)
	if err != nil {
		return out, err
	}
	if q.Generation > state.LiveGeneration {
		return out, fmt.Errorf("%w: generation is not published", graph.ErrInvalid)
	}
	if q.Generation == 0 {
		q.Generation = state.LiveGeneration
	}
	out.Generation = q.Generation
	out.Branch = state.Branch
	if q.Generation == 0 {
		return out, nil
	}
	out.CommitSHA = state.LiveCommit
	if q.Generation != state.LiveGeneration {
		out.CommitSHA, err = store.GenerationCommit(ctx, q.RepositoryID, q.Generation)
		if err != nil {
			return out, err
		}
	}
	page, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: q.RepositoryID, Generation: q.Generation, Kind: q.Kind, Cursor: q.Cursor, Limit: 12})
	if err != nil {
		return out, err
	}
	if page.Generation != q.Generation {
		return out, fmt.Errorf("%w: graph generation changed", graph.ErrInvalid)
	}
	out.NextCursor, out.Truncated = page.NextCursor, page.NextCursor != ""
	nodes := map[string]graph.Version{}
	edges := map[string]graph.Version{}
	for _, v := range page.Nodes {
		if v.Fact.Node != nil {
			nodes[v.Fact.Node.ID] = v
		}
	}
	for _, seed := range page.Nodes {
		if seed.Fact.Node == nil {
			continue
		}
		neighbors, err := store.Neighbors(ctx, graph.NeighborQuery{RepositoryID: q.RepositoryID, NodeID: seed.Fact.Node.ID, Direction: graph.Both, Generation: q.Generation, Limit: 24})
		if err != nil {
			return out, err
		}
		if neighbors.Generation != q.Generation {
			return out, fmt.Errorf("%w: graph generation changed", graph.ErrInvalid)
		}
		out.Truncated = out.Truncated || neighbors.NextCursor != ""
		for _, neighbor := range neighbors.Neighbors {
			e := neighbor.Edge.Fact.Edge
			if e == nil {
				continue
			}
			if neighbor.Node != nil && neighbor.Node.Fact.Node != nil {
				n := neighbor.Node.Fact.Node
				if _, exists := nodes[n.ID]; exists || len(nodes) < 160 {
					nodes[n.ID] = *neighbor.Node
				}
			}
			_, hasSource := nodes[e.SourceID]
			_, hasTarget := nodes[e.TargetID]
			if !hasSource || !hasTarget {
				out.Truncated = true
				continue
			}
			edges[e.ID] = neighbor.Edge
		}
	}
	for _, v := range nodes {
		out.Nodes = append(out.Nodes, v)
	}
	for _, v := range edges {
		out.Edges = append(out.Edges, v)
	}
	sort.Slice(out.Nodes, func(i, j int) bool { return out.Nodes[i].Fact.Node.ID < out.Nodes[j].Fact.Node.ID })
	sort.Slice(out.Edges, func(i, j int) bool { return out.Edges[i].Fact.Edge.ID < out.Edges[j].Fact.Edge.ID })
	return out, nil
}
