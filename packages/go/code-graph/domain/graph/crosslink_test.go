package graph

import (
	"errors"
	"testing"
)

func crossLink() CrossLink {
	return CrossLink{
		ID:         "0b6e3c52-7d1f-4f2a-9c1e-5a3b2d4e6f70",
		Owner:      "team:7c843a95-c1a4-4b53-a70d-2eb5627289f1",
		Kind:       CrossCallsAPI,
		Source:     CrossEnd{RepositoryID: ID("repo", "a"), NodeID: ID("entity", "a", "client"), QualifiedName: "shop.OrdersClient.create(shop.Order)", Kind: "method"},
		Target:     CrossEnd{RepositoryID: ID("repo", "b"), NodeID: ID("entity", "b", "handler"), QualifiedName: "orders.OrderController.create(orders.OrderRequest)", Kind: "method"},
		Label:      "POST /v1/orders",
		Provenance: CrossManual,
	}
}

func TestCrossLinkValidate(t *testing.T) {
	if err := crossLink().Validate(); err != nil {
		t.Fatalf("valid link: %v", err)
	}
	for name, change := range map[string]func(*CrossLink){
		"no id":           func(l *CrossLink) { l.ID = "" },
		"spaced owner":    func(l *CrossLink) { l.Owner = "team one" },
		"unknown kind":    func(l *CrossLink) { l.Kind = "teleports" },
		"bad node":        func(l *CrossLink) { l.Target.NodeID = "handler" },
		"same repository": func(l *CrossLink) { l.Target.RepositoryID = l.Source.RepositoryID },
		"no provenance":   func(l *CrossLink) { l.Provenance = "" },
		"long label":      func(l *CrossLink) { l.Label = string(make([]byte, 501)) },
	} {
		l := crossLink()
		change(&l)
		if err := l.Validate(); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: got %v, want ErrInvalid", name, err)
		}
	}
}

func TestCrossLinkQueryMatches(t *testing.T) {
	l := crossLink()
	target := CrossLinkQuery{RepositoryID: l.Target.RepositoryID, Direction: Incoming}
	if !target.Matches(l, Incoming) || target.Matches(l, Outgoing) {
		t.Fatal("a query without nodes matches every link on its side, and only there")
	}
	// By ID, or by qualified name when an ingestion renumbered the node.
	byID := CrossLinkQuery{RepositoryID: l.Target.RepositoryID, NodeIDs: []string{l.Target.NodeID}}
	byName := CrossLinkQuery{RepositoryID: l.Target.RepositoryID, NodeIDs: []string{ID("entity", "b", "new")}, QualifiedNames: []string{l.Target.QualifiedName}}
	other := CrossLinkQuery{RepositoryID: l.Target.RepositoryID, NodeIDs: []string{ID("entity", "b", "new")}, QualifiedNames: []string{"orders.Other"}}
	if !byID.Matches(l, Incoming) || !byName.Matches(l, Incoming) || other.Matches(l, Incoming) {
		t.Fatal("matching by node id or qualified name")
	}
	if l.Far(Incoming) != l.Source || l.Far(Outgoing) != l.Target || l.End(Incoming) != l.Target {
		t.Fatal("ends")
	}
	if err := (CrossLinkQuery{RepositoryID: l.Source.RepositoryID, Direction: "sideways"}).Validate(); !errors.Is(err, ErrInvalid) {
		t.Fatalf("direction: %v", err)
	}
}
