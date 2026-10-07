package java

import (
	"strconv"
	"testing"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

func TestViewMatchPrefersOutermostSameNameAndUsesSpanForAnonymous(t *testing.T) {
	outerName, innerName := span(12, 15), span(42, 45)
	v := &fileView{file: &ir.SourceFile{}, decls: []declSummary{
		{ID: "class", Kind: ir.DeclarationClass, Name: "A", Span: span(0, 200)},
		{ID: "outer", Kind: ir.DeclarationMethod, Name: "run", Span: span(10, 100), NameSpan: &outerName, OwnerID: "class"},
		{ID: "inner", Kind: ir.DeclarationMethod, Name: "run", Span: span(40, 80), NameSpan: &innerName, OwnerID: "local"},
		{ID: "anon", Kind: ir.DeclarationClass, Name: "", Span: span(120, 160), Form: ir.TypeAnonymous, OwnerID: "class"},
	}, byID: map[ir.DeclarationID]int{"class": 0, "outer": 1, "inner": 2, "anon": 3}}
	if d, ok := v.match(ir.DeclarationMethod, "run", 10, 100); !ok || d.ID != "outer" {
		t.Fatalf("outer: %+v", d)
	}
	if d, ok := v.match(ir.DeclarationMethod, "run", 40, 80); !ok || d.ID != "inner" {
		t.Fatalf("inner: %+v", d)
	}
	if d, ok := v.match(ir.DeclarationClass, "", 120, 160); !ok || d.ID != "anon" {
		t.Fatalf("anonymous by span: %+v", d)
	}
	if _, ok := v.match(ir.DeclarationClass, "", 125, 160); ok {
		t.Fatal("anonymous matched although its span is not contained")
	}
	if _, ok := v.match(ir.DeclarationMethod, "run", -1, -1); ok {
		t.Fatal("negative span matched")
	}
	if _, ok := v.match(ir.DeclarationConstructor, "A", 10, 100); ok {
		t.Fatal("constructor kind matched a method")
	}
}

func TestIdentityViewMatchUsesInnermostContainingThenClosest(t *testing.T) {
	in := semantic.SourceInput{Source: ir.Source{FileID: "C", Path: "src/C.java", SourceSetID: "main"}, Lineage: "file:c"}
	v := viewFromIdentities("main", in, semantic.FileIdentities{Declarations: []semantic.DeclarationIdentity{
		{DeclarationID: "class", Kind: ir.DeclarationClass, Name: "C", Span: span(0, 200), Key: &semantic.DeclarationKey{OwnerKey: "p", Kind: ir.DeclarationClass, Name: "C", CanonicalSignature: "p.C"}},
		{DeclarationID: "field", Kind: ir.DeclarationField, Name: "x", Span: span(20, 30), OwnerID: "class"},
		{DeclarationID: "method", Kind: ir.DeclarationMethod, Name: "m", Span: span(40, 120), OwnerID: "class"},
		{DeclarationID: "anon", Kind: ir.DeclarationClass, Name: "", Span: span(60, 100), OwnerID: "method"},
	}})
	if v.file != nil || v.decls[0].Qualified != "p.C" || v.decls[3].Form != ir.TypeAnonymous {
		t.Fatalf("identity view: %+v", v.decls)
	}
	// Javac's span excludes the parser's trailing semicolon: containment still finds the field.
	if d, ok := v.match(ir.DeclarationField, "x", 20, 29); !ok || d.ID != "field" {
		t.Fatalf("contained: %+v", d)
	}
	// Javac's span starts before the parser's (annotations): the closest overlapping declaration wins.
	if d, ok := v.match(ir.DeclarationMethod, "m", 35, 120); !ok || d.ID != "method" {
		t.Fatalf("overlapping: %+v", d)
	}
	if _, ok := v.match(ir.DeclarationMethod, "m", 130, 140); ok {
		t.Fatal("disjoint span matched")
	}
	sym := v.sourceSymbol(&v.decls[1])
	if sym.ID != symbolID("C", "field") || sym.OwnerSymbolID != symbolID("C", "class") || sym.Source.Evidence.Lineage != "file:c" {
		t.Fatalf("source symbol from identities: %+v", sym)
	}
}

func TestViewCacheEvictsBeyondLimitAndKeepsPinned(t *testing.T) {
	f := newFixture(t, "", nil)
	var inputs []semantic.SourceInput
	for i := 0; i < 12; i++ {
		id := "F" + strconv.Itoa(i)
		inputs = append(inputs, f.addParsed(id, "main", "src/"+id+".java", []byte("class "+id+" {}"), true))
	}
	s := unitRun(t, f)
	pinned, err := s.views.pin("main", inputs[0])
	must(t, err)
	for _, in := range inputs[1:] {
		v, err := s.views.byBridgePath(bridgePath(setDirectory("main"), in.Source.Path))
		must(t, err)
		if v.fileID() != in.Source.FileID || len(v.decls) != 1 {
			t.Fatalf("view %s: %+v", in.Source.Path, v.decls)
		}
	}
	if len(s.views.entries) != 8 || len(s.views.order) != 8 {
		t.Fatalf("cache size %d", len(s.views.entries))
	}
	if _, ok := s.views.entries[viewKey("main", inputs[1].Source.Path)]; ok {
		t.Fatal("oldest entry survived eviction")
	}
	if v, err := s.views.byBridgePath(pinned.bridgePath); err != nil || v != pinned {
		t.Fatal("pinned view is served without a workspace read")
	}
	if _, err := s.views.byBridgePath("sources/unknown/src/X.java"); err == nil {
		t.Fatal("path outside the attributed sets accepted")
	}
	if _, err := s.views.byBridgePath(bridgePath(setDirectory("main"), "src/Missing.java")); err == nil {
		t.Fatal("file outside the inventory accepted")
	}
	// An unchanged file without a baseline falls back to syntax, then to an opaque view.
	in := inputs[2]
	in.Affected = false
	v, err := s.views.build("main", in)
	must(t, err)
	if v.file == nil {
		t.Fatal("syntax fallback for an unchanged file without identities")
	}
	delete(f.w.syntax, in.Source.FileID)
	v, err = s.views.build("main", in)
	must(t, err)
	if v.file != nil || len(v.decls) != 0 {
		t.Fatal("opaque view expected")
	}
}
