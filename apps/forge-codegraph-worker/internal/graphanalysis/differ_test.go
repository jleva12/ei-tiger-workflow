package graphanalysis

import (
	"context"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
)

// fakeReader serves one baseline generation from in-memory versions.
type fakeReader struct {
	lineages map[string][]graph.Version
	nodes    map[string]graph.Version
	edges    map[string]graph.Version
	reads    int
}

func (r *fakeReader) RecordsByLineage(_ context.Context, _, lineage string, generation uint64) ([]graph.Version, error) {
	r.reads++
	var out []graph.Version
	for _, v := range r.lineages[lineage] {
		if v.OpenAt(generation) {
			out = append(out, v)
		}
	}
	return out, nil
}

func (r *fakeReader) get(table map[string]graph.Version, ids []string, generation uint64) map[string]graph.Version {
	r.reads++
	out := map[string]graph.Version{}
	for _, id := range ids {
		if v, ok := table[id]; ok && v.GenFrom <= generation {
			out[id] = v
		}
	}
	return out
}

func (r *fakeReader) GetNodes(_ context.Context, _ string, ids []string, generation uint64) (map[string]graph.Version, error) {
	return r.get(r.nodes, ids, generation), nil
}

func (r *fakeReader) GetEdges(_ context.Context, _ string, ids []string, generation uint64) (map[string]graph.Version, error) {
	return r.get(r.edges, ids, generation), nil
}

var (
	testCommit  = strings.Repeat("a", 40)
	testSHA     = strings.Repeat("b", 64)
	lineageA    = graph.Lineage("repo", "app", "main", "src/A.java")
	lineageGone = graph.Lineage("repo", "app", "main", "src/Gone.java")
)

func anchored(lineage string, line uint32) *graph.SourceAnchor {
	return &graph.SourceAnchor{Lineage: lineage, ContentSHA256: testSHA, Span: ir.Span{Start: ir.Position{Line: line}, End: ir.Position{Line: line}}}
}

func nodeFact(id, name string, a *graph.SourceAnchor) graph.Fact {
	return graph.Fact{Node: &graph.Node{ID: id, Kind: "method", Name: name, Source: a}}
}

func edgeFact(id, source, target string, a *graph.SourceAnchor) graph.Fact {
	return graph.Fact{Edge: &graph.Edge{ID: id, Kind: graph.EdgeCalls, SourceID: source, TargetID: target, Source: a}}
}

func version(f graph.Fact, lineage string, from, to uint64, retired bool) graph.Version {
	v := graph.Version{Fact: f, Lineage: lineage, GenFrom: from, CommitFrom: testCommit, FactDigest: f.Digest()}
	if to != 0 {
		v.GenTo, v.CommitTo, v.Retired = to, testCommit, retired
	}
	return v
}

type applied struct {
	batches [][]graph.Change
}

func (a *applied) apply(_ context.Context, changes []graph.Change) error {
	for _, c := range changes {
		if err := c.Validate(); err != nil {
			return err
		}
	}
	a.batches = append(a.batches, changes)
	return nil
}

func (a *applied) all() []graph.Change {
	var out []graph.Change
	for _, b := range a.batches {
		out = append(out, b...)
	}
	return out
}

func opsOf(changes []graph.Change) map[graph.Operation][]string {
	out := map[graph.Operation][]string{}
	for _, c := range changes {
		out[c.Op] = append(out[c.Op], c.Key.ID)
	}
	return out
}

func TestDifferInitialRunAddsEverything(t *testing.T) {
	ctx := context.Background()
	sink := &applied{}
	d := &Differ{Repo: "repo", Apply: sink.apply}
	n1, n2 := graph.ID("entity", "n1"), graph.ID("entity", "n2")
	must(t, d.Emit(ctx, nodeFact(n1, "one", anchored(lineageA, 1))))
	must(t, d.Emit(ctx, nodeFact(n2, "two", anchored(lineageA, 2))))
	must(t, d.Emit(ctx, edgeFact(graph.ID("edge", "e1"), n1, n2, anchored(lineageA, 1))))
	must(t, d.Emit(ctx, nodeFact(graph.ID("external", "x1"), "x", nil)))
	if len(sink.batches) != 0 {
		t.Fatal("nothing may be applied before the lineage closes")
	}
	must(t, d.Finish(ctx))
	changes := sink.all()
	if len(changes) != 4 || len(sink.batches) != 2 {
		t.Fatalf("changes %+v in %d batches", opsOf(changes), len(sink.batches))
	}
	for _, c := range changes {
		if c.Op != graph.OpAdd || c.Before != nil || c.After == nil {
			t.Fatalf("initial change: %+v", c)
		}
		if strings.HasPrefix(c.Key.ID, "external:") != (c.Lineage == "") {
			t.Fatalf("change lineage: %+v", c)
		}
	}
	if len(d.RetiredNodeIDs()) != 0 {
		t.Fatal("initial run retires nothing")
	}
	if err := d.Emit(ctx, nodeFact(n1, "late", anchored(lineageA, 1))); err == nil {
		t.Fatal("emit after finish must fail")
	}
}

func TestDifferAgainstBaseline(t *testing.T) {
	ctx := context.Background()
	ids := map[string]string{}
	for _, name := range []string{"n1", "n2", "n3", "n4", "n5", "x0", "x1", "x2", "x3", "x4", "m1"} {
		ids[name] = graph.ID("entity", name)
	}
	e1, m2 := graph.ID("edge", "e1"), graph.ID("edge", "m2")
	same := func(name string, line uint32) graph.Fact { return nodeFact(ids[name], name, anchored(lineageA, line)) }
	reader := &fakeReader{lineages: map[string][]graph.Version{}, nodes: map[string]graph.Version{}, edges: map[string]graph.Version{}}
	open := func(lineage string, f graph.Fact) {
		v := version(f, lineage, 2, 0, false)
		reader.lineages[lineage] = append(reader.lineages[lineage], v)
		if f.Node != nil {
			reader.nodes[f.Key().ID] = v
		} else {
			reader.edges[f.Key().ID] = v
		}
	}
	open(lineageA, same("n1", 1))
	open(lineageA, nodeFact(ids["n2"], "n2-old", anchored(lineageA, 2)))
	open(lineageA, same("n3", 3))
	open(lineageA, edgeFact(e1, ids["n1"], ids["n2"], anchored(lineageA, 1)))
	// n5 was retired at generation 2 and reappears now.
	reader.nodes[ids["n5"]] = version(same("n5", 5), lineageA, 1, 2, true)
	reader.lineages[lineageA] = append(reader.lineages[lineageA], reader.nodes[ids["n5"]])
	// A record retired above the baseline must stay invisible.
	reader.nodes[ids["n4"]] = version(same("n4", 4), lineageA, 4, 0, false)
	// Lineage-less externals: x0 open and unmentioned, x1 unchanged, x2
	// changed, x3 new, x4 retired earlier.
	external := func(name, text string) graph.Fact {
		return graph.Fact{Node: &graph.Node{ID: ids[name], Kind: graph.NodeExternalSymbol, Name: text}}
	}
	for _, name := range []string{"x0", "x1"} {
		reader.nodes[ids[name]] = version(external(name, name), "", 1, 0, false)
	}
	reader.nodes[ids["x2"]] = version(external("x2", "x2-old"), "", 1, 0, false)
	reader.nodes[ids["x4"]] = version(external("x4", "x4"), "", 1, 2, true)
	// A deleted file's lineage.
	open(lineageGone, nodeFact(ids["m1"], "m1", anchored(lineageGone, 1)))
	open(lineageGone, edgeFact(m2, ids["m1"], ids["n1"], anchored(lineageGone, 1)))

	sink := &applied{}
	d := &Differ{Repo: "repo", Baseline: 3, Reader: reader, Apply: sink.apply, LineagelessBatch: 2}
	for _, name := range []string{"x1", "x2", "x3", "x4"} {
		must(t, d.EmitLineageless(ctx, external(name, name)))
	}
	if len(sink.batches) != 2 {
		t.Fatalf("lineage-less facts flush per batch: %d batches", len(sink.batches))
	}
	for _, f := range []graph.Fact{same("n1", 1), nodeFact(ids["n2"], "n2-new", anchored(lineageA, 2)), same("n4", 4), same("n5", 5), edgeFact(e1, ids["n1"], ids["n2"], anchored(lineageA, 1))} {
		must(t, d.Emit(ctx, f))
	}
	must(t, d.Emit(ctx, same("n1", 1))) // an identical repeat is harmless
	// A second content for one key is dropped and reported: the first wins.
	must(t, d.Emit(ctx, nodeFact(ids["n1"], "conflict", anchored(lineageA, 1))))
	must(t, d.EmitLineageless(ctx, external("x1", "x1-again")))
	must(t, d.RetireLineage(ctx, lineageGone))
	must(t, d.Finish(ctx))
	if dropped, conflicts := d.Duplicates(); dropped != 3 || len(conflicts) != 2 || conflicts[0].ID != ids["n1"] || conflicts[1].ID != ids["x1"] {
		t.Fatalf("duplicates: %d %v", dropped, conflicts)
	}
	if nodes, edges := d.Accepted(); nodes != 8 || edges != 1 {
		t.Fatalf("accepted %d nodes, %d edges", nodes, edges)
	}

	ops := opsOf(sink.all())
	want := map[graph.Operation][]string{
		graph.OpUpdate: {ids["x2"], ids["n2"]},
		graph.OpAdd:    {ids["x3"], ids["n4"]},
		graph.OpReopen: {ids["x4"], ids["n5"]},
		graph.OpRetire: {ids["m1"], m2, ids["n3"]},
	}
	for op, expected := range want {
		if strings.Join(ops[op], ",") != strings.Join(expected, ",") {
			t.Fatalf("%s: got %v, want %v (all: %v)", op, ops[op], expected, ops)
		}
	}
	for _, c := range sink.all() {
		switch c.Op {
		case graph.OpUpdate, graph.OpRetire:
			if c.Before == nil || c.Before.GenTo != 0 || c.Before.Fact.Key() != c.Key {
				t.Fatalf("%s before-image: %+v", c.Op, c)
			}
		}
		if c.Op == graph.OpUpdate && c.Key.ID == ids["n2"] && (c.Before.Fact.Node.Name != "n2-old" || c.After.Node.Name != "n2-new" || c.Lineage != lineageA) {
			t.Fatalf("update of n2: %+v", c)
		}
		external := false
		for _, name := range []string{"x0", "x1", "x2", "x3", "x4"} {
			external = external || c.Key.ID == ids[name]
		}
		if external && c.Lineage != "" {
			t.Fatalf("lineage-less change carries a lineage: %+v", c)
		}
		if !external && c.Lineage == "" {
			t.Fatalf("file change lacks its lineage: %+v", c)
		}
	}
	retired := d.RetiredNodeIDs()
	if strings.Join(retired, ",") != strings.Join([]string{ids["m1"], ids["n3"]}, ",") {
		t.Fatalf("retired nodes: %v", retired)
	}
	if reader.reads == 0 {
		t.Fatal("baseline was never read")
	}
}

func TestDifferRequiresReaderForBaseline(t *testing.T) {
	d := &Differ{Repo: "repo", Baseline: 1, Apply: func(context.Context, []graph.Change) error { return nil }}
	if err := d.Emit(context.Background(), nodeFact(graph.ID("entity", "n"), "n", anchored(lineageA, 1))); err == nil {
		t.Fatal("a baseline without a reader must be rejected")
	}
	d = &Differ{Repo: "repo", Apply: func(context.Context, []graph.Change) error { return nil }}
	if err := d.Emit(context.Background(), graph.Fact{}); err == nil {
		t.Fatal("an empty fact must be rejected")
	}
	if err := d.RetireLineage(context.Background(), "bad lineage"); err == nil {
		t.Fatal("an invalid lineage must be rejected")
	}
}

// Regression for the audit's finding 4: a renamed file continues its
// entities under a new lineage. Retiring the old lineage must skip every
// record projected this run, so continued entities are replaced, never
// retired, and never reach the edge cascade.
func TestDifferRenameKeepsContinuedEntities(t *testing.T) {
	ctx := context.Background()
	oldLineage := graph.Lineage("repo", "app", "main", "src/Helper.java")
	newLineage := graph.Lineage("repo", "app", "main", "src/Helpers.java")
	callerLineage := graph.Lineage("repo", "app", "main", "src/Caller.java")
	helper, caller := graph.ID("entity", "helper"), graph.ID("entity", "caller")
	file := func(lineage, name string) graph.Fact {
		return graph.Fact{Node: &graph.Node{ID: lineage, Kind: graph.NodeSourceFile, Name: name, Source: anchored(lineage, 1)}}
	}
	contains := func(lineage string) graph.Fact {
		return graph.Fact{Edge: &graph.Edge{ID: graph.ID("edge", graph.EdgeContains, lineage, helper), Kind: graph.EdgeContains, SourceID: lineage, TargetID: helper, Source: anchored(lineage, 1)}}
	}
	calls := edgeFact(graph.ID("edge", "calls"), caller, helper, anchored(callerLineage, 3))

	reader := &fakeReader{lineages: map[string][]graph.Version{}, nodes: map[string]graph.Version{}, edges: map[string]graph.Version{}}
	open := func(lineage string, f graph.Fact) {
		v := version(f, lineage, 1, 0, false)
		reader.lineages[lineage] = append(reader.lineages[lineage], v)
		if f.Node != nil {
			reader.nodes[f.Key().ID] = v
		} else {
			reader.edges[f.Key().ID] = v
		}
	}
	open(oldLineage, file(oldLineage, "Helper.java"))
	open(oldLineage, nodeFact(helper, "ping", anchored(oldLineage, 2)))
	open(oldLineage, contains(oldLineage))
	open(callerLineage, nodeFact(caller, "call", anchored(callerLineage, 2)))
	open(callerLineage, calls)

	sink := &applied{}
	d := &Differ{Repo: "repo", Baseline: 1, Reader: reader, Apply: sink.apply}
	// The renamed file projects under its new lineage with the continued ID.
	must(t, d.Emit(ctx, file(newLineage, "Helpers.java")))
	must(t, d.Emit(ctx, nodeFact(helper, "ping", anchored(newLineage, 2))))
	must(t, d.Emit(ctx, contains(newLineage)))
	// The caller is a dependent, re-projected without any change.
	must(t, d.Emit(ctx, nodeFact(caller, "call", anchored(callerLineage, 2))))
	must(t, d.Emit(ctx, calls))
	must(t, d.RetireLineage(ctx, oldLineage))
	must(t, d.Finish(ctx))

	ops := opsOf(sink.all())
	want := map[graph.Operation][]string{
		graph.OpAdd:    {newLineage, contains(newLineage).Key().ID},
		graph.OpUpdate: {helper},
		graph.OpRetire: {oldLineage, contains(oldLineage).Key().ID},
	}
	for op, expected := range want {
		if strings.Join(ops[op], ",") != strings.Join(expected, ",") {
			t.Fatalf("%s: got %v, want %v (all: %v)", op, ops[op], expected, ops)
		}
	}
	if len(ops[graph.OpReopen]) != 0 {
		t.Fatalf("unexpected reopen: %v", ops)
	}
	for _, c := range sink.all() {
		if c.Key.ID == helper && (c.Op != graph.OpUpdate || c.Lineage != newLineage || c.Before.Lineage != oldLineage) {
			t.Fatalf("continued entity must move by update: %+v", c)
		}
	}
	if retired := d.RetiredNodeIDs(); strings.Join(retired, ",") != oldLineage {
		t.Fatalf("only the old file node may reach the cascade, got %v", retired)
	}
}
