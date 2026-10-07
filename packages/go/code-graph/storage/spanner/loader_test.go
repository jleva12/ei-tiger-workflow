package spannerstore

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

const (
	testCommit1 = "1111111111111111111111111111111111111111"
	testCommit2 = "2222222222222222222222222222222222222222"
)

func testNode(kind, name string, props map[string]graph.PropertyValue) graph.Fact {
	return graph.Fact{Node: &graph.Node{ID: graph.ID(kind, name), Kind: kind, Name: name, QualifiedName: "com.acme." + name, Properties: props}}
}

func testEdge(kind, from, to string) graph.Fact {
	return graph.Fact{Edge: &graph.Edge{ID: graph.ID("edge", kind, from, to), Kind: kind, SourceID: from, TargetID: to}}
}

func TestPlanChangeAdd(t *testing.T) {
	f := testNode("method", "Foo.bar", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/Foo.java"), "source_text": graph.StringValue("int bar() { return 1; }")})
	lineage := graph.Lineage("repo", "m", "main", "src/Foo.java")
	p, err := planChange("repo", 3, testCommit1, graph.Change{Op: graph.OpAdd, Key: f.Key(), Lineage: lineage, After: &f}, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if p.Close != nil || p.Insert == nil || len(p.mutations()) != 1 {
		t.Fatalf("add plans exactly one insert: %+v", p)
	}
	r := p.Insert
	doc, _ := codesearch.FromNode(*f.Node)
	if r.Repository != "repo" || r.Kind != graph.RecordNode || r.ID != f.Node.ID || r.GenFrom != 3 || r.Commit != testCommit1 || r.Lineage != lineage ||
		r.FactKind != "method" || r.Name != "Foo.bar" || r.SourceID != "" || r.TargetID != "" || r.FactDigest != f.Digest() || r.SearchHash != doc.Hash {
		t.Fatalf("row fields: %+v", *r)
	}
	if !strings.Contains(string(r.Payload), `"node"`) {
		t.Fatalf("payload is the fact JSON: %s", r.Payload)
	}
}

func TestPlanChangeEdgeAndIneligibleNode(t *testing.T) {
	e := testEdge(graph.EdgeCalls, graph.ID("method", "a"), graph.ID("method", "b"))
	p, err := planChange("repo", 1, testCommit1, graph.Change{Op: graph.OpAdd, Key: e.Key(), After: &e}, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if r := p.Insert; r.Kind != graph.RecordEdge || r.SourceID != e.Edge.SourceID || r.TargetID != e.Edge.TargetID || r.FactKind != graph.EdgeCalls || r.SearchHash != "" || r.Lineage != "" {
		t.Fatalf("edge row: %+v", *r)
	}
	n := testNode(graph.NodeExternalSymbol, "java.util.List", nil)
	p, err = planChange("repo", 1, testCommit1, graph.Change{Op: graph.OpReopen, Key: n.Key(), After: &n}, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if p.Insert.SearchHash != "" {
		t.Fatal("external symbols are not search documents")
	}
}

func TestPlanChangeUpdateRetire(t *testing.T) {
	before := testNode("class", "Foo", nil)
	after := testNode("class", "Foo", map[string]graph.PropertyValue{"visibility": graph.StringValue("public")})
	old := graph.Version{Fact: before, GenFrom: 2, CommitFrom: testCommit1}
	p, err := planChange("repo", 5, testCommit2, graph.Change{Op: graph.OpUpdate, Key: after.Key(), Before: &old, After: &after}, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if p.Close == nil || p.Insert == nil || len(p.mutations()) != 2 {
		t.Fatalf("update closes and inserts: %+v", p)
	}
	if c := p.Close; c.GenFrom != 2 || c.GenTo != 5 || c.Commit != testCommit2 || c.Retired || c.ID != before.Node.ID || c.Kind != graph.RecordNode {
		t.Fatalf("close op: %+v", *c)
	}
	if p.Insert.GenFrom != 5 || p.Insert.FactDigest == old.Fact.Digest() {
		t.Fatalf("new version: %+v", *p.Insert)
	}
	p, err = planChange("repo", 5, testCommit2, graph.Change{Op: graph.OpRetire, Key: before.Key(), Before: &old}, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if p.Insert != nil || p.Close == nil || !p.Close.Retired || p.Close.GenTo != 5 || len(p.mutations()) != 1 {
		t.Fatalf("retire closes with Retired=true: %+v", p)
	}
}

func TestPlanChangeRejects(t *testing.T) {
	f := testNode("class", "Foo", nil)
	old := graph.Version{Fact: f, GenFrom: 5, CommitFrom: testCommit1}
	if _, err := planChange("repo", 5, testCommit2, graph.Change{Op: graph.OpRetire, Key: f.Key(), Before: &old}, 1<<20); !errors.Is(err, graph.ErrInvalid) {
		t.Fatalf("before-image at the loading generation: %v", err)
	}
	if _, err := planChange("repo", 5, testCommit2, graph.Change{Op: graph.OpAdd, Key: f.Key()}, 1<<20); !errors.Is(err, graph.ErrInvalid) {
		t.Fatalf("add without after: %v", err)
	}
	big := testNode("class", "Big", map[string]graph.PropertyValue{"source_text": graph.StringValue(strings.Repeat("x", 4096))})
	if _, err := planChange("repo", 1, testCommit1, graph.Change{Op: graph.OpAdd, Key: big.Key(), After: &big}, 1024); !errors.Is(err, deployment.ErrLimitExceeded) {
		t.Fatalf("oversize record: %v", err)
	}
	if _, err := planChange("repo", 1, testCommit1, graph.Change{Op: "rename", Key: f.Key(), After: &f}, 1<<20); !errors.Is(err, graph.ErrInvalid) {
		t.Fatalf("unknown op: %v", err)
	}
}

func TestNewLoaderValidatesInputs(t *testing.T) {
	s := &Store{limits: DefaultLimits()}
	ctx := context.Background()
	l := s.NewLoader(deployment.Lease{}, 1, testCommit1, LoaderOptions{})
	if err := l.Apply(ctx, nil); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("invalid lease must poison the loader: %v", err)
	}
	lease := deployment.Lease{Fence: deployment.Fence{Key: deployment.RunKey{RepositoryID: "r", RunID: "run"}, Token: 1}, OwnerID: "w", ExpiresAt: time.Now().Add(time.Minute)}
	if err := s.NewLoader(lease, 0, testCommit1, LoaderOptions{}).Flush(ctx); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("generation zero: %v", err)
	}
	if err := s.NewLoader(lease, 1, "abc", LoaderOptions{}).Flush(ctx); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("bad commit: %v", err)
	}
	if st := s.NewLoader(lease, 1, testCommit1, LoaderOptions{}).Stats(); st != (LoaderStats{}) {
		t.Fatalf("fresh stats: %+v", st)
	}
}

func TestPlanChangeBuildsSearchDocument(t *testing.T) {
	f := testNode("method", "runAsyncImpl", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/LlmAgent.java"), "source_text": graph.StringValue("void runAsyncImpl() { }")})
	p, err := planChange("repo", 2, testCommit1, graph.Change{Op: graph.OpAdd, Key: f.Key(), After: &f}, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if p.Document == nil || len(p.mutations()) != 1 {
		t.Fatalf("a document node plans a search document beside its record: %+v", p)
	}
	d := p.Document
	doc, _ := codesearch.FromNode(*f.Node)
	if d.Repository != "repo" || d.NodeID != f.Node.ID || d.Generation != 2 || d.Kind != "method" || d.Name != "runAsyncImpl" || d.QualifiedName != "com.acme.runAsyncImpl" || d.FilePath != "src/LlmAgent.java" || d.Document.Hash != doc.Hash || d.Document.Text != doc.Text {
		t.Fatalf("document row: %+v", *d)
	}
	for _, want := range []string{"runAsyncImpl", "Async", "Impl", "acme"} {
		if !strings.Contains(" "+d.Identifiers+" ", " "+want+" ") {
			t.Fatalf("identifiers lack %q: %s", want, d.Identifiers)
		}
	}
	if d.bytes() <= len(doc.Text) {
		t.Fatal("row size estimate must cover text and overhead")
	}
	e := testEdge(graph.EdgeCalls, graph.ID("method", "a"), graph.ID("method", "b"))
	if p, err = planChange("repo", 2, testCommit1, graph.Change{Op: graph.OpAdd, Key: e.Key(), After: &e}, 1<<20); err != nil || p.Document != nil {
		t.Fatalf("edges carry no document: %+v %v", p, err)
	}
	x := testNode(graph.NodeExternalSymbol, "java.util.List", nil)
	if p, err = planChange("repo", 2, testCommit1, graph.Change{Op: graph.OpAdd, Key: x.Key(), After: &x}, 1<<20); err != nil || p.Document != nil {
		t.Fatalf("external symbols carry no document: %+v %v", p, err)
	}
}
