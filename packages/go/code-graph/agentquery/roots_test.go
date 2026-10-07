package agentquery

import (
	"context"
	"errors"
	"testing"

	"ei-aitiger-codegraph/pkg/deployment"
)

func TestRootsImpact(t *testing.T) {
	f := newFixture()
	test := withBuild(f)
	ctx := context.Background()
	res, err := RootsImpact(ctx, f.st, RootsImpactRequest{RepositoryID: repo, Roots: []Root{
		{ID: f.placeOrder.ID, Change: ChangeBody},
		{ID: f.orderService.ID, Change: ChangeSignature},
		{ID: f.placeOrder.ID, Change: ChangeBody},
	}})
	if err != nil {
		t.Fatal(err)
	}
	got := hitIDs(res.Hits)
	// Each root once; the widest kind is walked first and named first.
	if len(res.Roots) != 2 || res.Change != "signature,body" {
		t.Fatalf("roots %v change %q", res.Roots, res.Change)
	}
	// The body change reaches placeOrder's callers, and through what it
	// implements (place), the legacy callers, as Impact does.
	for _, want := range []string{f.checkout.ID, f.legacy.ID, f.cron.ID, test.ID} {
		if _, ok := got[want]; !ok {
			t.Errorf("missing %s: %+v", want, res.Hits)
		}
	}
	// Roots are never hits, even when another root depends on them.
	if _, ok := got[f.placeOrder.ID]; ok {
		t.Error("a root reported as a hit")
	}
	if res.Assessment == nil || len(res.Assessment.Tests) != 1 {
		t.Fatalf("assessment: %+v", res.Assessment)
	}

	if _, err = RootsImpact(ctx, f.st, RootsImpactRequest{RepositoryID: repo, Roots: []Root{{ID: f.place.ID, Change: "rename"}}}); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("unknown change: %v", err)
	}
	if _, err = RootsImpact(ctx, f.st, RootsImpactRequest{RepositoryID: repo, Roots: make([]Root, maxRoots+1)}); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("too many roots: %v", err)
	}
	// No roots: nothing impacted.
	if res, err = RootsImpact(ctx, f.st, RootsImpactRequest{RepositoryID: repo}); err != nil || len(res.Hits) != 0 || len(res.Roots) != 0 {
		t.Fatalf("no roots: %+v %v", res, err)
	}
}

func TestFileDeclarations(t *testing.T) {
	f := newFixture()
	withBuild(f)
	got, err := FileDeclarations(context.Background(), f.st, repo, "src/shop/OrderService.java", 0)
	if err != nil {
		t.Fatal(err)
	}
	ids := map[string]bool{}
	for _, v := range got {
		ids[v.Fact.Node.ID] = true
		if v.Fact.Node.Source == nil {
			t.Errorf("%s has no span", v.Fact.Node.ID)
		}
	}
	// The file's declarations, with spans; not the file node itself.
	if len(ids) != 2 || !ids[f.orderService.ID] || !ids[f.placeOrder.ID] {
		t.Fatalf("declarations: %v", ids)
	}
	if got, err = FileDeclarations(context.Background(), f.st, repo, "src/nowhere.java", 0); err != nil || len(got) != 0 {
		t.Fatalf("unknown file: %v %v", got, err)
	}
}
