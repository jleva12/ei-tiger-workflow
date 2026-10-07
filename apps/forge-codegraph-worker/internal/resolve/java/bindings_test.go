package java

import (
	"context"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// unitRun builds a run over the fixture without launching javac.
func unitRun(t *testing.T, f *fixture) *run {
	t.Helper()
	r := &Resolver{config: Config{JavaHome: "/jdk", WorkDir: t.TempDir(), CacheDir: t.TempDir(), MaxHeapMiB: 512, Parallelism: 1}, javaHome: "/jdk", fingerprints: fingerprintCache{entries: map[fingerprintKey]string{}}}
	s, err := r.newRun(context.Background(), f.req, f.w, t.TempDir())
	must(t, err)
	return s
}

func span(start, end uint64) ir.Span {
	return ir.Span{Start: ir.Position{ByteOffset: start, Line: 1}, End: ir.Position{ByteOffset: end, Line: 1}}
}

// declEvent describes a source declaration the way the bridge does.
func declEvent(path string, d ir.Declaration, elementKind, owner, signature string) event {
	return event{Kind: "declaration", Path: path, Start: int64(d.Span.Start.ByteOffset), End: int64(d.Span.End.ByteOffset), ElementKind: elementKind, Name: d.Name, Owner: owner, Signature: signature, SignatureValid: "true"}
}

func sourceTargetEvent(kind, path string, o ir.Occurrence, tree, elementKind, name, owner, signature string, target ir.Declaration) event {
	return event{Kind: kind, Path: path, Start: int64(o.Span.Start.ByteOffset), End: int64(o.Span.End.ByteOffset), TreeKind: tree, ElementKind: elementKind, Name: name, Owner: owner, Signature: signature, Status: "resolved", TargetPath: path, TargetStart: int64(target.Span.Start.ByteOffset), TargetEnd: int64(target.Span.End.ByteOffset)}
}

func findOccurrence(t *testing.T, file ir.SourceFile, content []byte, text string) ir.Occurrence {
	t.Helper()
	all := []ir.Occurrence{}
	for _, c := range file.Calls {
		all = append(all, c.Occurrence)
	}
	for _, r := range file.References {
		all = append(all, r.Occurrence)
	}
	for _, u := range file.TypeUses {
		all = append(all, u.Occurrence)
	}
	for _, l := range file.Lambdas {
		all = append(all, l.Occurrence)
	}
	for _, r := range file.CallableReferences {
		all = append(all, r.Occurrence)
	}
	for _, o := range all {
		if string(content[o.Span.Start.ByteOffset:o.Span.End.ByteOffset]) == text {
			return o
		}
	}
	t.Fatalf("no occurrence %q", text)
	return ir.Occurrence{}
}

func lookupOf(t *testing.T, lookups []semantic.Lookup, kind semantic.LookupKind, occurrence ir.OccurrenceID) semantic.Lookup {
	t.Helper()
	for _, l := range lookups {
		if l.Kind == kind && l.OccurrenceID == occurrence {
			return l
		}
	}
	t.Fatalf("no %s lookup for occurrence %s", kind, occurrence)
	return semantic.Lookup{}
}

func TestResolveFileMapsEventsToLookups(t *testing.T) {
	f := newFixture(t, "", nil)
	src := []byte(`class A { int x; int id(int v) { return v; } long id(long v) { return v; } void run() { id(1); x = 2; String s = null; missing(); orphan(); Runnable r = () -> run(); Runnable m = this::run; } }`)
	in := f.addParsed("A", "main", "src/A.java", src, true)
	s := unitRun(t, f)
	view, err := s.views.pin("main", in)
	must(t, err)
	defer s.views.unpin()
	file := f.files["A"]
	path := view.bridgePath
	s.origins["jrt:/java.base/java/lang/String.class"] = origin{artifactID: "jdk21", fingerprint: "sha256:" + strings.Repeat("a", 64)}
	s.origins["jrt:/java.base/java/lang/Runnable.class"] = origin{artifactID: "jdk21", fingerprint: "sha256:" + strings.Repeat("a", 64)}

	classA := f.declaration("A", ir.DeclarationClass, "A")
	fieldX := f.declaration("A", ir.DeclarationField, "x")
	run := f.declaration("A", ir.DeclarationMethod, "run")
	var idInt, idLong ir.Declaration
	for _, d := range file.Declarations {
		if d.Kind == ir.DeclarationMethod && d.Name == "id" {
			if strings.Contains(string(src[d.Span.Start.ByteOffset:d.Span.End.ByteOffset]), "int v") {
				idInt = d
			} else {
				idLong = d
			}
		}
	}
	fe := newFileEvents(path)
	fe.add(declEvent(path, classA, "CLASS", "", "A"))
	fe.add(declEvent(path, fieldX, "FIELD", "A", "x:int"))
	fe.add(declEvent(path, idInt, "METHOD", "A", "id(int)"))
	fe.add(declEvent(path, idLong, "METHOD", "A", "id(long)"))
	fe.add(declEvent(path, run, "METHOD", "A", "run()"))
	call := findOccurrence(t, file, src, "id(1)")
	fe.add(sourceTargetEvent("binding", path, call, "METHOD_INVOCATION", "METHOD", "id", "A", "id(int)", idInt))
	fe.add(sourceTargetEvent("candidate", path, call, "METHOD_INVOCATION", "METHOD", "id", "A", "id(int)", idInt))
	fe.add(sourceTargetEvent("candidate", path, call, "METHOD_INVOCATION", "METHOD", "id", "A", "id(long)", idLong))
	ref := findOccurrence(t, file, src, "x")
	fe.add(sourceTargetEvent("binding", path, ref, "IDENTIFIER", "FIELD", "x", "A", "x:int", fieldX))
	str := findOccurrence(t, file, src, "String")
	fe.add(event{Kind: "binding", Path: path, Start: int64(str.Span.Start.ByteOffset), End: int64(str.Span.End.ByteOffset), TreeKind: "IDENTIFIER", ElementKind: "CLASS", Name: "String", Owner: "java.lang", Signature: "java.lang.String", Status: "resolved", ArtifactURI: "jrt:/java.base/java/lang/String.class"})
	missing := findOccurrence(t, file, src, "missing()")
	fe.add(event{Kind: "binding", Path: path, Start: int64(missing.Span.Start.ByteOffset), End: int64(missing.Span.End.ByteOffset), TreeKind: "METHOD_INVOCATION", Status: "unresolved"})
	fe.add(event{Kind: "diagnostic", Path: path, Start: int64(missing.Span.Start.ByteOffset), End: int64(missing.Span.Start.ByteOffset) + 7, Code: "compiler.err.cant.resolve.location.args", Severity: "ERROR", Message: "cannot find symbol\n  symbol: method missing()"})
	// A package qualifier is an environment, not a lookup.
	fe.add(event{Kind: "binding", Path: path, Start: int64(ref.Span.Start.ByteOffset), End: int64(ref.Span.End.ByteOffset), TreeKind: "IDENTIFIER", ElementKind: "PACKAGE"})
	// Override of run() over an external method, and the lambda/reference implementing Runnable.run.
	fe.add(event{Kind: "override", Path: path, Start: int64(run.Span.Start.ByteOffset), End: int64(run.Span.End.ByteOffset), TreeKind: "METHOD", ElementKind: "METHOD", Name: "run", Owner: "java.lang.Runnable", Signature: "run()", ArtifactURI: "jrt:/java.base/java/lang/Runnable.class"})
	lambda := findOccurrence(t, file, src, "() -> run()")
	reference := findOccurrence(t, file, src, "this::run")
	iface := &event{Kind: "interface", ElementKind: "INTERFACE", Name: "Runnable", Owner: "java.lang", Signature: "java.lang.Runnable", ArtifactURI: "jrt:/java.base/java/lang/Runnable.class"}
	for _, o := range []struct {
		occ  ir.Occurrence
		tree string
	}{{lambda, "LAMBDA_EXPRESSION"}, {reference, "MEMBER_REFERENCE"}} {
		fe.add(event{Kind: "implements", Path: path, Start: int64(o.occ.Span.Start.ByteOffset), End: int64(o.occ.Span.End.ByteOffset), TreeKind: o.tree, Status: "resolved", ElementKind: "METHOD", Name: "run", Owner: "java.lang.Runnable", Signature: "run()", ArtifactURI: "jrt:/java.base/java/lang/Runnable.class", Interface: iface})
	}
	fe.add(event{Kind: "implements", Path: path, Start: 1, End: 2, TreeKind: "LAMBDA_EXPRESSION", Status: "unsupported", Message: "functional interface target has 2 abstract methods"})

	lookups, err := s.resolveFile(view, fe)
	must(t, err)
	must(t, s.symbols.flush())

	l := lookupOf(t, lookups, semantic.LookupCall, call.ID)
	if l.Status != semantic.LookupResolved || l.SelectedSymbolID != symbolID("A", idInt.ID) || len(l.CandidateIDs) != 2 || l.Evidence.Lineage != in.Lineage || l.Evidence.Span != call.Span {
		t.Fatalf("call: %+v", l)
	}
	if l := lookupOf(t, lookups, semantic.LookupMember, ref.ID); l.SelectedSymbolID != symbolID("A", fieldX.ID) {
		t.Fatalf("member: %+v", l)
	}
	l = lookupOf(t, lookups, semantic.LookupType, str.ID)
	sym, ok := f.w.symbols[l.SelectedSymbolID]
	if !ok || sym.External == nil || sym.External.ArtifactID != "jdk21" || sym.Key == nil || sym.Key.Kind != ir.DeclarationClass {
		t.Fatalf("external type: %+v %+v", l, sym)
	}
	l = lookupOf(t, lookups, semantic.LookupCall, missing.ID)
	if l.Status != semantic.LookupUnresolved || l.Cause != semantic.CauseSourceDiagnostic || l.DiagnosticCode != "compiler.err.cant.resolve.location.args" || !strings.HasPrefix(l.Reason, "compiler_error: ") || strings.Contains(l.Reason, "\n") {
		t.Fatalf("diagnostic: %+v", l)
	}
	orphan := findOccurrence(t, file, src, "orphan()")
	if l := lookupOf(t, lookups, semantic.LookupCall, orphan.ID); l.Status != semantic.LookupUnsupported || l.Cause != semantic.CauseAnalysisLimitation {
		t.Fatalf("unmapped occurrence: %+v", l)
	}
	for _, l := range lookups {
		if l.Kind == semantic.LookupMember && l.OccurrenceID == ref.ID && l.Status != semantic.LookupResolved {
			t.Fatalf("package pseudo-binding leaked: %+v", l)
		}
	}
	var overrides, implements []semantic.Lookup
	for _, l := range lookups {
		switch l.Kind {
		case semantic.LookupOverride:
			overrides = append(overrides, l)
		case semantic.LookupImplements:
			implements = append(implements, l)
		}
	}
	if len(overrides) != 1 || overrides[0].DeclarationID != run.ID || overrides[0].OccurrenceID != "" || overrides[0].Status != semantic.LookupResolved || f.w.symbols[overrides[0].SelectedSymbolID].External == nil {
		t.Fatalf("override: %+v", overrides)
	}
	if got := string(src[overrides[0].Evidence.Span.Start.ByteOffset:overrides[0].Evidence.Span.End.ByteOffset]); got != "run" {
		t.Fatalf("override evidence %q", got)
	}
	if len(implements) != 2 {
		t.Fatalf("implements (unsupported event without a site must be skipped): %+v", implements)
	}
	for _, l := range implements {
		if l.Status != semantic.LookupResolved || f.w.symbols[l.SelectedSymbolID].Key.CanonicalSignature != "run()" || (l.OccurrenceID != lambda.ID && l.OccurrenceID != reference.ID) {
			t.Fatalf("implements: %+v", l)
		}
		tl := lookupOf(t, lookups, semantic.LookupType, l.OccurrenceID)
		if tl.Status != semantic.LookupResolved || f.w.symbols[tl.SelectedSymbolID].Name != "Runnable" || tl.ID == l.ID {
			t.Fatalf("functional interface type: %+v", tl)
		}
	}
	ids := map[string]bool{}
	for _, l := range lookups {
		if ids[l.ID] {
			t.Fatalf("duplicate lookup ID %s", l.ID)
		}
		ids[l.ID] = true
	}
}

// Targets in other files: an affected file resolves through its syntax; an
// unchanged one through the previous generation's identity map.
func TestTargetMappingUsesSyntaxOrPreviousIdentities(t *testing.T) {
	f := newFixture(t, "", nil)
	a := []byte(`package p; class A { void run() { new B().work(); new C().help(); } }`)
	b := []byte(`package p; class B { void work() {} }`)
	inA := f.addParsed("A", "main", "src/p/A.java", a, true)
	f.addParsed("B", "main", "src/p/B.java", b, true)
	// C is unchanged: no syntax in the cache, only its previous identities.
	c := []byte(`package p; class C { void help() { class Local { void help() {} } } }`)
	src := ir.Source{FileID: "C", RepositoryID: "repo", SnapshotID: f.build.SnapshotID, Path: "src/p/C.java", ContentSHA256: rawSum(c), SizeBytes: uint64(len(c)), Language: "java", LanguageVersion: "21", ModuleID: "app", SourceSetID: "main"}
	inC := semantic.SourceInput{Source: src, Lineage: "file:c-lineage"}
	f.w.files = append(f.w.files, inC)
	f.w.bytes["C"] = c
	helpStart := uint64(strings.Index(string(c), "void help() { class"))
	helpEnd := uint64(strings.LastIndex(string(c), "}")) // encloses the local class
	localHelp := uint64(strings.Index(string(c), "void help() {}"))
	f.w.previous[inC.Lineage] = semantic.FileIdentities{Lineage: inC.Lineage, FileID: "C", Path: src.Path, ContentSHA256: src.ContentSHA256, Declarations: []semantic.DeclarationIdentity{
		{DeclarationID: "c-class", EntityID: "e1", Kind: ir.DeclarationClass, Name: "C", Span: span(11, uint64(len(c)))},
		{DeclarationID: "c-help", EntityID: "e2", Kind: ir.DeclarationMethod, Name: "help", Span: span(helpStart, helpEnd), OwnerID: "c-class"},
		{DeclarationID: "c-local", EntityID: "e3", Kind: ir.DeclarationClass, Name: "Local", Span: span(helpStart+14, helpEnd-2), OwnerID: "c-help"},
		{DeclarationID: "c-local-help", EntityID: "e4", Kind: ir.DeclarationMethod, Name: "help", Span: span(localHelp, localHelp+14), OwnerID: "c-local"},
	}}
	s := unitRun(t, f)
	view, err := s.views.pin("main", inA)
	must(t, err)
	defer s.views.unpin()
	fileA := f.files["A"]
	work := findOccurrence(t, fileA, a, "new B().work()")
	help := findOccurrence(t, fileA, a, "new C().help()")
	workDecl := f.declaration("B", ir.DeclarationMethod, "work")
	pathB := bridgePath(setDirectory("main"), "src/p/B.java")
	pathC := bridgePath(setDirectory("main"), "src/p/C.java")
	fe := newFileEvents(view.bridgePath)
	fe.add(event{Kind: "binding", Path: view.bridgePath, Start: int64(work.Span.Start.ByteOffset), End: int64(work.Span.End.ByteOffset), TreeKind: "METHOD_INVOCATION", ElementKind: "METHOD", Name: "work", Owner: "p.B", Signature: "work()", Status: "resolved", TargetPath: pathB, TargetStart: int64(workDecl.Span.Start.ByteOffset), TargetEnd: int64(workDecl.Span.End.ByteOffset)})
	fe.add(event{Kind: "binding", Path: view.bridgePath, Start: int64(help.Span.Start.ByteOffset), End: int64(help.Span.End.ByteOffset), TreeKind: "METHOD_INVOCATION", ElementKind: "METHOD", Name: "help", Owner: "p.C", Signature: "help()", Status: "resolved", TargetPath: pathC, TargetStart: int64(helpStart), TargetEnd: int64(helpEnd)})
	lookups, err := s.resolveFile(view, fe)
	must(t, err)
	if l := lookupOf(t, lookups, semantic.LookupCall, work.ID); l.SelectedSymbolID != symbolID("B", workDecl.ID) {
		t.Fatalf("affected target: %+v", l)
	}
	if l := lookupOf(t, lookups, semantic.LookupCall, help.ID); l.SelectedSymbolID != symbolID("C", "c-help") {
		t.Fatalf("unchanged target must be the outer help, not the local class member: %+v", l)
	}
	if len(s.views.entries) != 2 {
		t.Fatalf("view cache holds %d entries", len(s.views.entries))
	}
}

// Bytecode from a reactor output maps back to the producing set's source by
// compiler signature, whether the producer was attributed in this run
// (indexed from its declaration events) or not (previous identity keys).
func TestReactorOutputMapsToSourceBySignature(t *testing.T) {
	f := newFixture(t, "", func(in *bc.Inventory, checks *[]bc.InputCheck) {
		in.Inputs = append(in.Inputs, bc.Input{ID: "main-output", Kind: bc.InputClasses, Location: &bc.Location{Root: "checkout", Path: "target/classes"}, SHA256: strings.Repeat("0", 64)})
		*checks = append(*checks, bc.InputCheck{InputID: "main-output", Status: bc.Available, ObservedSHA256: strings.Repeat("0", 64)})
		in.SourceSets[0].OutputInputID = "main-output"
	})
	a := []byte(`package p; public class A { public A() {} public int run(String v) { return 1; } public enum Mode { ON } }`)
	b := []byte(`package p; class B { int work() { A.Mode.valueOf("ON"); return new A().run("x"); } }`)
	inA := f.addParsed("A", "main", "src/p/A.java", a, true)
	inB := f.addParsed("B", "test", "src/p/B.java", b, true)
	f.req.Contexts = []bc.SourceSetID{"test", "main"}
	s := unitRun(t, f)
	if s.contexts[0].set.ID != "main" || s.contexts[1].set.ID != "test" {
		t.Fatalf("producer must be attributed before consumer: %v %v", s.contexts[0].set.ID, s.contexts[1].set.ID)
	}
	output := "/out/main"
	s.outputs[output] = s.sets["main"]
	if !s.keys.indexes("main") || s.keys.indexes("test") {
		t.Fatal("only sets seen as compiled output are indexed")
	}
	// Index A's declarations as its own context processing would.
	viewA, err := s.views.pin("main", inA)
	must(t, err)
	run := f.declaration("A", ir.DeclarationMethod, "run")
	ctor := f.declaration("A", ir.DeclarationConstructor, "A")
	mode := f.declaration("A", ir.DeclarationEnum, "Mode")
	classA := f.declaration("A", ir.DeclarationClass, "A")
	for _, d := range []struct {
		d                ir.Declaration
		kind, owner, sig string
	}{{classA, "CLASS", "p", "p.A"}, {ctor, "CONSTRUCTOR", "p.A", "<init>()"}, {run, "METHOD", "p.A", "run(java.lang.String)"}, {mode, "ENUM", "p.A", "p.A.Mode"}} {
		sd, _ := viewA.decl(d.d.ID)
		s.keys.add("main", declEvent(viewA.bridgePath, d.d, d.kind, d.owner, d.sig), viewA, sd)
	}
	s.views.unpin()
	viewB, err := s.views.pin("test", inB)
	must(t, err)
	defer s.views.unpin()
	fileB := f.files["B"]
	call := findOccurrence(t, fileB, b, `new A().run("x")`)
	valueOf := findOccurrence(t, fileB, b, `A.Mode.valueOf("ON")`)
	fe := newFileEvents(viewB.bridgePath)
	fe.add(event{Kind: "binding", Path: viewB.bridgePath, Start: int64(call.Span.Start.ByteOffset), End: int64(call.Span.End.ByteOffset), TreeKind: "METHOD_INVOCATION", ElementKind: "METHOD", Name: "run", Owner: "p.A", Signature: "run(java.lang.String)", Status: "resolved", ArtifactURI: "file://" + output + "/p/A.class"})
	fe.add(event{Kind: "binding", Path: viewB.bridgePath, Start: int64(valueOf.Span.Start.ByteOffset), End: int64(valueOf.Span.End.ByteOffset), TreeKind: "METHOD_INVOCATION", ElementKind: "METHOD", Name: "valueOf", Owner: "p.A.Mode", Signature: "valueOf(java.lang.String)", Status: "resolved", ArtifactURI: "file://" + output + "/p/A$Mode.class"})
	lookups, err := s.resolveFile(viewB, fe)
	must(t, err)
	must(t, s.symbols.flush())
	if l := lookupOf(t, lookups, semantic.LookupCall, call.ID); l.SelectedSymbolID != symbolID("A", run.ID) {
		t.Fatalf("reactor member: %+v", l)
	}
	l := lookupOf(t, lookups, semantic.LookupCall, valueOf.ID)
	sym := f.w.symbols[l.SelectedSymbolID]
	if sym.Derived == nil || sym.Derived.Rule != "enum_builtin_member" || sym.OwnerSymbolID != symbolID("A", mode.ID) {
		t.Fatalf("derived enum member through bytecode: %+v", sym)
	}

	// A producer that is not attributed this run: keys come from previous identities.
	f.req.Contexts = []bc.SourceSetID{"test"}
	s = unitRun(t, f)
	s.outputs[output] = s.sets["main"]
	inA.Affected = false
	f.w.files[0] = inA
	f.w.previous[inA.Lineage] = semantic.FileIdentities{Lineage: inA.Lineage, FileID: "A", Path: inA.Source.Path, ContentSHA256: inA.Source.ContentSHA256, Declarations: []semantic.DeclarationIdentity{
		{DeclarationID: "prev-A", EntityID: "e1", Kind: ir.DeclarationClass, Name: "A", Span: classA.Span, Key: &semantic.DeclarationKey{OwnerKey: "p", Kind: ir.DeclarationClass, Name: "A", CanonicalSignature: "p.A"}},
		{DeclarationID: "prev-run", EntityID: "e2", Kind: ir.DeclarationMethod, Name: "run", Span: run.Span, OwnerID: "prev-A", Key: &semantic.DeclarationKey{OwnerKey: "p.A", Kind: ir.DeclarationMethod, Name: "run", CanonicalSignature: "run(java.lang.String)"}},
		{DeclarationID: "prev-ctor", EntityID: "e3", Kind: ir.DeclarationConstructor, Name: "A", Span: ctor.Span, OwnerID: "prev-A", Key: &semantic.DeclarationKey{OwnerKey: "p.A", Kind: ir.DeclarationConstructor, Name: "A", CanonicalSignature: "A()"}},
	}}
	viewB, err = s.views.pin("test", inB)
	must(t, err)
	fe.add(event{Kind: "binding", Path: viewB.bridgePath, Start: int64(call.Span.Start.ByteOffset), End: int64(call.Span.End.ByteOffset), TreeKind: "NEW_CLASS", ElementKind: "CONSTRUCTOR", Name: "<init>", Owner: "p.A", Signature: "<init>()", Status: "resolved", ArtifactURI: "file://" + output + "/p/A.class"})
	lookups, err = s.resolveFile(viewB, fe)
	must(t, err)
	must(t, s.symbols.flush())
	if l := lookupOf(t, lookups, semantic.LookupCall, call.ID); l.SelectedSymbolID != symbolID("A", "prev-run") || len(l.CandidateIDs) != 1 {
		t.Fatalf("previous-identity reactor member: %+v", l)
	}
	if sym, ok := f.w.symbols[symbolID("A", "prev-run")]; !ok || sym.Source == nil || sym.Source.DeclarationID != "prev-run" {
		t.Fatalf("foreign source symbol not written: %+v", sym)
	}
}

func TestCompilerKeyRules(t *testing.T) {
	e := event{Owner: "", Signature: "<init>(int)", Name: "<init>"}
	ctor := &declSummary{Kind: ir.DeclarationConstructor, Name: "A"}
	if k := compilerKey(ctor, e); k == nil || k.OwnerKey != "<default-package>" || k.CanonicalSignature != "A(int)" {
		t.Fatalf("constructor key: %+v", k)
	}
	if compilerKey(&declSummary{Kind: ir.DeclarationClass, Name: "L", Form: ir.TypeLocal}, event{Signature: "L"}) != nil {
		t.Fatal("local class keyed")
	}
	if compilerKey(&declSummary{Kind: ir.DeclarationClass, Name: "L"}, event{Signature: "L", Nesting: "ANONYMOUS"}) != nil {
		t.Fatal("anonymous class (identity view) keyed")
	}
	if compilerKey(&declSummary{Kind: ir.DeclarationParameter, Name: "p"}, event{Signature: "p:int"}) != nil || compilerKey(&declSummary{Kind: ir.DeclarationInitializer}, event{}) != nil {
		t.Fatal("parameter or initializer keyed")
	}
	if compilerKey(&declSummary{Kind: ir.DeclarationMethod, Name: "m"}, event{Owner: "p.A", Signature: "m(java.lang.String)"}) == nil {
		t.Fatal("method without IR payload (identity view) not keyed")
	}
	if compilerKey(&declSummary{Kind: ir.DeclarationField, Name: "f"}, event{Owner: "p.A", Signature: "f:\x01"}) != nil {
		t.Fatal("control character accepted")
	}
}
