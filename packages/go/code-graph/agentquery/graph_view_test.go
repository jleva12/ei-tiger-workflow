package agentquery

import (
	"context"
	"errors"
	"fmt"
	"testing"

	"ei-aitiger-codegraph/pkg/graph"
)

type viewStore struct {
	generation uint64
	page       graph.NodePage
	neighbors  map[string]graph.NeighborPage
	queries    []graph.NeighborQuery
	list       graph.ListQuery
	err        error
}

func (s *viewStore) State(context.Context, string) (graph.RepositoryState, error) {
	return graph.RepositoryState{RepositoryID: "repo", Branch: "main", LiveCommit: fmt.Sprintf("commit-%d", s.generation), LiveGeneration: s.generation}, nil
}
func (s *viewStore) GenerationCommit(_ context.Context, _ string, generation uint64) (string, error) {
	return fmt.Sprintf("commit-%d", generation), nil
}
func (s *viewStore) ListNodes(_ context.Context, q graph.ListQuery) (graph.NodePage, error) {
	s.list = q
	return s.page, nil
}
func (s *viewStore) Neighbors(_ context.Context, q graph.NeighborQuery) (graph.NeighborPage, error) {
	s.queries = append(s.queries, q)
	return s.neighbors[q.NodeID], s.err
}
func viewNode(id string) graph.Version {
	return graph.Version{Fact: graph.Fact{Node: &graph.Node{ID: id, Kind: "method", Name: id}}, GenFrom: 1}
}
func viewEdge(id, source, target string) graph.Version {
	return graph.Version{Fact: graph.Fact{Edge: &graph.Edge{ID: id, Kind: "calls", SourceID: source, TargetID: target}}, GenFrom: 1}
}

func TestGraphViewPinsGenerationAndKeepsRealDirectedEdges(t *testing.T) {
	a, b := viewNode("a"), viewNode("b")
	edge := viewEdge("ab", "a", "b")
	s := &viewStore{generation: 9, page: graph.NodePage{Generation: 7, Nodes: []graph.Version{a, b}, NextCursor: "next-seeds"}, neighbors: map[string]graph.NeighborPage{
		"a": {Generation: 7, Neighbors: []graph.Neighbor{{Edge: edge, Node: &b}, {Edge: viewEdge("dangling", "a", "absent")}}, NextCursor: "more-neighbors"},
		"b": {Generation: 7, Neighbors: []graph.Neighbor{{Edge: edge, Node: &a}, {Edge: viewEdge("ba", "b", "a"), Node: &a}, {Edge: viewEdge("self", "b", "b"), Node: &b}}},
	}}
	out, err := GraphView(context.Background(), s, GraphViewRequest{RepositoryID: "repo", Generation: 7, Kind: "method", Cursor: "seeds"})
	if err != nil {
		t.Fatal(err)
	}
	if out.Generation != 7 || out.Branch != "main" || out.CommitSHA != "commit-7" || len(out.Nodes) != 2 || len(out.Edges) != 3 || !out.Truncated || out.NextCursor != "next-seeds" {
		t.Fatalf("snapshot: %+v", out)
	}
	if s.list.Generation != 7 || s.list.Kind != "method" || s.list.Cursor != "seeds" || s.list.Limit != 12 {
		t.Fatalf("list: %+v", s.list)
	}
	for _, q := range s.queries {
		if q.Generation != 7 || q.Direction != graph.Both || q.Limit != 24 {
			t.Fatalf("neighbor read: %+v", q)
		}
	}
	if out.Edges[0].Fact.Edge.SourceID != "a" || out.Edges[0].Fact.Edge.TargetID != "b" {
		t.Fatal("edge direction changed")
	}
}

func TestGraphViewResolvesLiveAndDoesNotExposeUnpublishedNodes(t *testing.T) {
	s := &viewStore{generation: 4, page: graph.NodePage{Generation: 4}}
	out, err := GraphView(context.Background(), s, GraphViewRequest{RepositoryID: "repo"})
	if err != nil || out.Generation != 4 || s.list.Generation != 4 {
		t.Fatalf("live resolution: %+v %v", out, err)
	}
	s = &viewStore{generation: 0}
	out, err = GraphView(context.Background(), s, GraphViewRequest{RepositoryID: "repo"})
	if err != nil || out.Generation != 0 || out.Nodes == nil || out.Edges == nil || s.list.RepositoryID != "" {
		t.Fatalf("unpublished graph: %+v %v", out, err)
	}
	if _, err = GraphView(context.Background(), s, GraphViewRequest{RepositoryID: "repo", Generation: 1}); !errors.Is(err, graph.ErrInvalid) {
		t.Fatal("future generation accepted")
	}
}

func TestGraphViewBoundsLargeNeighborhoods(t *testing.T) {
	s := &viewStore{generation: 1, page: graph.NodePage{Generation: 1}, neighbors: map[string]graph.NeighborPage{}}
	for i := 0; i < 12; i++ {
		seed := viewNode(fmt.Sprintf("seed-%02d", i))
		s.page.Nodes = append(s.page.Nodes, seed)
		page := graph.NeighborPage{Generation: 1}
		for j := 0; j < 24; j++ {
			node := viewNode(fmt.Sprintf("neighbor-%02d-%02d", i, j))
			page.Neighbors = append(page.Neighbors, graph.Neighbor{Node: &node, Edge: viewEdge(fmt.Sprintf("edge-%02d-%02d", i, j), seed.Fact.Node.ID, node.Fact.Node.ID)})
		}
		s.neighbors[seed.Fact.Node.ID] = page
	}
	out, err := GraphView(context.Background(), s, GraphViewRequest{RepositoryID: "repo"})
	if err != nil || len(out.Nodes) != 160 || !out.Truncated || len(out.Edges) > 288 {
		t.Fatalf("unbounded result: nodes=%d edges=%d err=%v", len(out.Nodes), len(out.Edges), err)
	}
	ids := map[string]bool{}
	for _, v := range out.Nodes {
		ids[v.Fact.Node.ID] = true
	}
	for _, v := range out.Edges {
		e := v.Fact.Edge
		if !ids[e.SourceID] || !ids[e.TargetID] {
			t.Fatal("dangling edge returned")
		}
	}
}

func TestGraphViewReadFailuresAndGenerationMismatch(t *testing.T) {
	s := &viewStore{generation: 1, page: graph.NodePage{Generation: 1, Nodes: []graph.Version{viewNode("a")}}, err: errors.New("read failed")}
	if _, err := GraphView(context.Background(), s, GraphViewRequest{RepositoryID: "repo"}); err != s.err {
		t.Fatal("partial graph returned on read error")
	}
	s.err = nil
	s.neighbors = map[string]graph.NeighborPage{"a": {Generation: 2}}
	if _, err := GraphView(context.Background(), s, GraphViewRequest{RepositoryID: "repo"}); !errors.Is(err, graph.ErrInvalid) {
		t.Fatal("mixed generations accepted")
	}
	if _, err := GraphView(context.Background(), nil, GraphViewRequest{}); !errors.Is(err, graph.ErrInvalid) {
		t.Fatal("empty repository accepted")
	}
}
