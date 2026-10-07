//go:build integration

package spannerstore

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"testing"
	"time"

	database "cloud.google.com/go/spanner/admin/database/apiv1"
	"cloud.google.com/go/spanner/admin/database/apiv1/databasepb"

	"ei-aitiger-codegraph/pkg/graph"
)

func crossLinkIDs(links []graph.CrossLink) []string {
	ids := make([]string, 0, len(links))
	for _, l := range links {
		ids = append(ids, l.ID)
	}
	return ids
}

func TestCrossLinks(t *testing.T) {
	ctx := context.Background()
	checkout, orders, billing := graph.ID("repo", t.Name(), "checkout"), graph.ID("repo", t.Name(), "orders"), graph.ID("repo", t.Name(), "billing")
	client := graph.CrossEnd{RepositoryID: checkout, NodeID: graph.ID("entity", "checkout", "client"), QualifiedName: "shop.OrdersClient.create(shop.Order)", Kind: "method"}
	handler := graph.CrossEnd{RepositoryID: orders, NodeID: graph.ID("entity", "orders", "handler"), QualifiedName: "orders.OrderController.create(orders.OrderRequest)", Kind: "method"}
	publisher := graph.CrossEnd{RepositoryID: orders, NodeID: graph.ID("entity", "orders", "publisher"), QualifiedName: "orders.Events.created(orders.Order)", Kind: "method"}
	listener := graph.CrossEnd{RepositoryID: billing, NodeID: graph.ID("entity", "billing", "listener"), QualifiedName: "billing.OrderListener.on(billing.OrderCreated)", Kind: "method"}
	team, other := "team:"+t.Name()+"-1", "team:"+t.Name()+"-2"
	link := func(owner, id, kind string, from, to graph.CrossEnd) graph.CrossLink {
		return graph.CrossLink{ID: id, Owner: owner, Kind: kind, Source: from, Target: to, Provenance: graph.CrossManual, Label: id}
	}
	calls := link(team, "calls", graph.CrossCallsAPI, client, handler)
	events := link(team, "events", graph.CrossSendsEvent, publisher, listener)
	theirs := link(other, "theirs", graph.CrossConnectsTo, client, handler)
	if err := store.ReplaceCrossLinks(ctx, team, []graph.CrossLink{calls, events}); err != nil {
		t.Fatalf("replace: %v", err)
	}
	if err := store.ReplaceCrossLinks(ctx, other, []graph.CrossLink{theirs}); err != nil {
		t.Fatalf("replace other: %v", err)
	}

	query := func(q graph.CrossLinkQuery) []string {
		t.Helper()
		links, err := store.CrossLinks(ctx, q)
		if err != nil {
			t.Fatalf("cross links %+v: %v", q, err)
		}
		return crossLinkIDs(links)
	}
	if got := query(graph.CrossLinkQuery{RepositoryID: orders, Direction: graph.Both}); !slices.Equal(got, []string{"calls", "events", "theirs"}) {
		t.Fatalf("both ways, every owner: %v", got)
	}
	if got := query(graph.CrossLinkQuery{RepositoryID: orders, Direction: graph.Incoming}); !slices.Equal(got, []string{"calls", "theirs"}) {
		t.Fatalf("into orders: %v", got)
	}
	if got := query(graph.CrossLinkQuery{RepositoryID: orders, Direction: graph.Outgoing, NodeIDs: []string{publisher.NodeID}}); !slices.Equal(got, []string{"events"}) {
		t.Fatalf("out of the publisher: %v", got)
	}
	// A node the graph renumbered is found by its qualified name.
	renumbered := graph.ID("entity", "orders", "handler-2")
	if got := query(graph.CrossLinkQuery{RepositoryID: orders, Direction: graph.Incoming, NodeIDs: []string{renumbered}, QualifiedNames: []string{handler.QualifiedName}}); !slices.Equal(got, []string{"calls", "theirs"}) {
		t.Fatalf("by qualified name: %v", got)
	}
	if got := query(graph.CrossLinkQuery{RepositoryID: orders, Direction: graph.Incoming, NodeIDs: []string{renumbered}}); len(got) != 0 {
		t.Fatalf("an unknown node: %v", got)
	}
	links, err := store.CrossLinks(ctx, graph.CrossLinkQuery{RepositoryID: billing, Direction: graph.Incoming})
	if err != nil || len(links) != 1 || links[0] != events {
		t.Fatalf("round trip: %+v %v", links, err)
	}

	// Replacing is the whole set: what isn't listed goes, other owners stay.
	if err := store.ReplaceCrossLinks(ctx, team, []graph.CrossLink{events}); err != nil {
		t.Fatalf("replace again: %v", err)
	}
	if got := query(graph.CrossLinkQuery{RepositoryID: orders, Direction: graph.Incoming}); !slices.Equal(got, []string{"theirs"}) {
		t.Fatalf("after replacing: %v", got)
	}
	owned, err := store.OwnedCrossLinks(ctx, team)
	if err != nil || !slices.Equal(crossLinkIDs(owned), []string{"events"}) {
		t.Fatalf("owned: %v %v", crossLinkIDs(owned), err)
	}
	if err := store.ReplaceCrossLinks(ctx, team, nil); err != nil {
		t.Fatalf("clear: %v", err)
	}
	if owned, _ := store.OwnedCrossLinks(ctx, team); len(owned) != 0 {
		t.Fatalf("cleared: %v", crossLinkIDs(owned))
	}

	for name, links := range map[string][]graph.CrossLink{
		"another owner's link": {theirs},
		"twice":                {events, events},
		"invalid":              {link(team, "self", graph.CrossCallsAPI, client, client)},
	} {
		if err := store.ReplaceCrossLinks(ctx, team, links); !errors.Is(err, graph.ErrInvalid) {
			t.Errorf("%s: got %v, want ErrInvalid", name, err)
		}
	}
}

// A database made before cross-repository links has no CGCrossLinks table:
// reads find no links rather than failing every traversal.
func TestCrossLinksWithoutTheTable(t *testing.T) {
	ctx := context.Background()
	db := fmt.Sprintf("projects/codegraph-test/instances/test/databases/cg-old-%d", time.Now().UnixNano())
	if err := ProvisionEmulator(ctx, db, testVectorLength); err != nil {
		t.Fatalf("provision: %v", err)
	}
	admin, err := database.NewDatabaseAdminClient(ctx)
	if err != nil {
		t.Fatalf("admin client: %v", err)
	}
	defer admin.Close()
	op, err := admin.UpdateDatabaseDdl(ctx, &databasepb.UpdateDatabaseDdlRequest{Database: db, Statements: []string{"DROP INDEX CGCrossLinksBySource", "DROP INDEX CGCrossLinksByTarget", "DROP TABLE CGCrossLinks"}})
	if err == nil {
		err = op.Wait(ctx)
	}
	if err != nil {
		t.Fatalf("drop: %v", err)
	}
	old, err := New(ctx, Config{Database: db, Scope: "test", CursorSigningKey: []byte("0123456789abcdef0123456789abcdef"), VectorLength: testVectorLength})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	defer old.Close()
	links, err := old.CrossLinks(ctx, graph.CrossLinkQuery{RepositoryID: graph.ID("repo", "any"), Direction: graph.Both, NodeIDs: []string{graph.ID("entity", "any")}})
	if err != nil || links != nil {
		t.Fatalf("no table, no links: %v %v", links, err)
	}
	if owned, err := old.OwnedCrossLinks(ctx, "team:any"); err != nil || owned != nil {
		t.Fatalf("no table, none owned: %v %v", owned, err)
	}
	// Re-provisioning adds just what's missing.
	if err := ProvisionEmulator(ctx, db, testVectorLength); err != nil {
		t.Fatalf("re-provision: %v", err)
	}
	if links, err := old.CrossLinks(ctx, graph.CrossLinkQuery{RepositoryID: graph.ID("repo", "any"), Direction: graph.Both}); err != nil || len(links) != 0 {
		t.Fatalf("after adding the table: %v %v", links, err)
	}
}
