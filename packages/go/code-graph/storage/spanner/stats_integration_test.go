//go:build integration

package spannerstore

import (
	"context"
	"reflect"
	"testing"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/graph"
)

func TestRepositoryStats(t *testing.T) {
	ctx := context.Background()
	repo := "stats-" + t.Name()
	const model, dims = "test-embed", testVectorLength
	_, err := store.Stats(ctx, "stats-unknown", model, dims)
	wantErr(t, err, graph.ErrNotFound, "unknown repository")
	_, err = store.Stats(ctx, "", model, dims)
	wantErr(t, err, graph.ErrInvalid, "invalid repository id")
	_, err = store.Stats(ctx, repo, model, 0)
	wantErr(t, err, graph.ErrInvalid, "a model without dimensions")

	// Registered, and loading, but nothing published: every count is zero.
	register(t, repo)
	zero := func(what string) {
		t.Helper()
		st, err := store.Stats(ctx, repo, model, dims)
		if err != nil || st.RepositoryID != repo || st.Generation != 0 || st.CommitSHA != "" || st.RunID != "" || st.Nodes+st.Edges+st.Searchable != 0 ||
			st.NodeKinds == nil || st.EdgeKinds == nil || len(st.NodeKinds)+len(st.EdgeKinds) != 0 || st.Embeddings != (EmbeddingStats{Model: model, Dimensions: dims}) {
			t.Fatalf("%s: %+v %v", what, st, err)
		}
	}
	zero("registered")
	order := testNode("class", "OrderService", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/order/OrderService.java"), "source_text": graph.StringValue("class OrderService { void placeOrder() {} }")})
	place := testNode("method", "OrderService.placeOrder", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/order/OrderService.java"), "source_text": graph.StringValue("void placeOrder() { inventory.reserve(); }")})
	reserve := testNode("method", "InventoryService.reserve", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/inventory/InventoryService.java"), "source_text": graph.StringValue("void reserve() { stock--; }")})
	list := testNode(graph.NodeExternalSymbol, "java.util.List", nil)
	run1, lease1 := startGeneration(t, repo, 1, 1)
	load(t, lease1, 1, add(order, ""), add(place, ""), add(reserve, ""), add(list, ""),
		add(testEdge(graph.EdgeContains, order.Node.ID, place.Node.ID), ""), add(testEdge(graph.EdgeCalls, place.Node.ID, reserve.Node.ID), ""))
	zero("loaded, not published")
	publish(t, lease1, run1)

	// Two of the three searchable nodes get vectors.
	page, err := store.SearchDocuments(ctx, repo, model, dims, "")
	if err != nil || len(page.Documents) != 3 {
		t.Fatalf("documents to embed: %+v %v", page, err)
	}
	var rows []codesearch.Embedding
	embedded := map[string]bool{}
	for i, d := range page.Documents[:2] {
		rows = append(rows, codesearch.Embedding{Document: d, Vector: unit(i, dims)})
		embedded[d.NodeID] = true
	}
	if err = store.PutEmbeddings(ctx, repo, model, 1, rows); err != nil {
		t.Fatalf("put embeddings: %v", err)
	}
	st, err := store.Stats(ctx, repo, model, dims)
	if err != nil || st.Branch != "main" || st.Generation != 1 || st.CommitSHA != testCommit1 || st.RunID != run1.Key.RunID ||
		st.Nodes != 4 || st.Edges != 2 || st.Searchable != 3 || st.Embeddings != (EmbeddingStats{Model: model, Dimensions: dims, Current: 2, Stored: 2}) {
		t.Fatalf("stats at generation 1: %+v %v", st, err)
	}
	if want := map[string]uint64{"class": 1, "method": 2, graph.NodeExternalSymbol: 1}; !reflect.DeepEqual(st.NodeKinds, want) {
		t.Fatalf("node kinds %v, want %v", st.NodeKinds, want)
	}
	if want := map[string]uint64{graph.EdgeContains: 1, graph.EdgeCalls: 1}; !reflect.DeepEqual(st.EdgeKinds, want) {
		t.Fatalf("edge kinds %v, want %v", st.EdgeKinds, want)
	}
	// Another model has nothing stored; with embeddings off none are counted.
	if st, err = store.Stats(ctx, repo, "other-embed", dims); err != nil || st.Searchable != 3 || st.Embeddings != (EmbeddingStats{Model: "other-embed", Dimensions: dims}) {
		t.Fatalf("stats for another model: %+v %v", st, err)
	}
	if st, err = store.Stats(ctx, repo, "", 0); err != nil || st.Searchable != 3 || st.Nodes != 4 || st.Embeddings != (EmbeddingStats{}) {
		t.Fatalf("stats without embeddings: %+v %v", st, err)
	}

	// Generation 2 changes the text of an embedded node: its vector is still
	// stored but no longer current.
	changed := place
	if !embedded[place.Node.ID] {
		changed = order
	}
	before := mustNode(t, repo, changed.Node.ID, 0)
	node := *changed.Node
	changed.Node = &node
	changed.Node.Properties = map[string]graph.PropertyValue{"file_path": graph.StringValue("src/order/OrderService.java"), "source_text": graph.StringValue("// rewritten")}
	run2, lease2 := startGeneration(t, repo, 2, 2)
	load(t, lease2, 2, update(before, changed))
	publish(t, lease2, run2)
	st, err = store.Stats(ctx, repo, model, dims)
	if err != nil || st.Generation != 2 || st.CommitSHA != testCommit2 || st.RunID != run2.Key.RunID || st.Nodes != 4 || st.Edges != 2 || st.Searchable != 3 ||
		st.Embeddings != (EmbeddingStats{Model: model, Dimensions: dims, Current: 1, Stored: 2}) {
		t.Fatalf("stats at generation 2: %+v %v", st, err)
	}
}
