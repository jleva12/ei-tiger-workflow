package java

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/ir"
)

func decl(start, end int64, kind, name string) event {
	return event{Kind: "declaration", Path: "sources/x/A.java", Start: start, End: end, ElementKind: kind, Name: name, SignatureValid: "true"}
}

func TestDeclarationIndexInnermostContaining(t *testing.T) {
	events := []event{
		decl(0, 200, "CLASS", "A"),
		decl(10, 100, "METHOD", "m"),
		decl(20, 60, "CLASS", "Local"),
		decl(30, 50, "METHOD", "m"), // Local.m, same name as the enclosing method
		decl(110, 120, "LOCAL_VARIABLE", "a"),
		decl(110, 130, "LOCAL_VARIABLE", "b"), // int a=1, b=2: shared start, different ends
		decl(150, 170, "FIELD", "f"),
		decl(160, 180, "FIELD", "g"), // partial overlap, never produced by javac but must not break lookups
	}
	x := newDeclarationIndex(events)
	cases := []struct {
		start, end int64
		kind       ir.DeclarationKind
		name       string
		want       int64 // expected event start; -1 for none
	}{
		{35, 36, ir.DeclarationMethod, "m", 30}, // innermost m
		{12, 13, ir.DeclarationMethod, "m", 10}, // outer m by its own name span
		{35, 36, ir.DeclarationClass, "A", 0},   // walks up past non-matching containers
		{112, 113, ir.DeclarationLocal, "a", 110},
		{125, 126, ir.DeclarationLocal, "b", 110},
		{112, 113, ir.DeclarationLocal, "b", 110}, // b also contains a's span; the query asks for b
		{175, 176, ir.DeclarationField, "g", 160},
		{300, 301, ir.DeclarationClass, "A", -1},
		{35, 36, ir.DeclarationField, "z", -1},
	}
	for _, c := range cases {
		e, ok := x.innermost(c.start, c.end, func(e event) bool { return declarationMatches(c.kind, c.name, e) })
		if c.want < 0 {
			if ok {
				t.Fatalf("[%d,%d] %s %s: unexpected %+v", c.start, c.end, c.kind, c.name, e)
			}
			continue
		}
		if !ok || e.Start != c.want || e.Name != c.name {
			t.Fatalf("[%d,%d] %s %s: got %+v ok=%v", c.start, c.end, c.kind, c.name, e, ok)
		}
	}
	// The per-file lookup goes through the name span and kind matching.
	fe := newFileEvents("sources/x/A.java")
	for _, e := range events {
		fe.add(e)
	}
	ns := span(31, 32)
	if e, ok := fe.declarationFor(&declSummary{Kind: ir.DeclarationMethod, Name: "m", Span: span(30, 50), NameSpan: &ns}); !ok || e.Start != 30 {
		t.Fatalf("declarationFor: %+v %v", e, ok)
	}
	if e, ok := fe.declarationFor(&declSummary{Kind: ir.DeclarationConstructor, Name: "A", Span: span(30, 50)}); ok {
		t.Fatalf("constructor matched a method: %+v", e)
	}
}

func TestStreamEventsGroupsOneFileAtATime(t *testing.T) {
	lines := []string{
		`{"kind":"diagnostic","path":"sources/x/A.java","start":5,"end":9,"code":"c","severity":"ERROR","message":"bad"}`,
		`{"kind":"declaration","path":"sources/x/A.java","start":0,"end":100,"element_kind":"CLASS","name":"A"}`,
		`{"kind":"binding","path":"sources/x/A.java","start":10,"end":20,"tree_kind":"IDENTIFIER","status":"resolved"}`,
		`{"kind":"candidate","path":"sources/x/A.java","start":10,"end":20,"tree_kind":"METHOD_INVOCATION"}`,
		`{"kind":"override","path":"sources/x/A.java","start":30,"end":40}`,
		`{"kind":"implements","path":"sources/x/A.java","start":50,"end":60,"tree_kind":"LAMBDA_EXPRESSION","status":"resolved","interface":{"kind":"interface","name":"R"}}`,
		`{"kind":"declaration","path":"sources/x/B.java","start":0,"end":10,"element_kind":"CLASS","name":"B"}`,
		`{"kind":"diagnostic","path":"","code":"c","severity":"ERROR","message":"global"}`,
		`{"kind":"summary","source_files":2}`,
	}
	path := filepath.Join(t.TempDir(), "events.jsonl")
	must(t, os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o600))
	var seen []string
	var first *fileEvents
	err := streamEvents(context.Background(), path, func(fe *fileEvents) error {
		seen = append(seen, fe.path)
		if fe.path == "sources/x/A.java" {
			copy := *fe
			first = &copy
		}
		return nil
	})
	must(t, err)
	if strings.Join(seen, ",") != "sources/x/A.java,sources/x/B.java," {
		t.Fatalf("file order: %v", seen)
	}
	if first == nil || len(first.declarations) != 1 || len(first.bindings[spanKey{10, 20}]) != 1 || len(first.candidates[spanKey{10, 20}]) != 1 || len(first.overrides) != 1 || len(first.implements) != 1 || first.implements[0].Interface == nil || first.implements[0].Interface.Name != "R" {
		t.Fatalf("grouped events: %+v", first)
	}
	if d := first.diagnosticAt(span(8, 12)); !strings.HasPrefix(d, "compiler_error: c: bad") {
		t.Fatalf("diagnostic overlap: %q", d)
	}
	if d := first.diagnosticAt(span(9, 12)); d != "" {
		t.Fatalf("half-open overlap: %q", d)
	}
	if e, ok := first.at(span(10, 20), "reference"); !ok || e.TreeKind != "IDENTIFIER" {
		t.Fatalf("at: %+v %v", e, ok)
	}
	if _, ok := first.at(span(10, 20), "call"); ok {
		t.Fatal("identifier accepted as a call")
	}
	// Missing summary: truncated stream.
	must(t, os.WriteFile(path, []byte(strings.Join(lines[:3], "\n")+"\n"), 0o600))
	if err := streamEvents(context.Background(), path, func(*fileEvents) error { return nil }); err == nil || !strings.Contains(err.Error(), "truncated") {
		t.Fatalf("truncated stream accepted: %v", err)
	}
	// Frames after the summary are contamination.
	must(t, os.WriteFile(path, []byte(lines[8]+"\n"+lines[1]+"\n"), 0o600))
	if err := streamEvents(context.Background(), path, func(*fileEvents) error { return nil }); err == nil {
		t.Fatal("frames after summary accepted")
	}
}

func TestDecodeEventRejectsMalformedFramesWithBoundedEvidence(t *testing.T) {
	for _, frame := range []string{"", `{"kind":"binding"`, strings.Repeat("x", 4096), `{"kind":"unknown"}`, `{"kind":"context_done"}`} {
		_, err := decodeEvent([]byte(frame), 17)
		if err == nil || !strings.Contains(err.Error(), "frame 17") || len(err.Error()) > 512 {
			t.Errorf("frame error not bounded/contextual: %v", err)
		}
	}
	e, err := decodeEvent([]byte(`{"kind":"summary","source_files":1}`), 18)
	if err != nil || e.SourceFiles != 1 {
		t.Fatalf("valid frame rejected: %+v %v", e, err)
	}
}
