package agentquery

import (
	"context"
	"slices"
	"testing"

	"ei-aitiger-codegraph/agentquery/memstore"
	"ei-aitiger-codegraph/pkg/graph"
)

// services are three repositories linked the way services are: checkout's
// OrdersClient.create calls the orders API that OrderController.create
// serves, and orders publishes an event (Events.created) that billing's
// OrderListener.on consumes.
type services struct {
	st                        *memstore.Store
	checkout, orders, billing string
	placeOrder, client        graph.Node // checkout
	handler, save, created    graph.Node // orders
	listener                  graph.Node // billing
	callsAPI, sendsEvent      graph.CrossLink
}

func newServices() *services {
	s := &services{checkout: graph.ID("repo", "checkout"), orders: graph.ID("repo", "orders"), billing: graph.ID("repo", "billing")}
	s.st = memstore.New(s.checkout)
	orders, billing := s.st.Add(s.orders), s.st.Add(s.billing)
	method := func(st *memstore.Store, qualified string) graph.Node {
		return st.AddNode(graph.Node{ID: graph.ID("entity", st.Repo, qualified), Kind: "method", Name: qualified, QualifiedName: qualified, Properties: map[string]graph.PropertyValue{"file_path": graph.StringValue(qualified + ".java")}})
	}
	s.placeOrder, s.client = method(s.st, "shop.CheckoutService.placeOrder()"), method(s.st, "shop.OrdersClient.create(shop.Order)")
	s.handler, s.save, s.created = method(orders, "orders.OrderController.create(orders.OrderRequest)"), method(orders, "orders.OrderService.save(orders.Order)"), method(orders, "orders.Events.created(orders.Order)")
	s.listener = method(billing, "billing.OrderListener.on(billing.OrderCreated)")
	s.st.AddEdge(graph.EdgeCalls, s.placeOrder.ID, s.client.ID)
	orders.AddEdge(graph.EdgeCalls, s.handler.ID, s.save.ID)
	orders.AddEdge(graph.EdgeCalls, s.save.ID, s.created.ID)
	s.callsAPI = s.st.Link("calls", graph.CrossCallsAPI, s.checkout, s.client.ID, s.orders, s.handler.ID)
	s.sendsEvent = s.st.Link("events", graph.CrossSendsEvent, s.orders, s.created.ID, s.billing, s.listener.ID)
	return s
}

func everyone(string) bool { return true }

func hopTargets(hops []CrossHop) []string {
	var out []string
	for _, h := range hops {
		if h.Node != nil {
			out = append(out, string(h.Direction)+" "+h.Node.Fact.Node.QualifiedName)
		} else {
			out = append(out, string(h.Direction)+" stale")
		}
	}
	return out
}

func TestCrossHops(t *testing.T) {
	ctx := context.Background()
	s := newServices()
	out, err := CrossHops(ctx, s.st, s.checkout, s.client, graph.Outgoing, everyone)
	if err != nil || !slices.Equal(hopTargets(out), []string{"out " + s.handler.QualifiedName}) || out[0].RepositoryID != s.orders {
		t.Fatalf("the client calls the handler in orders: %v %v", hopTargets(out), err)
	}
	in, err := CrossHops(ctx, s.st, s.orders, s.handler, graph.Both, everyone)
	if err != nil || !slices.Equal(hopTargets(in), []string{"in " + s.client.QualifiedName}) {
		t.Fatalf("the handler is called from checkout: %v %v", hopTargets(in), err)
	}
	if none, _ := CrossHops(ctx, s.st, s.orders, s.handler, graph.Outgoing, everyone); len(none) != 0 {
		t.Fatalf("the handler calls nothing across: %v", hopTargets(none))
	}
	// Repositories the caller can't read aren't there.
	hidden, _ := CrossHops(ctx, s.st, s.orders, s.handler, graph.Incoming, func(repo string) bool { return repo != s.checkout })
	if len(hidden) != 0 {
		t.Fatalf("checkout is hidden: %v", hopTargets(hidden))
	}

	// Re-ingested without a baseline, the handler has a new ID: the link
	// finds it by its qualified name, both ways.
	orders := s.st.Others[s.orders]
	delete(orders.Nodes, s.handler.ID)
	renumbered := s.handler
	renumbered.ID = graph.ID("entity", "orders", "handler-2")
	orders.AddNode(renumbered)
	out, _ = CrossHops(ctx, s.st, s.checkout, s.client, graph.Outgoing, everyone)
	if len(out) != 1 || out[0].Node == nil || out[0].Node.Fact.Node.ID != renumbered.ID {
		t.Fatalf("followed to the renumbered handler: %v", hopTargets(out))
	}
	in, _ = CrossHops(ctx, s.st, s.orders, renumbered, graph.Incoming, everyone)
	if !slices.Equal(hopTargets(in), []string{"in " + s.client.QualifiedName}) {
		t.Fatalf("followed back from the renumbered handler: %v", hopTargets(in))
	}

	// Gone altogether, the far end is stale, and keeps what the link recorded.
	delete(orders.Nodes, renumbered.ID)
	out, _ = CrossHops(ctx, s.st, s.checkout, s.client, graph.Outgoing, everyone)
	compact := CompactCrossHops(out)
	if len(out) != 1 || !out[0].Stale || compact[0].QualifiedName != s.handler.QualifiedName || compact[0].Via != graph.CrossCallsAPI || compact[0].RepositoryID != s.orders {
		t.Fatalf("stale: %+v", compact)
	}
}

func acrossNames(across []ImpactAcross) []string {
	var out []string
	for _, a := range across {
		out = append(out, a.Node.Fact.Node.QualifiedName)
		for _, h := range a.Hits {
			out = append(out, h.Node.Fact.Node.QualifiedName)
		}
	}
	return out
}

func TestImpactAcrossRepositories(t *testing.T) {
	ctx := context.Background()
	s := newServices()
	// Changing billing's listener reaches orders through the event it
	// consumes, then checkout through the API the publisher's callers serve.
	res, err := Impact(ctx, s.st, ImpactRequest{RepositoryID: s.billing, NodeID: s.listener.ID, Change: ChangeBody, Depth: 6, Across: everyone})
	if err != nil {
		t.Fatalf("impact: %v", err)
	}
	want := []string{s.created.QualifiedName, s.save.QualifiedName, s.handler.QualifiedName, s.client.QualifiedName, s.placeOrder.QualifiedName}
	if got := acrossNames(res.Across); !slices.Equal(got, want) {
		t.Fatalf("across: %v, want %v", got, want)
	}
	intoOrders, intoCheckout := res.Across[0], res.Across[1]
	if intoOrders.RepositoryID != s.orders || intoOrders.FromRepositoryID != s.billing || intoOrders.From != s.listener.ID || intoOrders.Depth != 1 || intoOrders.Link.ID != "events" {
		t.Fatalf("into orders: %+v", intoOrders)
	}
	if depths := []int{intoOrders.Hits[0].Depth, intoOrders.Hits[1].Depth}; !slices.Equal(depths, []int{2, 3}) {
		t.Fatalf("depths count from the root: %v", depths)
	}
	if intoCheckout.RepositoryID != s.checkout || intoCheckout.From != s.handler.ID || intoCheckout.Depth != 4 || intoCheckout.Hits[0].Depth != 5 {
		t.Fatalf("into checkout: %+v", intoCheckout)
	}
	compact := CompactImpact(res)
	if len(compact.Across) != 2 || compact.Across[1].Node.QualifiedName != s.client.QualifiedName || compact.Across[1].Via != graph.CrossCallsAPI || len(compact.Across[1].Nodes) != 1 {
		t.Fatalf("compact: %+v", compact.Across)
	}

	// The depth bounds the walk across as within: three hops end at the
	// handler, before the API call into checkout.
	res, _ = Impact(ctx, s.st, ImpactRequest{RepositoryID: s.billing, NodeID: s.listener.ID, Change: ChangeBody, Depth: 3, Across: everyone})
	if got := acrossNames(res.Across); !slices.Equal(got, want[:3]) {
		t.Fatalf("depth 3: %v", got)
	}
	// Only readable repositories, and only when asked.
	res, _ = Impact(ctx, s.st, ImpactRequest{RepositoryID: s.billing, NodeID: s.listener.ID, Change: ChangeBody, Depth: 6, Across: func(repo string) bool { return repo != s.checkout }})
	if got := acrossNames(res.Across); !slices.Equal(got, want[:3]) {
		t.Fatalf("checkout unreadable: %v", got)
	}
	res, _ = Impact(ctx, s.st, ImpactRequest{RepositoryID: s.billing, NodeID: s.listener.ID, Change: ChangeBody, Depth: 6})
	if res.Across != nil {
		t.Fatalf("not asked: %v", acrossNames(res.Across))
	}
	// The limit counts what's reached across.
	res, _ = Impact(ctx, s.st, ImpactRequest{RepositoryID: s.billing, NodeID: s.listener.ID, Change: ChangeBody, Depth: 6, Limit: 2, Across: everyone})
	if got := acrossNames(res.Across); len(got) != 2 || !res.Truncated {
		t.Fatalf("limit 2: %v truncated=%v", got, res.Truncated)
	}
}
