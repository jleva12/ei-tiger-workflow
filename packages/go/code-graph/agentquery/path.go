package agentquery

import (
	"context"
	"fmt"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

const (
	defaultPathHops = 4
	maxPathHops     = 8
	// maxPathVisited bounds the nodes a path search may discover on both
	// sides together before it gives up.
	maxPathVisited = 5000
	pathEdgeBudget = 20000
)

// PathRequest asks how FromID reaches ToID over proven edges. Direction
// Outgoing (the default) follows dependencies: a path A -> B -> C means A
// depends on B and B on C. Both ignores edge direction. Kinds defaults to
// the semantic edge kinds plus contains, so a path may descend from a type
// into the member that holds the dependency.
type PathRequest struct {
	RepositoryID string
	FromID       string
	ToID         string
	Generation   uint64
	MaxHops      int
	Kinds        []string
	Direction    graph.Direction
}

// PathEdge is one step of a path.
type PathEdge struct {
	EdgeID   string `json:"edge_id"`
	Kind     string `json:"kind"`
	SourceID string `json:"source_id"`
	TargetID string `json:"target_id"`
}

// PathResult is the shortest path found, as briefs and edges in order from
// FromID to ToID, or Found false. Visited counts the nodes the search
// discovered; Truncated reports that it stopped at a bound before deciding.
type PathResult struct {
	From      string     `json:"from"`
	To        string     `json:"to"`
	Found     bool       `json:"found"`
	Hops      int        `json:"hops"`
	Nodes     []Brief    `json:"nodes"`
	Edges     []PathEdge `json:"edges"`
	Visited   int        `json:"visited"`
	Truncated bool       `json:"truncated"`
}

// Path runs a bidirectional breadth-first search: the forward side follows
// edges from FromID, the backward side follows them into ToID, each side
// expanding one level per query over the edge indexes, until the sides
// meet, MaxHops levels have been expanded, or the visited bound is hit.
func Path(ctx context.Context, st Store, req PathRequest) (PathResult, error) {
	if req.MaxHops < 0 || req.MaxHops > maxPathHops {
		return PathResult{}, fmt.Errorf("%w: path max_hops 0-%d", deployment.ErrInvalidRequest, maxPathHops)
	}
	if req.MaxHops == 0 {
		req.MaxHops = defaultPathHops
	}
	switch req.Direction {
	case "":
		req.Direction = graph.Outgoing
	case graph.Outgoing, graph.Both:
	default:
		return PathResult{}, fmt.Errorf("%w: path direction must be out or both", deployment.ErrInvalidRequest)
	}
	if len(req.Kinds) == 0 {
		// A class depends on nothing itself; its members do. Following
		// contains lets a path start at a type and descend into the member
		// that carries the dependency, and end at a type through a member.
		req.Kinds = append(spannerstore.SemanticEdgeKinds(), graph.EdgeContains)
	}
	for _, id := range []string{req.FromID, req.ToID} {
		if _, err := st.GetNode(ctx, req.RepositoryID, id, req.Generation); err != nil {
			return PathResult{}, err
		}
	}
	result := PathResult{From: req.FromID, To: req.ToID, Nodes: []Brief{}, Edges: []PathEdge{}}
	if req.FromID == req.ToID {
		return finishPath(ctx, st, req, result, []string{req.FromID}, nil)
	}
	type side struct {
		frontier []string
		visited  map[string]bool
		parent   map[string]spannerstore.EdgeRef // the edge that discovered the node
		hops     int
	}
	forward := &side{frontier: []string{req.FromID}, visited: map[string]bool{req.FromID: true}, parent: map[string]spannerstore.EdgeRef{}}
	backward := &side{frontier: []string{req.ToID}, visited: map[string]bool{req.ToID: true}, parent: map[string]spannerstore.EdgeRef{}}
	var meet string
	for meet == "" && forward.hops+backward.hops < req.MaxHops && len(forward.frontier) > 0 && len(backward.frontier) > 0 {
		// Expand the smaller frontier; ties go to the forward side.
		s, other := forward, backward
		direction := graph.Outgoing
		if len(backward.frontier) < len(forward.frontier) {
			s, other, direction = backward, forward, graph.Incoming
		}
		if req.Direction == graph.Both {
			direction = graph.Both
		}
		refs, cut, err := st.DependencyEdges(ctx, req.RepositoryID, s.frontier, direction, req.Kinds, req.Generation, pathEdgeBudget)
		if err != nil {
			return PathResult{}, err
		}
		if cut {
			result.Truncated = true
		}
		inFrontier := make(map[string]bool, len(s.frontier))
		for _, id := range s.frontier {
			inFrontier[id] = true
		}
		var next []string
		for _, e := range refs {
			far := e.TargetID
			if s == backward || (req.Direction == graph.Both && !inFrontier[e.SourceID]) {
				far = e.SourceID
			}
			if s == backward && req.Direction == graph.Both && !inFrontier[e.TargetID] {
				far = e.TargetID
			}
			if far == "" || s.visited[far] {
				continue
			}
			s.visited[far] = true
			s.parent[far] = e
			next = append(next, far)
			if other.visited[far] {
				meet = far
				break
			}
		}
		s.frontier = next
		s.hops++
		result.Visited = len(forward.visited) + len(backward.visited) - 2
		if result.Visited > maxPathVisited {
			result.Truncated = true
			break
		}
	}
	if meet == "" {
		return result, nil
	}
	// Walk back from the meeting node to each end and stitch the two halves.
	var ids []string
	var edges []PathEdge
	for id := meet; id != req.FromID; {
		e := forward.parent[id]
		edges = append([]PathEdge{{EdgeID: e.ID, Kind: e.Kind, SourceID: e.SourceID, TargetID: e.TargetID}}, edges...)
		ids = append([]string{id}, ids...)
		id = e.SourceID
		if req.Direction == graph.Both && id == ids[0] {
			id = e.TargetID
		}
	}
	ids = append([]string{req.FromID}, ids...)
	for id := meet; id != req.ToID; {
		e := backward.parent[id]
		edges = append(edges, PathEdge{EdgeID: e.ID, Kind: e.Kind, SourceID: e.SourceID, TargetID: e.TargetID})
		next := e.TargetID
		if req.Direction == graph.Both && next == id {
			next = e.SourceID
		}
		ids = append(ids, next)
		id = next
	}
	result.Found, result.Hops = true, len(edges)
	return finishPath(ctx, st, req, result, ids, edges)
}

func finishPath(ctx context.Context, st Store, req PathRequest, result PathResult, ids []string, edges []PathEdge) (PathResult, error) {
	nodes, err := st.GetNodes(ctx, req.RepositoryID, ids, req.Generation)
	if err != nil {
		return PathResult{}, err
	}
	for _, id := range ids {
		if v, ok := nodes[id]; ok && v.Fact.Node != nil {
			result.Nodes = append(result.Nodes, Briefly(*v.Fact.Node))
		} else {
			result.Nodes = append(result.Nodes, Brief{ID: id})
		}
	}
	if edges != nil {
		result.Edges = edges
	}
	if len(ids) == 1 {
		result.Found = true
	}
	return result, nil
}
