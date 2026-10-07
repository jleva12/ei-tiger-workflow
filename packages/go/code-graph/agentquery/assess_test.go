package agentquery

import (
	"context"
	"errors"
	"testing"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

// withBuild adds the build context the assessment reads: a source_file node
// per lineage with its Maven source set, and one test method that calls
// placeOrder from a test source root.
func withBuild(f *fixture) graph.Node {
	mainLineage := graph.Lineage(repo, "m", "main", "src/shop/OrderService.java")
	testLineage := graph.Lineage(repo, "m", "test", "src/test/java/shop/OrderServiceTest.java")
	f.st.AddNode(graph.Node{ID: mainLineage, Kind: graph.NodeSourceFile, Name: "OrderService.java", Properties: map[string]graph.PropertyValue{"file_path": graph.StringValue("src/shop/OrderService.java"), "language": graph.StringValue("java"), "module_id": graph.StringValue("m"), "source_set_id": graph.StringValue("m:main")}})
	f.st.AddNode(graph.Node{ID: testLineage, Kind: graph.NodeSourceFile, Name: "OrderServiceTest.java", Properties: map[string]graph.PropertyValue{"file_path": graph.StringValue("src/test/java/shop/OrderServiceTest.java"), "language": graph.StringValue("java"), "module_id": graph.StringValue("m"), "source_set_id": graph.StringValue("m:test")}})
	test := f.st.AddNode(graph.Node{ID: graph.ID("method", "testPlaceOrder()"), Kind: "method", Name: "testPlaceOrder", QualifiedName: "testPlaceOrder()", Source: &graph.SourceAnchor{Lineage: testLineage, ContentSHA256: "", Span: ir.Span{Start: ir.Position{Line: 12}}},
		Properties: map[string]graph.PropertyValue{"file_path": graph.StringValue("src/test/java/shop/OrderServiceTest.java"), "language": graph.StringValue("java")}})
	f.st.AddEdge(graph.EdgeCalls, test.ID, f.placeOrder.ID)
	return test
}

func hitIDs(hits []ImpactHit) map[string]ImpactHit {
	out := map[string]ImpactHit{}
	for _, h := range hits {
		out[h.Node.Fact.Node.ID] = h
	}
	return out
}

func TestImpactChangeKinds(t *testing.T) {
	f := newFixture()
	test := withBuild(f)
	ctx := context.Background()
	// body: callers, and callers of what placeOrder overrides (place), transitively.
	res, err := Impact(ctx, f.st, ImpactRequest{RepositoryID: repo, NodeID: f.placeOrder.ID, Change: ChangeBody})
	if err != nil {
		t.Fatal(err)
	}
	got := hitIDs(res.Hits)
	if res.Change != ChangeBody || len(res.Roots) != 2 || len(got) != 4 || got[f.checkout.ID].Depth != 1 || got[f.legacy.ID].From != f.place.ID || got[f.cron.ID].Depth != 2 || got[test.ID].Root != "test" {
		t.Fatalf("body: %+v", res)
	}
	// signature: every direct user, no dispatch roots.
	if res, err = Impact(ctx, f.st, ImpactRequest{RepositoryID: repo, NodeID: f.placeOrder.ID, Change: ChangeSignature}); err != nil || len(res.Roots) != 1 || len(res.Hits) != 2 {
		t.Fatalf("signature: %+v %v", res, err)
	}
	// remove: the same users; a type use counts too.
	if res, err = Impact(ctx, f.st, ImpactRequest{RepositoryID: repo, NodeID: f.orderService.ID, Change: ChangeRemove, Depth: 1}); err != nil || len(res.Hits) != 1 || res.Hits[0].Node.Fact.Node.ID != f.placeOrder.ID || res.Hits[0].Via != graph.EdgeUsesType {
		t.Fatalf("remove: %+v %v", res, err)
	}
	// contract on the interface method: its overrider is a root too, so the
	// overrider's callers are impacted alongside the interface's callers.
	res, err = Impact(ctx, f.st, ImpactRequest{RepositoryID: repo, NodeID: f.place.ID, Change: ChangeContract})
	if err != nil {
		t.Fatal(err)
	}
	got = hitIDs(res.Hits)
	if len(res.Roots) != 2 || res.Roots[1] != f.placeOrder.ID || len(got) != 4 || got[f.checkout.ID].From != f.placeOrder.ID || got[f.legacy.ID].From != f.place.ID {
		t.Fatalf("contract: %+v", res)
	}
	if _, err = Impact(ctx, f.st, ImpactRequest{RepositoryID: repo, NodeID: f.place.ID, Change: "rename"}); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("unknown change: %v", err)
	}
}

func TestImpactAssessment(t *testing.T) {
	f := newFixture()
	test := withBuild(f)
	res, err := Impact(context.Background(), f.st, ImpactRequest{RepositoryID: repo, NodeID: f.placeOrder.ID})
	if err != nil {
		t.Fatal(err)
	}
	a := res.Assessment
	if a == nil {
		t.Fatal("no assessment")
	}
	// Three main hits and one test hit, all in the root module.
	if len(a.Groups) != 2 || a.Groups[0].Module != "." || a.Groups[0].Root != "main" || a.Groups[0].Nodes != 3 || a.Groups[1].Root != "test" || a.Groups[1].Nodes != 1 {
		t.Fatalf("groups: %+v", a.Groups)
	}
	if a.ByDepth["1"] != 3 || a.ByDepth["2"] != 1 {
		t.Fatalf("by depth: %+v", a.ByDepth)
	}
	// checkout and nightly have no dependants of their own; legacyCheckout does.
	entries := map[string]bool{}
	for _, b := range a.EntryPoints {
		entries[b.ID] = true
	}
	if !a.EntryPointsComplete || len(entries) != 2 || !entries[f.checkout.ID] || !entries[f.cron.ID] {
		t.Fatalf("entry points: %+v", a.EntryPoints)
	}
	if len(a.Tests) != 1 || a.Tests[0].Class != "shop.OrderServiceTest" || a.Tests[0].Module != "." || a.Tests[0].Nodes != 1 {
		t.Fatalf("tests: %+v", a.Tests)
	}
	if len(a.Commands) != 1 || a.Commands[0] != "mvn -Dtest=shop.OrderServiceTest -Dsurefire.failIfNoSpecifiedTests=false test" {
		t.Fatalf("commands: %v", a.Commands)
	}
	hit := hitIDs(res.Hits)[test.ID]
	if hit.Module != "." || hit.Root != "test" {
		t.Fatalf("test hit location: %+v", hit)
	}
	c := CompactImpact(res)
	if c.Assessment == nil || len(c.Assessment.Commands) != 1 || c.Nodes[0].Module != "." {
		t.Fatalf("compact carries the assessment: %+v", c)
	}
}

func TestLocateAndTestClass(t *testing.T) {
	cases := []struct {
		path, sourceSet, module, root, class string
	}{
		{"core/src/main/java/com/google/adk/agents/BaseAgent.java", "maven-module:abc:main", "core", "main", ""},
		{"core/src/test/java/com/google/adk/runner/RunnerTest.java", "maven-module:abc:test", "core", "test", "com.google.adk.runner.RunnerTest"},
		{"contrib/planners/src/test/java/com/google/adk/planner/LoopPlannerTest.java", "", "contrib/planners", "test", "com.google.adk.planner.LoopPlannerTest"},
		{"tokt/src/test/kotlin/com/google/adk/tokt/KtRunnerInteropTest.kt", "", "tokt", "test", "com.google.adk.tokt.KtRunnerInteropTest"},
		{"src/main/java/App.java", "", ".", "main", ""},
		{"dev/browser/main.js", "", "dev", "main", ""},
	}
	for _, c := range cases {
		props := map[string]graph.PropertyValue{"file_path": graph.StringValue(c.path), "language": graph.StringValue("java")}
		if c.sourceSet != "" {
			props["source_set_id"] = graph.StringValue(c.sourceSet)
		}
		info := locate(props)
		if info.module != c.module || info.root != c.root {
			t.Errorf("%s: module %q root %q", c.path, info.module, info.root)
		}
		if c.class != "" {
			if tc := testClassOf(info); tc == nil || tc.Class != c.class {
				t.Errorf("%s: test class %+v", c.path, tc)
			}
		}
	}
	cmds := testCommands([]TestClass{{Module: "core", Class: "b.T2", Language: "java"}, {Module: "core", Class: "a.T1", Language: "java"}, {Module: "web", Class: "x", Language: "typescript"}})
	if len(cmds) != 1 || cmds[0] != "mvn -pl core -am -Dtest=a.T1,b.T2 -Dsurefire.failIfNoSpecifiedTests=false test" {
		t.Fatalf("commands: %v", cmds)
	}
}

func TestChangesAndChangeImpact(t *testing.T) {
	f := newFixture()
	test := withBuild(f)
	ctx := context.Background()
	// Generation 2: refund is added, placeOrder is updated, pay is retired.
	refund := graph.Node{ID: graph.ID("method", "refund(shop.Order)"), Kind: "method", Name: "refund", QualifiedName: "refund(shop.Order)", Properties: map[string]graph.PropertyValue{"file_path": graph.StringValue("src/shop/PaymentService.java")}}
	f.st.AddVersion(graph.Version{Fact: graph.Fact{Node: &refund}, GenFrom: 2, CommitFrom: "c2"})
	old := f.st.Nodes[f.placeOrder.ID]
	closed := old
	closed.GenTo, closed.CommitTo = 2, "c2"
	f.st.AddVersion(closed)
	current := old
	current.GenFrom, current.CommitFrom = 2, "c2"
	f.st.AddVersion(current)
	pay := f.st.Nodes[f.pay.ID]
	pay.GenTo, pay.CommitTo, pay.Retired = 2, "c2", true
	delete(f.st.Nodes, f.pay.ID)
	f.st.AddVersion(pay)

	set, err := Changes(ctx, f.st, ChangesRequest{RepositoryID: repo, Generation: 2})
	if err != nil {
		t.Fatal(err)
	}
	ops := map[string]string{}
	for _, n := range set.Nodes {
		ops[n.ID] = n.Op
	}
	if set.Generation != 2 || set.Added != 1 || set.Updated != 1 || set.Retired != 1 || ops[refund.ID] != "added" || ops[f.placeOrder.ID] != "updated" || ops[f.pay.ID] != "retired" {
		t.Fatalf("change set: %+v", set)
	}
	// Generation 1 added every node; the totals count every record, the
	// listing only declarations (nine here, not the two file nodes).
	if set, err = Changes(ctx, f.st, ChangesRequest{RepositoryID: repo, Generation: 1, Limit: 3}); err != nil || len(set.Nodes) != 9 || set.Added != 11 {
		t.Fatalf("generation 1: %+v %v", set, err)
	}
	if set, err = Changes(ctx, f.st, ChangesRequest{RepositoryID: repo, Generation: 1, Kinds: []string{"source_file"}}); err != nil || len(set.Nodes) != 2 {
		t.Fatalf("kinds filter: %+v %v", set, err)
	}

	res, err := ChangeImpact(ctx, f.st, ChangeImpactRequest{RepositoryID: repo, Generation: 2})
	if err != nil {
		t.Fatal(err)
	}
	// Roots: refund and placeOrder at generation 2, pay at generation 1.
	// placeOrder's callers are impacted; pay's only dependant is placeOrder,
	// itself a root, so it adds nothing.
	got := hitIDs(res.Impact.Hits)
	if res.Generation != 2 || len(res.Changed) != 3 || res.RootsTruncated || len(res.Impact.Roots) != 3 || len(got) != 2 || got[f.checkout.ID].Depth != 1 || got[test.ID].Root != "test" || res.Impact.Assessment == nil || len(res.Impact.Assessment.Commands) != 1 {
		t.Fatalf("change impact: %+v", res)
	}
	if res, err = ChangeImpact(ctx, f.st, ChangeImpactRequest{RepositoryID: repo, Generation: 2, MaxRoots: 1}); err != nil || !res.RootsTruncated || len(res.Changed) != 1 {
		t.Fatalf("root bound: %+v %v", res, err)
	}
	if _, err = ChangeImpact(ctx, f.st, ChangeImpactRequest{RepositoryID: repo, Generation: 2, Change: "x"}); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("change kind: %v", err)
	}
	c := CompactChangeImpact(res)
	if c.Generation != 2 || len(c.Impact.Nodes) != len(res.Impact.Hits) {
		t.Fatalf("compact: %+v", c)
	}
	_ = spannerstore.ChangeAdded
}
