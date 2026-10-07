package agentquery

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"testing"

	"ei-aitiger-codegraph/agentquery/memstore"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

const repo = "acme-shop"

// fixture is a small service graph: a controller calls OrderService.placeOrder,
// which calls into inventory and payment; placeOrder overrides OrderPort.place,
// which a legacy caller still calls through the interface.
type fixture struct {
	st                                                                    *memstore.Store
	orderService, placeOrder, reserve, pay, checkout, place, legacy, cron graph.Node
	source                                                                string
}

func newFixture() *fixture {
	st := memstore.New(repo)
	f := &fixture{st: st}
	f.source = "package shop;\n\npublic class OrderService implements OrderPort {\n    public Order placeOrder(Cart cart) {\n        inventory.reserve(cart);\n        return payments.pay(cart);\n    }\n}\n"
	sum := sha256.Sum256([]byte(f.source))
	sha := hex.EncodeToString(sum[:])
	st.Sources[sha] = []byte(f.source)
	node := func(kind, name, qualified, path string, props map[string]string, span *ir.Span) graph.Node {
		n := graph.Node{ID: graph.ID(kind, qualified), Kind: kind, Name: name, QualifiedName: qualified, Properties: map[string]graph.PropertyValue{"file_path": graph.StringValue(path), "language": graph.StringValue("java")}}
		for k, v := range props {
			n.Properties[k] = graph.StringValue(v)
		}
		if span != nil {
			n.Source = &graph.SourceAnchor{Lineage: graph.Lineage(repo, "m", "main", path), ContentSHA256: sha, Span: *span}
		}
		return st.AddNode(n)
	}
	placeStart := uint64(strings.Index(f.source, "public Order placeOrder"))
	placeEnd := uint64(strings.Index(f.source, "    }\n}\n") + len("    }"))
	f.orderService = node("class", "OrderService", "shop.OrderService", "src/shop/OrderService.java", map[string]string{"signature": "public class OrderService implements OrderPort", "docstring": "Places orders."}, &ir.Span{Start: ir.Position{ByteOffset: 0, Line: 1, Column: 1}, End: ir.Position{ByteOffset: uint64(len(f.source)), Line: 8, Column: 2}})
	f.placeOrder = node("method", "placeOrder", "placeOrder(shop.Cart)", "src/shop/OrderService.java", map[string]string{"signature": "public Order placeOrder(Cart cart)"}, &ir.Span{Start: ir.Position{ByteOffset: placeStart, Line: 4, Column: 5}, End: ir.Position{ByteOffset: placeEnd, Line: 7, Column: 6}})
	f.reserve = node("method", "reserve", "reserve(shop.Cart)", "src/shop/InventoryService.java", map[string]string{"signature": "void reserve(Cart cart)"}, nil)
	f.pay = node("method", "pay", "pay(shop.Cart)", "src/shop/PaymentService.java", map[string]string{"signature": "Order pay(Cart cart)"}, nil)
	f.checkout = node("method", "checkout", "checkout(shop.Cart)", "src/shop/CheckoutController.java", nil, nil)
	f.place = node("method", "place", "place(shop.Cart)", "src/shop/OrderPort.java", nil, nil)
	f.legacy = node("method", "legacyCheckout", "legacyCheckout(shop.Cart)", "src/legacy/LegacyCheckout.java", nil, nil)
	f.cron = node("method", "nightly", "nightly()", "src/legacy/Nightly.java", nil, nil)
	st.AddEdge(graph.EdgeContains, f.orderService.ID, f.placeOrder.ID)
	st.AddEdge(graph.EdgeCalls, f.placeOrder.ID, f.reserve.ID)
	st.AddEdge(graph.EdgeCalls, f.placeOrder.ID, f.pay.ID)
	st.AddEdge(graph.EdgeCalls, f.checkout.ID, f.placeOrder.ID)
	st.AddEdge(graph.EdgeUsesType, f.placeOrder.ID, f.orderService.ID)
	st.AddEdge(graph.EdgeOverrides, f.placeOrder.ID, f.place.ID)
	st.AddEdge(graph.EdgeCalls, f.legacy.ID, f.place.ID)
	st.AddEdge(graph.EdgeCalls, f.cron.ID, f.legacy.ID)
	st.Hits = []spannerstore.SearchHit{st.Hit(f.placeOrder.ID, 1.2, true), st.Hit(f.orderService.ID, 0.03, false)}
	return f
}

type fakeEmbedder struct{ calls int }

func (e *fakeEmbedder) Model() string      { return "fake" }
func (e *fakeEmbedder) Dimensions() int    { return 4 }
func (e *fakeEmbedder) MaxInputBytes() int { return 8000 }
func (e *fakeEmbedder) Embed(_ context.Context, texts []string) ([][]float64, error) {
	e.calls++
	out := make([][]float64, len(texts))
	for i := range texts {
		out[i] = []float64{1, 0, 0, 0}
	}
	return out, nil
}

func TestExplore(t *testing.T) {
	f := newFixture()
	ctx := context.Background()
	embedder := &fakeEmbedder{}
	res, err := Explore(ctx, f.st, embedder, ExploreRequest{RepositoryIDs: []string{repo}, Question: "who calls placeOrder in OrderService?", Expand: 1, NearPath: "src/shop/Cart.java"})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Semantic || embedder.calls != 1 || len(f.st.Searches) != 1 || f.st.Searches[0].Model != "fake" || f.st.Searches[0].Vector == nil || f.st.Searches[0].NearPath != "src/shop/Cart.java" || f.st.Searches[0].Limit != defaultSeeds {
		t.Fatalf("search request: %+v semantic=%v calls=%d", f.st.Searches, res.Semantic, embedder.calls)
	}
	if res.Query.Text != "who calls placeOrder in OrderService?" || !res.Query.Sentence || len(res.Query.Symbols) != 2 {
		t.Fatalf("parsed query: %+v", res.Query)
	}
	if len(res.Seeds) != 2 || res.Seeds[0].ID != f.placeOrder.ID || !res.Seeds[0].Expanded || res.Seeds[1].Expanded || res.Seeds[0].Signature != "public Order placeOrder(Cart cart)" || res.Seeds[0].StartLine != 4 || res.Seeds[0].ContentSHA256 == "" {
		t.Fatalf("seeds: %+v", res.Seeds)
	}
	if res.Seeds[1].Documentation != "Places orders." {
		t.Fatalf("documentation: %+v", res.Seeds[1])
	}
	// One hop from placeOrder: callees out, the caller in, the seed link to
	// OrderService kept as a link, and the containment edge ignored.
	want := map[string]graph.Direction{f.reserve.ID: graph.Outgoing, f.pay.ID: graph.Outgoing, f.checkout.ID: graph.Incoming, f.place.ID: graph.Outgoing}
	if len(res.Related) != len(want) {
		t.Fatalf("related: %+v", res.Related)
	}
	for _, r := range res.Related {
		if want[r.ID] != r.Direction || r.SeedID != f.placeOrder.ID || r.Via == "" || r.FilePath == "" {
			t.Fatalf("related entry: %+v", r)
		}
	}
	if len(res.Links) != 1 || res.Links[0].Via != graph.EdgeUsesType || res.Links[0].SourceID != f.placeOrder.ID || res.Links[0].TargetID != f.orderService.ID {
		t.Fatalf("links: %+v", res.Links)
	}
	if len(res.Files) != 5 || res.Files[0].FilePath != "src/shop/OrderService.java" || res.Files[0].Seeds != 2 || res.Files[0].Language != "java" || res.Files[1].Related != 1 {
		t.Fatalf("files: %+v", res.Files)
	}
	if res.Truncated {
		t.Fatalf("no truncation expected: %+v", res)
	}

	// Lexical mode never embeds; a page cap reports truncation.
	f.st.NeighborPage = 2
	res, err = Explore(ctx, f.st, embedder, ExploreRequest{RepositoryIDs: []string{repo}, Question: "placeOrder", Mode: spannerstore.SearchLexical, Expand: 1, Neighbors: 10})
	if err != nil {
		t.Fatal(err)
	}
	if res.Semantic || embedder.calls != 1 || !res.Truncated || len(res.Related)+len(res.Links) != 2 {
		t.Fatalf("lexical, truncated: semantic=%v calls=%d truncated=%v related=%d links=%d", res.Semantic, embedder.calls, res.Truncated, len(res.Related), len(res.Links))
	}

	// Zero limits take their defaults, so the one seed is expanded; without
	// an embedder the search stays lexical.
	f.st.NeighborPage = 0
	res, err = Explore(ctx, f.st, nil, ExploreRequest{RepositoryIDs: []string{repo}, Question: "placeOrder", Limit: 1})
	if err != nil {
		t.Fatal(err)
	}
	if res.Semantic || len(res.Seeds) != 1 || !res.Seeds[0].Expanded || len(res.Related) != 5 || len(res.Links) != 0 || f.st.Searches[len(f.st.Searches)-1].Vector != nil {
		t.Fatalf("defaults: %+v", res)
	}

	for _, bad := range []ExploreRequest{
		{Question: "x"},
		{RepositoryIDs: []string{repo}},
		{RepositoryIDs: []string{repo}, Question: strings.Repeat("q", maxQuestionBytes+1)},
		{RepositoryIDs: []string{repo}, Question: "x", Limit: maxSeeds + 1},
		{RepositoryIDs: []string{repo}, Question: "x", Expand: maxExpand + 1},
		{RepositoryIDs: []string{repo}, Question: "x", Neighbors: maxNeighbors + 1},
	} {
		if _, err := Explore(ctx, f.st, nil, bad); !errors.Is(err, deployment.ErrInvalidRequest) {
			t.Fatalf("%+v: got %v, want invalid request", bad, err)
		}
	}
}

func TestImpact(t *testing.T) {
	f := newFixture()
	ctx := context.Background()
	res, err := Impact(ctx, f.st, ImpactRequest{RepositoryID: repo, NodeID: f.placeOrder.ID})
	if err != nil {
		t.Fatal(err)
	}
	// The override's callers and the base method's callers are both impacted;
	// the second hop reaches the legacy caller's caller. Callees are not.
	if res.Root != f.placeOrder.ID || len(res.Roots) != 2 || res.Roots[1] != f.place.ID || res.Truncated {
		t.Fatalf("roots: %+v", res)
	}
	got := map[string]ImpactHit{}
	for _, h := range res.Hits {
		got[h.Node.Fact.Node.ID] = h
	}
	if len(got) != 3 || got[f.checkout.ID].Depth != 1 || got[f.checkout.ID].From != f.placeOrder.ID || got[f.legacy.ID].Depth != 1 || got[f.legacy.ID].From != f.place.ID || got[f.cron.ID].Depth != 2 || got[f.cron.ID].From != f.legacy.ID {
		t.Fatalf("hits: %+v", res.Hits)
	}
	if got[f.checkout.ID].Via != graph.EdgeCalls {
		t.Fatalf("via: %+v", got[f.checkout.ID])
	}
	// Depth one stops before the second hop; a limit truncates.
	if res, err = Impact(ctx, f.st, ImpactRequest{RepositoryID: repo, NodeID: f.placeOrder.ID, Depth: 1}); err != nil || len(res.Hits) != 2 || res.Truncated {
		t.Fatalf("depth 1: %+v %v", res, err)
	}
	if res, err = Impact(ctx, f.st, ImpactRequest{RepositoryID: repo, NodeID: f.placeOrder.ID, Limit: 1}); err != nil || len(res.Hits) != 1 || !res.Truncated {
		t.Fatalf("limit 1: %+v %v", res, err)
	}
	if _, err = Impact(ctx, f.st, ImpactRequest{RepositoryID: repo, NodeID: "missing"}); !errors.Is(err, graph.ErrNotFound) {
		t.Fatalf("missing root: %v", err)
	}
	if _, err = Impact(ctx, f.st, ImpactRequest{RepositoryID: repo, NodeID: f.placeOrder.ID, Depth: maxImpactDepth + 1}); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("depth bound: %v", err)
	}
}

func TestNodeSource(t *testing.T) {
	f := newFixture()
	ctx := context.Background()
	res, err := NodeSource(ctx, f.st, SourceRequest{RepositoryID: repo, NodeID: f.placeOrder.ID})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(res.Text, "public Order placeOrder(Cart cart) {") || !strings.HasSuffix(res.Text, "    }") || res.StartLine != 4 || res.EndLine != 7 || res.Truncated {
		t.Fatalf("exact span: %+v", res)
	}
	res, err = NodeSource(ctx, f.st, SourceRequest{RepositoryID: repo, NodeID: f.placeOrder.ID, ContextLines: 1})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(res.Text, "public class OrderService implements OrderPort {\n    public Order placeOrder") || !strings.HasSuffix(res.Text, "    }\n}\n") || res.StartLine != 3 || res.EndLine != 8 || res.ByteStart != uint64(strings.Index(f.source, "public class")) {
		t.Fatalf("with context: %+v", res)
	}
	if res, err = NodeSource(ctx, f.st, SourceRequest{RepositoryID: repo, NodeID: f.placeOrder.ID, ContextLines: 50}); err != nil || res.Text != f.source || res.StartLine != 1 || res.EndLine != 8 {
		t.Fatalf("context beyond the file: %+v %v", res, err)
	}
	if _, err = NodeSource(ctx, f.st, SourceRequest{RepositoryID: repo, NodeID: f.reserve.ID}); !errors.Is(err, graph.ErrNotFound) {
		t.Fatalf("no anchor: %v", err)
	}
	if _, err = NodeSource(ctx, f.st, SourceRequest{RepositoryID: repo, NodeID: f.placeOrder.ID, ContextLines: maxContextLines + 1}); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("context bound: %v", err)
	}

	// A span past the retained bytes is an integrity failure, a huge span is cut.
	big := strings.Repeat("x", maxSourceBytes+10) + "\n"
	sum := sha256.Sum256([]byte(big))
	sha := hex.EncodeToString(sum[:])
	f.st.Sources[sha] = []byte(big)
	huge := f.st.AddNode(graph.Node{ID: "huge", Kind: "method", Name: "huge", Source: &graph.SourceAnchor{ContentSHA256: sha, Span: ir.Span{Start: ir.Position{Line: 1}, End: ir.Position{ByteOffset: uint64(len(big)), Line: 1}}}})
	if res, err = NodeSource(ctx, f.st, SourceRequest{RepositoryID: repo, NodeID: huge.ID}); err != nil || !res.Truncated || len(res.Text) != maxSourceBytes {
		t.Fatalf("huge span: truncated=%v len=%d err=%v", res.Truncated, len(res.Text), err)
	}
	broken := f.st.AddNode(graph.Node{ID: "broken", Kind: "method", Name: "broken", Source: &graph.SourceAnchor{ContentSHA256: sha, Span: ir.Span{End: ir.Position{ByteOffset: uint64(len(big)) + 1}}}})
	if _, err = NodeSource(ctx, f.st, SourceRequest{RepositoryID: repo, NodeID: broken.ID}); !errors.Is(err, graph.ErrIntegrity) {
		t.Fatalf("broken span: %v", err)
	}
}
