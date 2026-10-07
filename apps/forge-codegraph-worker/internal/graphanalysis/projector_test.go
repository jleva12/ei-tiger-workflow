package graphanalysis

import (
	"bytes"
	"context"
	"fmt"
	"strings"
	"testing"
	"unicode/utf8"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

type projected struct {
	facts []graph.Fact
	nodes map[string]graph.Node
	edges []graph.Edge
}

func (p projected) nodeKinds() map[string]int {
	out := map[string]int{}
	for _, n := range p.nodes {
		out[n.Kind]++
	}
	return out
}

func (p projected) edgesOf(kind string) []graph.Edge {
	var out []graph.Edge
	for _, e := range p.edges {
		if e.Kind == kind {
			out = append(out, e)
		}
	}
	return out
}

func (p projected) hasEdge(kind, source, target string) bool {
	for _, e := range p.edgesOf(kind) {
		if e.SourceID == source && e.TargetID == target {
			return true
		}
	}
	return false
}

func project(t testing.TB, f *fixture, files ...semantic.SourceInput) (projected, semantic.ProjectionResult) {
	t.Helper()
	p := projected{nodes: map[string]graph.Node{}}
	out, err := (Projector{}).Project(context.Background(), semantic.ProjectRequest{Run: f.run, Files: files}, f.ws, func(_ context.Context, fact graph.Fact) error {
		if err := fact.Validate(); err != nil {
			return err
		}
		p.facts = append(p.facts, fact)
		if fact.Node != nil {
			if _, dup := p.nodes[fact.Node.ID]; dup {
				t.Fatalf("node %s emitted twice", fact.Node.ID)
			}
			p.nodes[fact.Node.ID] = *fact.Node
		} else {
			p.edges = append(p.edges, *fact.Edge)
		}
		return nil
	})
	must(t, err)
	if int(out.Nodes) != len(p.nodes) || int(out.Edges) != len(p.edges) {
		t.Fatalf("result %+v disagrees with %d nodes and %d edges", out, len(p.nodes), len(p.edges))
	}
	return p, out
}

// projectorFixture wires hand-built lookups over the parsed fixture: resolved
// calls, an unresolved call, a lambda implementing Handler.handle, an
// override, an inheritance, a type use of an external class, plus a
// constructed and a derived symbol.
func projectorFixture(t *testing.T) (*fixture, semantic.SourceInput, *semantic.DeclarationKey) {
	t.Helper()
	ctx := context.Background()
	f := newFixture(t, "run-1")
	a := f.addFile("src/A.java", fixtureA)
	file := f.file(a)
	stringKey := &semantic.DeclarationKey{OwnerKey: "java.lang", Kind: ir.DeclarationClass, Name: "String", CanonicalSignature: "java.lang.String"}
	classA := f.declaration(a, ir.DeclarationClass, "A")
	must(t, f.ws.PutSymbols(ctx, []semantic.Symbol{
		{ID: "sym:external:String", Name: "String", Key: stringKey, External: &semantic.ExternalSymbol{ArtifactID: "jdk", ArtifactFingerprint: "fp"}},
		{ID: "sym:intrinsic:int", Name: "int", Intrinsic: &semantic.IntrinsicSymbol{Language: "java", Name: "int", DefinitionDigest: "sha256:int"}},
		{ID: "sym:constructed:String[]", Name: "String[]", Constructed: &semantic.ConstructedSymbol{Language: "java", Kind: "array", CanonicalSignature: "java.lang.String[]", ComponentSymbolIDs: []string{"sym:external:String"}, DefinitionDigest: "sha256:array"}},
		{ID: "sym:derived:A.<init>", Name: "A", Key: &semantic.DeclarationKey{OwnerKey: "demo.A", Kind: ir.DeclarationConstructor, Name: "A", CanonicalSignature: "demo.A.A()"}, Derived: &semantic.DerivedSymbol{Language: "java", Rule: "implicit_constructor", SourceSymbolIDs: []string{f.symbolID(a, classA.ID)}, DefinitionDigest: "sha256:ctor"}},
	}))
	f.match(a)

	aRun := f.method(a, "A", "run")
	baseRun := f.method(a, "Base", "run")
	helper := f.method(a, "A", "helper")
	handle := f.method(a, "Handler", "handle")
	base := f.declaration(a, ir.DeclarationClass, "Base")
	lookup := func(id string, o ir.Occurrence, kind semantic.LookupKind, symbol string) semantic.Lookup {
		return semantic.Lookup{ID: id, FileID: a.Source.FileID, OccurrenceID: o.ID, Kind: kind, Status: semantic.LookupResolved, SelectedSymbolID: symbol, Evidence: anchor(a, o.Span)}
	}
	var lookups []semantic.Lookup
	for i, c := range file.Calls {
		switch c.Name {
		case "helper":
			lookups = append(lookups, lookup("lk:call:"+string(c.Occurrence.ID), c.Occurrence, semantic.LookupCall, f.symbolID(a, helper.ID)))
		case "unknown":
			lookups = append(lookups, semantic.Lookup{ID: "lk:unknown", FileID: a.Source.FileID, OccurrenceID: c.Occurrence.ID, Kind: semantic.LookupCall, Status: semantic.LookupUnresolved, Reason: "cannot find symbol", Cause: semantic.CauseSourceDiagnostic, DiagnosticCode: "compiler.err.cant.resolve", Evidence: anchor(a, c.Occurrence.Span)})
		default:
			t.Fatalf("unexpected call %d: %q", i, c.Name)
		}
	}
	if len(file.Lambdas) != 1 {
		t.Fatalf("fixture has %d lambdas", len(file.Lambdas))
	}
	lookups = append(lookups, lookup("lk:lambda", file.Lambdas[0].Occurrence, semantic.LookupImplements, f.symbolID(a, handle.ID)))
	lookups = append(lookups, semantic.Lookup{ID: "lk:override", FileID: a.Source.FileID, DeclarationID: aRun.ID, Kind: semantic.LookupOverride, Status: semantic.LookupResolved, SelectedSymbolID: f.symbolID(a, baseRun.ID), Evidence: anchor(a, *aRun.NameSpan)})
	types := map[ir.TypeRefID]ir.TypeRef{}
	for _, tr := range file.Types {
		types[tr.ID] = tr
	}
	heritage, stringUse := 0, 0
	for _, u := range file.TypeUses {
		switch {
		case u.Role == ir.TypeUseHeritage:
			heritage++
			lookups = append(lookups, lookup("lk:extends", u.Occurrence, semantic.LookupInheritance, f.symbolID(a, base.ID)))
		case u.Role == ir.TypeUseParameter && types[u.TypeRefID].Spelling == "String":
			stringUse++
			lookups = append(lookups, lookup("lk:string", u.Occurrence, semantic.LookupType, "sym:external:String"))
		}
	}
	if heritage != 1 || stringUse != 1 {
		t.Fatalf("fixture has %d heritage and %d String parameter type uses", heritage, stringUse)
	}
	must(t, f.ws.PutLookups(ctx, a.Source.FileID, lookups))
	return f, a, stringKey
}

func TestProjectorEmitsDeclarationsSitesAndLookups(t *testing.T) {
	f, a, stringKey := projectorFixture(t)
	p, _ := project(t, f, a)
	ids := f.identities(a)
	kinds := p.nodeKinds()

	if kinds[graph.NodeSourceFile] != 1 || p.nodes[a.Lineage].Kind != graph.NodeSourceFile || graph.Text(p.nodes[a.Lineage].Properties, "file_path") != "src/A.java" {
		t.Fatalf("source_file node: %v", kinds)
	}
	if kinds["interface"] != 1 || kinds["class"] != 2 || kinds["method"] != 5 || kinds["parameter"] < 1 {
		t.Fatalf("declaration nodes: %v", kinds)
	}
	if kinds[NodeImport] != 1 || kinds[NodeAnnotation] != 1 || kinds[NodeTypeUse] == 0 {
		t.Fatalf("site nodes for occurrences without lookups: %v", kinds)
	}
	if kinds[NodeCallSite] != 0 || kinds[NodeLambda] != 0 {
		t.Fatalf("occurrences with lookups must not get site nodes: %v", kinds)
	}

	// Lineage-less symbols.
	external := graph.ID("external", "repo", "jdk", "fp", CanonicalKey(stringKey))
	if n, ok := p.nodes[external]; !ok || n.Kind != graph.NodeExternalSymbol || n.Source != nil || n.QualifiedName != "java.lang.String" || graph.Text(n.Properties, "artifact_id") != "jdk" || graph.Text(n.Properties, "declaration_kind") != "class" {
		t.Fatalf("external symbol node: %+v", p.nodes[external])
	}
	intrinsic := graph.ID("intrinsic", "java", "int", "sha256:int")
	if n, ok := p.nodes[intrinsic]; !ok || n.Kind != graph.NodeIntrinsic || n.Name != "int" || graph.Text(n.Properties, "definition_digest") != "sha256:int" {
		t.Fatalf("intrinsic node: %+v", p.nodes[intrinsic])
	}
	constructed := graph.ID("constructed", "repo", "java", "array", "java.lang.String[]", "sha256:array", external)
	if n, ok := p.nodes[constructed]; !ok || n.Kind != graph.NodeConstructedType || graph.Text(n.Properties, "type_kind") != "array" || !p.hasEdge(graph.EdgeTypeComponent, constructed, external) {
		t.Fatalf("constructed type node: %+v", p.nodes[constructed])
	}
	classA := f.entity(a, ir.DeclarationClass, "A")
	ctorKey := &semantic.DeclarationKey{OwnerKey: "demo.A", Kind: ir.DeclarationConstructor, Name: "A", CanonicalSignature: "demo.A.A()"}
	derived := graph.ID("derived", "repo", "java", "implicit_constructor", "sha256:ctor", CanonicalKey(ctorKey), classA)
	if n, ok := p.nodes[derived]; !ok || n.Kind != "derived_constructor" || graph.Text(n.Properties, "derivation_rule") != "implicit_constructor" || !p.hasEdge(graph.EdgeDerivedFrom, derived, classA) {
		t.Fatalf("derived symbol node: %+v", p.nodes[derived])
	}
	for _, e := range p.edgesOf(graph.EdgeDerivedFrom) {
		if e.Source != nil {
			t.Fatal("derived_from edges must stay lineage-less")
		}
	}

	// Structure and search properties.
	helper := f.methodEntity(a, "A", "helper")
	if n := p.nodes[helper]; n.Kind != "method" || n.QualifiedName != "demo.A.helper()" || graph.Text(n.Properties, "source_text") != "void helper() {}" || graph.Text(n.Properties, "signature") == "" || graph.Text(n.Properties, "search_document_hash") == "" {
		t.Fatalf("helper node: %+v", n)
	}
	if doc, ok := codesearch.FromNode(p.nodes[helper]); !ok || doc.Hash != graph.Text(p.nodes[helper].Properties, "search_document_hash") {
		t.Fatal("helper search document is not sealed")
	}
	if _, ok := p.nodes[classA].Properties["source_text"]; ok {
		t.Fatal("type nodes must not repeat their members' code")
	}
	if !p.hasEdge(graph.EdgeContains, a.Lineage, classA) || !p.hasEdge(graph.EdgeContains, classA, helper) {
		t.Fatal("contains edges from file to type and type to member are missing")
	}

	// Lookups.
	aRun, baseRun := f.methodEntity(a, "A", "run"), f.methodEntity(a, "Base", "run")
	overrides := p.edgesOf(graph.EdgeOverrides)
	if len(overrides) != 1 || overrides[0].SourceID != aRun || overrides[0].TargetID != baseRun || overrides[0].ID != graph.ID("edge", graph.EdgeOverrides, aRun, baseRun, "") || overrides[0].Source == nil || overrides[0].Source.Span != *f.method(a, "A", "run").NameSpan {
		t.Fatalf("overrides edge: %+v", overrides)
	}
	if _, ok := overrides[0].Properties["occurrence_id"]; ok || graph.Text(overrides[0].Properties, "status") != "resolved" {
		t.Fatalf("override properties: %+v", overrides[0].Properties)
	}
	lambda := f.file(a).Lambdas[0].Occurrence
	var lambdaSite semantic.OccurrenceIdentity
	for _, o := range ids.Occurrences {
		if o.OccurrenceID == lambda.ID {
			lambdaSite = o
		}
	}
	handle := f.methodEntity(a, "Handler", "handle")
	implements := p.edgesOf(graph.EdgeImplements)
	if len(implements) != 1 || implements[0].SourceID != lambdaSite.EnclosingEntityID || implements[0].TargetID != handle || implements[0].ID != graph.ID("edge", graph.EdgeImplements, lambdaSite.EnclosingEntityID, handle, lambdaSite.PersistentID) || graph.Text(implements[0].Properties, "occurrence_kind") != NodeLambda || graph.Text(implements[0].Properties, "occurrence_id") != lambdaSite.PersistentID {
		t.Fatalf("implements edge: %+v", implements)
	}
	calls := p.edgesOf(graph.EdgeCalls)
	if len(calls) != 2 {
		t.Fatalf("calls edges: %+v", calls)
	}
	for _, e := range calls {
		if e.TargetID != helper || graph.Text(e.Properties, "occurrence_kind") != NodeCallSite {
			t.Fatalf("calls edge: %+v", e)
		}
	}
	if !p.hasEdge(graph.EdgeInherits, classA, f.entity(a, ir.DeclarationClass, "Base")) {
		t.Fatal("inherits edge is missing")
	}
	// The String type use is enclosed by the parameter it types, so the
	// uses_type edge starts at that parameter's entity.
	usesType := p.edgesOf(graph.EdgeUsesType)
	if len(usesType) != 1 || usesType[0].TargetID != external || p.nodes[usesType[0].SourceID].Kind != "parameter" || !p.hasEdge(graph.EdgeContains, handle, usesType[0].SourceID) {
		t.Fatalf("uses_type edge to the external class: %+v", usesType)
	}
	unresolved := 0
	for _, n := range p.nodes {
		if n.Kind != graph.NodeUnresolvedReference {
			continue
		}
		unresolved++
		props := n.Properties
		if graph.Text(props, "lookup_kind") != "call" || graph.Text(props, "status") != "unresolved" || graph.Text(props, "reason") != "cannot find symbol" || graph.Text(props, "cause") != "source_diagnostic" || graph.Text(props, "diagnostic_code") != "compiler.err.cant.resolve" || graph.Text(props, "occurrence_kind") != NodeCallSite {
			t.Fatalf("unresolved reference: %+v", props)
		}
		if n.ID != graph.ID("lookup", graph.Text(props, "occurrence_id"), "call") || !p.hasEdge(graph.EdgeContains, f.methodEntity(a, "A", "missing"), n.ID) {
			t.Fatalf("unresolved reference is detached: %+v", n)
		}
	}
	if unresolved != 1 {
		t.Fatalf("%d unresolved references, want 1", unresolved)
	}

	// Every anchored fact points at this file variant; lineage-less facts
	// carry no anchor at all.
	for _, fact := range p.facts {
		a2 := fact.Anchor()
		id := fact.Key().ID
		lineageless := strings.HasPrefix(id, "external:") || strings.HasPrefix(id, "intrinsic:") || strings.HasPrefix(id, "constructed:") || strings.HasPrefix(id, "derived:") || (fact.Edge != nil && (fact.Edge.Kind == graph.EdgeDerivedFrom || fact.Edge.Kind == graph.EdgeTypeComponent))
		if lineageless != (a2 == nil) {
			t.Fatalf("anchor presence of %s: %v", id, a2)
		}
		if a2 != nil && (a2.Lineage != a.Lineage || a2.ContentSHA256 != a.Source.ContentSHA256) {
			t.Fatalf("fact %s anchored elsewhere: %+v", id, a2)
		}
	}

	// On an initial run the differ turns every fact into an add.
	var applied []graph.Change
	d := &Differ{Repo: "repo", Apply: func(_ context.Context, c []graph.Change) error { applied = append(applied, c...); return nil }}
	for _, fact := range p.facts {
		must(t, d.Emit(context.Background(), fact))
	}
	must(t, d.Finish(context.Background()))
	if len(applied) != len(p.facts) {
		t.Fatalf("%d changes for %d facts", len(applied), len(p.facts))
	}
	for _, c := range applied {
		if c.Op != graph.OpAdd || (c.After.Anchor() == nil) != (c.Lineage == "") || (c.Lineage != "" && c.Lineage != a.Lineage) {
			t.Fatalf("initial change: %+v", c)
		}
	}
}

func TestProjectorEmitsFileFactsInOrderAndRejectsMissingIdentities(t *testing.T) {
	f, a, _ := projectorFixture(t)
	p, _ := project(t, f, a)
	first := -1
	for i, fact := range p.facts {
		if fact.Anchor() != nil {
			first = i
			break
		}
	}
	if first < 0 || p.facts[first].Node == nil || p.facts[first].Node.Kind != graph.NodeSourceFile {
		t.Fatal("a file's facts must start with its source_file node")
	}
	for _, fact := range p.facts[:first] {
		if fact.Anchor() != nil {
			t.Fatal("lineage-less facts must precede file facts")
		}
	}
	b := f.addFile("src/B.java", "package demo; class B {}")
	if _, err := (Projector{}).Project(context.Background(), semantic.ProjectRequest{Run: f.run, Files: []semantic.SourceInput{b}}, f.ws, func(context.Context, graph.Fact) error { return nil }); err == nil {
		t.Fatal("projection without identities must fail")
	}
}

func TestLargeMethodTextIsChunkedWithoutTruncation(t *testing.T) {
	content := []byte(strings.Repeat("event(\"世\");\n", 9000))
	in := semantic.SourceInput{Source: ir.Source{Path: "Events.java", Language: "java", ContentSHA256: strings.Repeat("a", 64), SizeBytes: uint64(len(content))}, Lineage: graph.Lineage("repo", "app", "main", "Events.java")}
	d := ir.Declaration{ID: "d1", Kind: ir.DeclarationMethod, Span: ir.Span{Start: ir.Position{Line: 1}, End: ir.Position{ByteOffset: uint64(len(content)), Line: 9001}}}
	n := graph.Node{ID: graph.ID("entity", "events"), Name: "events", Kind: "method", Properties: map[string]graph.PropertyValue{}}
	var restored strings.Builder
	chunks, edges := 0, 0
	must(t, retainDeclarationText(context.Background(), &n, in, d, &lineIndex{content: content}, func(_ context.Context, f graph.Fact) error {
		must(t, f.Validate())
		if f.Node != nil {
			text := codesearch.Property(*f.Node, "source_text")
			if len(text) > codesearch.MaxChunkBytes || !utf8.ValidString(text) {
				t.Fatal("invalid text window")
			}
			if string(content[f.Node.Source.Span.Start.ByteOffset:f.Node.Source.Span.End.ByteOffset]) != text {
				t.Fatal("chunk span does not address its exact code")
			}
			if doc, ok := codesearch.FromNode(*f.Node); !ok || doc.Hash != codesearch.Property(*f.Node, "search_document_hash") {
				t.Fatal("chunk has no stable search document")
			}
			if f.Node.Source.Lineage != in.Lineage || f.Node.Source.Span.Start.Line == 0 {
				t.Fatalf("chunk anchor: %+v", f.Node.Source)
			}
			restored.WriteString(text)
			chunks++
		} else {
			if f.Edge.Kind != graph.EdgeHasChunk || f.Edge.SourceID != n.ID {
				t.Fatal("chunk is detached from its method")
			}
			edges++
		}
		return nil
	}))
	if restored.String() != string(content) || chunks < 2 || edges != chunks {
		t.Fatal("large method text was truncated or disconnected")
	}
	if _, ok := n.Properties["source_text"]; ok || n.Properties["source_text_chunked"].Bool == nil {
		t.Fatal("chunked method must not also retain its whole text")
	}
}

// Resolver output is not trusted to be storable: a diagnostic can quote
// source bytes, a generated API can offer thousands of candidates, a site can
// hold several unresolved lookups of one kind, and an inferred type can have
// a signature of any length. Every one of them still projects.
func TestProjectorKeepsHostileLookupsStorable(t *testing.T) {
	ctx := context.Background()
	f, a, _ := projectorFixture(t)
	huge := "com.acme.Gen<" + strings.Repeat("Map<String,\x00List<\xff>>,", 20000) + ">"
	var candidates []string
	var symbols []semantic.Symbol
	for i := 0; i < 300; i++ {
		id := fmt.Sprintf("sym:external:Overload%d", i)
		candidates = append(candidates, id)
		symbols = append(symbols, semantic.Symbol{ID: id, Name: "call", Key: &semantic.DeclarationKey{OwnerKey: "api.Client", Kind: ir.DeclarationMethod, Name: "call", CanonicalSignature: fmt.Sprintf("api.Client.call(T%d)", i)}, External: &semantic.ExternalSymbol{ArtifactID: "api", ArtifactFingerprint: "fp"}})
	}
	symbols = append(symbols, semantic.Symbol{ID: "sym:external:Huge", Name: huge, Key: &semantic.DeclarationKey{OwnerKey: huge, Kind: ir.DeclarationClass, Name: huge, CanonicalSignature: huge}, External: &semantic.ExternalSymbol{ArtifactID: "api", ArtifactFingerprint: "fp"}})
	must(t, f.ws.PutSymbols(ctx, symbols))

	aRun := f.method(a, "A", "run")
	unresolved := func(id, reason string, ids []string) semantic.Lookup {
		return semantic.Lookup{ID: id, FileID: a.Source.FileID, DeclarationID: aRun.ID, Kind: semantic.LookupOverride, Status: semantic.LookupAmbiguous, Reason: reason, CandidateIDs: ids, CandidateRole: "binding_alternative", Evidence: anchor(a, *aRun.NameSpan)}
	}
	lookups, err := f.ws.Lookups(ctx, a.Source.FileID)
	must(t, err)
	lookups = append(lookups,
		unresolved("lk:many", "ambiguous: "+strings.Repeat("\xfe\x00quoted source ", 1000), candidates),
		unresolved("lk:huge", "no such supertype", []string{"sym:external:Huge", "sym:external:Huge"}),
	)
	must(t, f.ws.PutLookups(ctx, a.Source.FileID, lookups))

	p, _ := project(t, f, a)
	entity := f.methodEntity(a, "A", "run")
	var sited []graph.Node
	for _, n := range p.nodes {
		if n.Kind == graph.NodeUnresolvedReference && p.hasEdge(graph.EdgeContains, entity, n.ID) {
			sited = append(sited, n)
		}
	}
	if len(sited) != 2 || sited[0].ID == sited[1].ID {
		t.Fatalf("two lookups at one declaration need two nodes: %+v", sited)
	}
	for _, n := range sited {
		reason, targets := graph.Text(n.Properties, "reason"), n.Properties["candidate_target_ids"].Strings
		if len(reason) > maxReasonBytes || strings.IndexByte(reason, 0) >= 0 || !utf8.ValidString(reason) || targets == nil {
			t.Fatalf("reason or candidates not storable: %d bytes", len(reason))
		}
		switch len(*targets) {
		case maxCandidateTargets:
			if c := n.Properties["candidate_count"].Int64; c == nil || *c != 300 {
				t.Fatalf("a cut candidate list keeps its full count: %v", n.Properties["candidate_count"])
			}
		case 1: // the same candidate twice is listed once
		default:
			t.Fatalf("candidates: %d", len(*targets))
		}
	}
	for _, n := range p.nodes {
		if len(n.Name) > maxKeyBytes || len(n.QualifiedName) > maxKeyBytes {
			t.Fatalf("node %s keeps an unbounded name", n.ID)
		}
		if n.Kind == graph.NodeExternalSymbol && strings.HasPrefix(n.Name, "com.acme.Gen<") && (len(n.Name) > maxNameBytes || len(graph.Text(n.Properties, "owner_key")) > maxKeyBytes) {
			t.Fatalf("external name %d bytes, owner %d bytes", len(n.Name), len(graph.Text(n.Properties, "owner_key")))
		}
	}
}

// Bytes that are not UTF-8 have no character boundary: chunking must still
// make progress, and every chunk must be storable text.
func TestChunkingSurvivesBytesThatAreNotText(t *testing.T) {
	content := append(bytes.Repeat([]byte{0x80}, 3*codesearch.MaxChunkBytes), []byte("\x00tail\n")...)
	in := semantic.SourceInput{Source: ir.Source{Path: "Blob.java", Language: "java", ContentSHA256: strings.Repeat("a", 64), SizeBytes: uint64(len(content))}, Lineage: graph.Lineage("repo", "app", "main", "Blob.java")}
	d := ir.Declaration{ID: "d1", Kind: ir.DeclarationField, Span: ir.Span{Start: ir.Position{Line: 1}, End: ir.Position{ByteOffset: uint64(len(content)), Line: 2}}}
	n := graph.Node{ID: graph.ID("entity", "blob"), Name: "blob\xff", Kind: "field", Properties: map[string]graph.PropertyValue{}}
	cleanNode(&n)
	chunks := 0
	must(t, retainDeclarationText(context.Background(), &n, in, d, &lineIndex{content: content}, func(_ context.Context, f graph.Fact) error {
		if err := f.Validate(); err != nil {
			return err
		}
		if f.Node != nil {
			chunks++
			if doc, ok := codesearch.FromNode(*f.Node); !ok || doc.Hash != codesearch.Property(*f.Node, "search_document_hash") {
				t.Fatal("a cleaned chunk must be sealed over its stored text")
			}
		}
		if chunks > 10 {
			t.Fatal("chunking does not make progress")
		}
		return nil
	}))
	if chunks != 4 {
		t.Fatalf("%d chunks", chunks)
	}
}
