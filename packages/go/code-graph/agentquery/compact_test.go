package agentquery

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

func TestBriefAndCompactNeighbors(t *testing.T) {
	f := newFixture()
	b := Briefly(f.placeOrder)
	if b.ID != f.placeOrder.ID || b.Kind != "method" || b.QualifiedName != "placeOrder(shop.Cart)" || b.Name != "" || b.File != "src/shop/OrderService.java" || b.Line != 4 {
		t.Fatalf("brief: %+v", b)
	}
	// A name that is not the start of the qualified name is kept.
	if b := Briefly(graph.Node{ID: "x", Kind: "class", Name: "OrderService", QualifiedName: "shop.OrderService"}); b.Name != "OrderService" {
		t.Fatalf("class brief: %+v", b)
	}
	page, err := f.st.Neighbors(context.Background(), graph.NeighborQuery{RepositoryID: repo, NodeID: f.placeOrder.ID, Direction: graph.Both, EdgeKinds: []string{graph.EdgeCalls}})
	if err != nil {
		t.Fatal(err)
	}
	c := CompactNeighbors(page, f.placeOrder.ID)
	if len(c.Neighbors) != 3 {
		t.Fatalf("compact neighbors: %+v", c)
	}
	dirs := map[string]graph.Direction{}
	for _, n := range c.Neighbors {
		dirs[n.ID] = n.Direction
		if n.Via != graph.EdgeCalls || n.EdgeID == "" {
			t.Fatalf("neighbor: %+v", n)
		}
	}
	if dirs[f.checkout.ID] != graph.Incoming || dirs[f.reserve.ID] != graph.Outgoing || dirs[f.pay.ID] != graph.Outgoing {
		t.Fatalf("directions: %v", dirs)
	}
	// Compact is well under half the size even for these bare fixture nodes;
	// real nodes carry signatures, hashes and spans that briefs drop.
	full, _ := json.Marshal(page)
	small, _ := json.Marshal(c)
	if len(small)*2 > len(full) {
		t.Fatalf("compact page is %d bytes against %d", len(small), len(full))
	}
}

func TestImpactCountsEdgesAndCompacts(t *testing.T) {
	f := newFixture()
	// A second caller edge into placeOrder from checkout counts on the same hit.
	f.st.AddEdge(graph.EdgeReferences, f.checkout.ID, f.placeOrder.ID)
	res, err := Impact(context.Background(), f.st, ImpactRequest{RepositoryID: repo, NodeID: f.placeOrder.ID})
	if err != nil {
		t.Fatal(err)
	}
	// Level one reads four edges into the roots (two from checkout, one
	// from legacy, and placeOrder's own override of place); level two reads
	// the one into legacy.
	if res.Edges != 5 {
		t.Fatalf("edges followed: %+v", res)
	}
	for _, h := range res.Hits {
		if h.Node.Fact.Node.ID == f.checkout.ID && h.Edges != 2 {
			t.Fatalf("checkout reached by two edges: %+v", h)
		}
	}
	c := CompactImpact(res)
	if len(c.Nodes) != 3 || c.ByKind["method"] != 3 || c.Edges != 5 || c.Nodes[0].Via == "" {
		t.Fatalf("compact impact: %+v", c)
	}
}

func TestPath(t *testing.T) {
	f := newFixture()
	ctx := context.Background()
	// checkout -> placeOrder -> pay is a dependency chain of two hops.
	res, err := Path(ctx, f.st, PathRequest{RepositoryID: repo, FromID: f.checkout.ID, ToID: f.pay.ID})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Found || res.Hops != 2 || len(res.Nodes) != 3 || res.Nodes[0].ID != f.checkout.ID || res.Nodes[1].ID != f.placeOrder.ID || res.Nodes[2].ID != f.pay.ID || len(res.Edges) != 2 || res.Edges[0].Kind != graph.EdgeCalls {
		t.Fatalf("path: %+v", res)
	}
	// The reverse direction is not a dependency path, but is reachable when
	// direction is ignored.
	if res, err = Path(ctx, f.st, PathRequest{RepositoryID: repo, FromID: f.pay.ID, ToID: f.checkout.ID}); err != nil || res.Found {
		t.Fatalf("reverse dependency path: %+v %v", res, err)
	}
	if res, err = Path(ctx, f.st, PathRequest{RepositoryID: repo, FromID: f.pay.ID, ToID: f.checkout.ID, Direction: graph.Both}); err != nil || !res.Found || res.Hops != 2 {
		t.Fatalf("undirected path: %+v %v", res, err)
	}
	// The cron job reaches pay through legacy, place, placeOrder in four hops
	// (nightly -> legacyCheckout -> place <- placeOrder is not a dependency
	// chain), so with dependencies only it needs the override edge: nightly
	// -> legacyCheckout -> place; placeOrder -> place, never place -> pay.
	if res, err = Path(ctx, f.st, PathRequest{RepositoryID: repo, FromID: f.cron.ID, ToID: f.pay.ID}); err != nil || res.Found {
		t.Fatalf("no dependency path from nightly to pay: %+v %v", res, err)
	}
	if res, err = Path(ctx, f.st, PathRequest{RepositoryID: repo, FromID: f.cron.ID, ToID: f.pay.ID, Direction: graph.Both, MaxHops: 3}); err != nil || res.Found {
		t.Fatalf("four-hop undirected path must not be found within three: %+v %v", res, err)
	}
	if res, err = Path(ctx, f.st, PathRequest{RepositoryID: repo, FromID: f.cron.ID, ToID: f.pay.ID, Direction: graph.Both, MaxHops: 4}); err != nil || !res.Found || res.Hops != 4 {
		t.Fatalf("four-hop undirected path: %+v %v", res, err)
	}
	if res, err = Path(ctx, f.st, PathRequest{RepositoryID: repo, FromID: f.pay.ID, ToID: f.pay.ID}); err != nil || !res.Found || res.Hops != 0 || len(res.Nodes) != 1 {
		t.Fatalf("trivial path: %+v %v", res, err)
	}
	if _, err = Path(ctx, f.st, PathRequest{RepositoryID: repo, FromID: f.pay.ID, ToID: "missing"}); !errors.Is(err, graph.ErrNotFound) {
		t.Fatalf("missing end: %v", err)
	}
	if _, err = Path(ctx, f.st, PathRequest{RepositoryID: repo, FromID: f.pay.ID, ToID: f.checkout.ID, MaxHops: maxPathHops + 1}); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("hop bound: %v", err)
	}
}

func TestHubs(t *testing.T) {
	f := newFixture()
	res, err := Hubs(context.Background(), f.st, HubsRequest{RepositoryID: repo, Limit: 3})
	if err != nil {
		t.Fatal(err)
	}
	// place has two incoming semantic edges (placeOrder overrides it, legacy
	// calls it); every other node has one. Contains edges do not count.
	if len(res.Hubs) != 3 || res.Hubs[0].ID != f.place.ID || res.Hubs[0].InDegree != 2 || res.Hubs[1].InDegree != 1 || res.Hubs[2].InDegree != 1 {
		t.Fatalf("hubs: %+v", res.Hubs)
	}
	if res.Hubs[0].ByKind[graph.EdgeCalls] != 1 || res.Hubs[0].ByKind[graph.EdgeOverrides] != 1 {
		t.Fatalf("by kind: %+v", res.Hubs[0])
	}
	if res, err = Hubs(context.Background(), f.st, HubsRequest{RepositoryID: repo, Limit: 5, NodeKinds: []string{"class"}}); err != nil || len(res.Hubs) != 1 || res.Hubs[0].ID != f.orderService.ID {
		t.Fatalf("class hubs: %+v %v", res, err)
	}
	if _, err = Hubs(context.Background(), f.st, HubsRequest{RepositoryID: repo, Limit: maxHubs + 1}); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("limit bound: %v", err)
	}
}

func TestSearchExpansion(t *testing.T) {
	f := newFixture()
	ctx := context.Background()
	// Three top hits share the fragment "order"; a question with no exact
	// match is expanded with it and the second pass is lexical.
	f.st.Hits = []spannerstore.SearchHit{f.st.Hit(f.placeOrder.ID, 0.9, false), f.st.Hit(f.orderService.ID, 0.8, false), f.st.Hit(f.checkout.ID, 0.7, false)}
	res, err := Search(ctx, f.st, nil, SearchRequest{RepositoryIDs: []string{repo}, Text: "how does the shop place things", Limit: 5, Expand: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Expanded) == 0 || res.Expanded[0] != "order" {
		t.Fatalf("expansion: %+v", res.Expanded)
	}
	if n := len(f.st.Searches); n != 2 || f.st.Searches[1].Mode != spannerstore.SearchLexical || !strings.Contains(f.st.Searches[1].Text, "order") || f.st.Searches[1].Vector != nil {
		t.Fatalf("second pass: %+v", f.st.Searches)
	}
	if len(res.Hits) != 3 {
		t.Fatalf("fused hits: %d", len(res.Hits))
	}
	// An exact match suppresses expansion; a short query too.
	f.st.Searches = nil
	f.st.Hits[0].ExactMatch = true
	if res, err = Search(ctx, f.st, nil, SearchRequest{RepositoryIDs: []string{repo}, Text: "how does the shop place things", Expand: true}); err != nil || len(res.Expanded) != 0 || len(f.st.Searches) != 1 {
		t.Fatalf("exact match must not expand: %+v %v", res.Expanded, err)
	}
	f.st.Hits[0].ExactMatch = false
	f.st.Searches = nil
	if res, err = Search(ctx, f.st, nil, SearchRequest{RepositoryIDs: []string{repo}, Text: "placeOrder", Expand: true}); err != nil || len(res.Expanded) != 0 || len(f.st.Searches) != 1 {
		t.Fatalf("identifier query must not expand: %+v %v", res.Expanded, err)
	}
	// Compact hits keep the ranking signals and drop the record.
	c := CompactHits(res.Hits)
	if len(c) != 3 || c[0].ID != f.placeOrder.ID || c[0].Score != 0.9 || c[0].File == "" {
		t.Fatalf("compact hits: %+v", c)
	}
	if _, err = Search(ctx, f.st, nil, SearchRequest{RepositoryIDs: []string{repo}, Text: "  "}); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("empty query: %v", err)
	}
}

func TestCompactExplore(t *testing.T) {
	f := newFixture()
	f.st.Hits = []spannerstore.SearchHit{f.st.Hit(f.placeOrder.ID, 1, true)}
	res, err := Explore(context.Background(), f.st, nil, ExploreRequest{RepositoryIDs: []string{repo}, Question: "who calls placeOrder?"})
	if err != nil {
		t.Fatal(err)
	}
	c := CompactExplore(res)
	if len(c.Seeds) != 1 || c.Seeds[0].ID != f.placeOrder.ID || !c.Seeds[0].Expanded || len(c.Related) != len(res.Related) || c.Files == nil {
		t.Fatalf("compact explore: %+v", c)
	}
	full, _ := json.Marshal(res)
	small, _ := json.Marshal(c)
	if len(small) >= len(full) {
		t.Fatalf("compact explore is %d bytes against %d", len(small), len(full))
	}
}
