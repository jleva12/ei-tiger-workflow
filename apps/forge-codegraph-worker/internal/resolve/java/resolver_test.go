package java

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
)

func testJDK(t *testing.T) string {
	t.Helper()
	home := os.Getenv("CODEGRAPH_TEST_JAVA_HOME")
	if home == "" {
		t.Skip("set CODEGRAPH_TEST_JAVA_HOME to a pinned JDK directory without symlinks")
	}
	return home
}

func readFixture(t *testing.T, rel string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", "fixture", filepath.FromSlash(rel)))
	must(t, err)
	return b
}

func TestNewValidatesJDK(t *testing.T) {
	if _, err := New(Config{JavaHome: t.TempDir(), WorkDir: t.TempDir(), CacheDir: t.TempDir()}); err == nil {
		t.Fatal("directory without bin/java accepted")
	}
	if _, err := New(Config{JavaHome: "relative", WorkDir: t.TempDir(), CacheDir: t.TempDir()}); err == nil {
		t.Fatal("relative JavaHome accepted")
	}
	home := testJDK(t)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	if r.Version() != Version || r.PolicyDigest() == "" {
		t.Fatal("version/policy")
	}
}

// The two-file fixture exercises calls, type uses, member references, an
// interface implementation, an Object override, a lambda and a method
// reference against the real javac bridge.
func TestResolveBindsOverridesAndImplementations(t *testing.T) {
	home := testJDK(t)
	f := newFixture(t, home, nil)
	app := readFixture(t, "src/p/App.java")
	greeter := readFixture(t, "src/p/Greeter.java")
	f.addParsed("Greeter", "main", "src/p/Greeter.java", greeter, true)
	f.addParsed("App", "main", "src/p/App.java", app, true)
	work := t.TempDir()
	cache := t.TempDir()
	r, err := New(Config{JavaHome: home, WorkDir: work, CacheDir: cache})
	must(t, err)
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unsupported != 0 || result.Unresolved != 0 || result.Ambiguous != 0 || result.Resolved == 0 {
		for _, id := range []string{"App", "Greeter"} {
			for _, l := range f.lookups(id) {
				if l.Status != semantic.LookupResolved {
					t.Logf("%s %+v", id, l)
				}
			}
		}
		t.Fatalf("result: %+v", result)
	}
	if result.Symbols == 0 || int(result.Symbols) != len(f.w.symbols) {
		t.Fatalf("symbols written=%d stored=%d", result.Symbols, len(f.w.symbols))
	}
	greet := f.declaration("Greeter", ir.DeclarationMethod, "greet")
	greetSymbol := symbolID("Greeter", greet.ID)
	appGreet := f.declaration("App", ir.DeclarationMethod, "greet")
	toString := f.declaration("App", ir.DeclarationMethod, "toString")

	// Calls: source targets in this and another file, external targets.
	if l := f.occurrenceLookup("App", semantic.LookupCall, `greet(name)`, app); l.SelectedSymbolID != symbolID("App", appGreet.ID) {
		t.Fatalf("same-file call: %+v", l)
	}
	if l := f.occurrenceLookup("App", semantic.LookupCall, `direct.greet("x")`, app); l.SelectedSymbolID != greetSymbol || len(l.CandidateIDs) != 1 {
		t.Fatalf("cross-file call: %+v", l)
	}
	for _, text := range []string{`List.of("a", "b")`, `names.size()`, `names.get(0)`} {
		l := f.occurrenceLookup("App", semantic.LookupCall, text, app)
		sym := f.w.symbols[l.SelectedSymbolID]
		if sym.External == nil || sym.External.ArtifactID != "jdk21" || !strings.HasPrefix(sym.External.ArtifactFingerprint, "sha256:") {
			t.Fatalf("%s: %+v -> %+v", text, l, sym)
		}
	}
	// Types and members.
	if l := f.occurrenceLookup("App", semantic.LookupType, `List<String>`, app); f.w.symbols[l.SelectedSymbolID].External == nil || f.w.symbols[l.SelectedSymbolID].Name != "List" {
		t.Fatalf("parameterized type: %+v", l)
	}
	if l := f.occurrenceLookup("App", semantic.LookupInheritance, `Greeter`, app); l.SelectedSymbolID != symbolID("Greeter", f.declaration("Greeter", ir.DeclarationInterface, "Greeter").ID) {
		t.Fatalf("heritage: %+v", l)
	}
	names := f.declaration("App", ir.DeclarationField, "names")
	if l := f.occurrenceLookup("App", semantic.LookupMember, `names`, app); l.SelectedSymbolID != symbolID("App", names.ID) {
		t.Fatalf("member: %+v", l)
	}
	// Overrides: one for toString (external Object#toString), one for greet.
	var overrides []semantic.Lookup
	for _, l := range f.lookups("App") {
		if l.Kind == semantic.LookupOverride {
			overrides = append(overrides, l)
		}
	}
	if len(overrides) != 2 {
		t.Fatalf("overrides: %+v", overrides)
	}
	for _, l := range overrides {
		if l.Status != semantic.LookupResolved || l.OccurrenceID != "" {
			t.Fatalf("override: %+v", l)
		}
		switch l.DeclarationID {
		case toString.ID:
			sym := f.w.symbols[l.SelectedSymbolID]
			if sym.External == nil || sym.External.ArtifactID != "jdk21" || sym.Key == nil || sym.Key.OwnerKey != "java.lang.Object" || sym.Key.CanonicalSignature != "toString()" {
				t.Fatalf("toString override target: %+v", sym)
			}
		case appGreet.ID:
			if l.SelectedSymbolID != greetSymbol {
				t.Fatalf("greet override target: %+v", l)
			}
		default:
			t.Fatalf("override on unexpected declaration: %+v", l)
		}
		if string(app[l.Evidence.Span.Start.ByteOffset:l.Evidence.Span.End.ByteOffset]) != f.files["App"].Declarations[indexOf(f.files["App"], l.DeclarationID)].Name {
			t.Fatalf("override evidence is not the method name: %+v", l)
		}
	}
	// Implements: the lambda and the method reference both bind to Greeter.greet.
	lambda := f.occurrenceLookup("App", semantic.LookupImplements, `name -> greet(name) + "!"`, app)
	reference := f.occurrenceLookup("App", semantic.LookupImplements, `this::greet`, app)
	for _, l := range []semantic.Lookup{lambda, reference} {
		if l.Status != semantic.LookupResolved || l.SelectedSymbolID != greetSymbol {
			t.Fatalf("implements: %+v", l)
		}
		typeLookup := f.occurrenceLookup("App", semantic.LookupType, string(app[l.Evidence.Span.Start.ByteOffset:l.Evidence.Span.End.ByteOffset]), app)
		if typeLookup.OccurrenceID != l.OccurrenceID || typeLookup.SelectedSymbolID != symbolID("Greeter", f.declaration("Greeter", ir.DeclarationInterface, "Greeter").ID) {
			t.Fatalf("functional interface type lookup: %+v", typeLookup)
		}
	}
	if l := f.occurrenceLookup("App", semantic.LookupCall, `this::greet`, app); l.SelectedSymbolID != symbolID("App", appGreet.ID) {
		t.Fatalf("method reference call: %+v", l)
	}
	// Symbols: keys come from javac signatures; every declaration is represented.
	sym := f.w.symbols[symbolID("App", appGreet.ID)]
	if sym.Key == nil || sym.Key.OwnerKey != "p.App" || sym.Key.CanonicalSignature != "greet(java.lang.String)" || sym.Key.Kind != ir.DeclarationMethod {
		t.Fatalf("source key: %+v", sym.Key)
	}
	for id, file := range f.files {
		for _, d := range file.Declarations {
			if _, ok := f.w.symbols[symbolID(ir.FileID(id), d.ID)]; !ok {
				t.Fatalf("declaration %s %s has no symbol", id, d.Name)
			}
		}
	}
	if _, ok := f.w.symbols[intrinsic("void").ID]; !ok {
		t.Fatal("intrinsic void missing")
	}
	// Scratch files are gone; the compiled bridge is cached.
	entries, err := os.ReadDir(work)
	must(t, err)
	if len(entries) != 0 {
		t.Fatal("compiler scratch files not cleaned")
	}
	if _, err := os.Stat(filepath.Join(r.bridgeDir, "BindingBridge.class")); err != nil || !strings.HasPrefix(r.bridgeDir, filepath.Join(cache, "bridge")) {
		t.Fatalf("bridge cache: %s %v", r.bridgeDir, err)
	}
}

func indexOf(f ir.SourceFile, id ir.DeclarationID) int {
	for i, d := range f.Declarations {
		if d.ID == id {
			return i
		}
	}
	return -1
}

// Erroneous or ambiguous invocations never bind, and carry compiler evidence.
func TestResolveRecordsDiagnosticsWithoutBinding(t *testing.T) {
	home := testJDK(t)
	f := newFixture(t, home, nil)
	src := []byte(`class A { void pick(String s) {} void pick(Integer n) {} void bad(Missing value) {} void run() { pick(null); Missing.now(); bad(null); } }`)
	f.addParsed("A", "main", "src/A.java", src, true)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unresolved+result.Ambiguous == 0 {
		t.Fatalf("diagnostics hidden: %+v", result)
	}
	for _, text := range []string{"pick(null)", "Missing.now()", "bad(null)"} {
		l := f.occurrenceLookup("A", semantic.LookupCall, text, src)
		if l.Status == semantic.LookupResolved || l.SelectedSymbolID != "" || !strings.Contains(l.Reason, "compiler") || l.Cause == "" {
			t.Fatalf("invalid call bound: %+v", l)
		}
	}
	if l := f.occurrenceLookup("A", semantic.LookupCall, "pick(null)", src); l.Status != semantic.LookupAmbiguous || l.DiagnosticCode == "" {
		t.Fatalf("ambiguous overload: %+v", l)
	}
}

// A consumer set sees the producer set's pinned class directory. Bytecode
// members map back to the producer's source declarations, and the producer
// is attributed first because the consumer depends on it.
func TestResolveMapsReactorBytecodeToSource(t *testing.T) {
	home := testJDK(t)
	a := []byte(`package p; public class A {
 public int run(String value){return 1;}
 public int run(int value){return 2;}
 public enum Mode { ON, OFF }
}`)
	b := []byte(`package p; class B { int work(){ A.Mode m = A.Mode.valueOf("ON"); return new A().run("hello"); } }`)
	var f *fixture
	f = newFixture(t, home, func(in *bc.Inventory, checks *[]bc.InputCheck) {
		// The producer's compiled output lives in the checkout, like a
		// Maven target/classes directory; it is fingerprinted after writing.
		in.Inputs = append(in.Inputs, bc.Input{ID: "main-output", Kind: bc.InputClasses, Location: &bc.Location{Root: "checkout", Path: "target/classes"}, SHA256: strings.Repeat("0", 64)})
		*checks = append(*checks, bc.InputCheck{InputID: "main-output", Status: bc.Available, ObservedSHA256: strings.Repeat("0", 64)})
		in.SourceSets[0].OutputInputID = "main-output"
	})
	output := filepath.Join(f.checkout, "target", "classes")
	must(t, os.MkdirAll(output, 0o700))
	src := filepath.Join(t.TempDir(), "A.java")
	must(t, os.WriteFile(src, a, 0o600))
	if body, err := exec.Command(filepath.Join(home, "bin", "javac"), "-proc:none", "-d", output, src).CombinedOutput(); err != nil {
		t.Fatalf("fixture bytecode: %v %s", err, body)
	}
	fp, err := manifest.Fingerprint(context.Background(), f.checkout, "target/classes", bc.InputClasses, bc.DefaultLimits())
	must(t, err)
	for i := range f.build.Inventory.Inputs {
		if f.build.Inventory.Inputs[i].ID == "main-output" {
			f.build.Inventory.Inputs[i].SHA256 = fp
		}
	}
	f.w.build = f.build
	f.addParsed("A", "main", "src/p/A.java", a, true)
	f.addParsed("B", "test", "src/p/B.java", b, true)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	// Request the consumer first: ordering must still attribute main before test.
	f.req.Contexts = []bc.SourceSetID{"test", "main"}
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unsupported != 0 || result.Unresolved != 0 {
		for _, id := range []string{"A", "B"} {
			for _, l := range f.lookups(id) {
				if l.Status != semantic.LookupResolved {
					t.Logf("%s %+v", id, l)
				}
			}
		}
		t.Fatalf("result: %+v", result)
	}
	var run ir.DeclarationID
	for _, d := range f.files["A"].Declarations {
		if d.Kind == ir.DeclarationMethod && d.Name == "run" && strings.Contains(string(a[d.Span.Start.ByteOffset:d.Span.End.ByteOffset]), "String value") {
			run = d.ID
		}
	}
	if l := f.occurrenceLookup("B", semantic.LookupCall, `new A().run("hello")`, b); l.SelectedSymbolID != symbolID("A", run) || len(l.CandidateIDs) != 2 {
		t.Fatalf("bytecode member did not map to source: %+v", l)
	}
	valueOf := f.occurrenceLookup("B", semantic.LookupCall, `A.Mode.valueOf("ON")`, b)
	sym := f.w.symbols[valueOf.SelectedSymbolID]
	if sym.Derived == nil || sym.Derived.Rule != "enum_builtin_member" || sym.OwnerSymbolID != symbolID("A", f.declaration("A", ir.DeclarationEnum, "Mode").ID) {
		t.Fatalf("enum builtin through bytecode: %+v", sym)
	}
}

// The binding rules the previous implementation proved on javac fixtures:
// varargs written rank, inferred var provenance, compact record parameters,
// anonymous/local/union origins, implicit members and static-import
// candidate completeness. Every lookup resolves and no coverage is lost.
func TestResolveKeepsLegacyBindingSemantics(t *testing.T) {
	home := testJDK(t)
	f := newFixture(t, home, nil)
	src := []byte(`import static java.lang.Math.max;
class A {
 String greeting="😀";
 void doubles(double[]... values) {}
 void strings(String[] ... values) {}
 record R(int value, String text) { R { if(value<1)throw new IllegalArgumentException(text); value=value+1; } }
 enum Mode { ON, OFF }
 enum Style { ONE("one"){ int size(){return 1;} }; Style(String name){} abstract int size(); }
 static class Empty { Empty(){this(1);} Empty(int n){super();} }
 <T extends Number> void run(T input) {
  var number = 1; var values = new int[2][3]; var list = java.util.List.of("one", "two"); var generic = input;
  for (var item : list) { var length = item.length(); }
  new Empty(); Mode.values(); Mode.valueOf("ON"); new R(1, "x").value(); max(1,2);
  Runnable action = new Runnable(){ public void run(){ System.out.println("anonymous"); } }; action.run();
  class Local { void work(){} } new Local().work();
  int[] ints = new int[2]; char letters[] = new char[2]; java.util.List<? extends Number> numbers = java.util.List.of(1);
  try { if(System.currentTimeMillis()>0) throw new java.io.IOException(); throw new ReflectiveOperationException(); } catch(java.io.IOException | ReflectiveOperationException e){System.out.println(e);}
 }
}`)
	f.addParsed("A", "main", "src/A.java", src, true)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unsupported != 0 || result.Unresolved != 0 || result.Ambiguous != 0 {
		for _, l := range f.lookups("A") {
			if l.Status != semantic.LookupResolved {
				t.Logf("%q %+v", src[l.Evidence.Span.Start.ByteOffset:l.Evidence.Span.End.ByteOffset], l)
			}
		}
		t.Fatalf("legacy coverage: %+v", result)
	}
	constructed, derived, external := 0, 0, 0
	for _, s := range f.w.symbols {
		if s.Constructed != nil {
			constructed++
		}
		if s.Derived != nil {
			derived++
		}
		if s.External != nil {
			external++
		}
	}
	if constructed < 4 || derived < 5 || external == 0 {
		t.Fatalf("constructed=%d derived=%d external=%d", constructed, derived, external)
	}
	if l := f.occurrenceLookup("A", semantic.LookupCall, "max(1,2)", src); len(l.CandidateIDs) < 4 {
		t.Fatalf("static import overload environment truncated: %+v", l)
	}
	for _, written := range []string{"double[]", "String[]"} {
		l := f.occurrenceLookup("A", semantic.LookupType, written, src)
		sym := f.w.symbols[l.SelectedSymbolID]
		if sym.Constructed == nil || sym.Constructed.CanonicalSignature != strings.ReplaceAll(written, "String", "java.lang.String") {
			t.Fatalf("varargs %q bound to %+v", written, sym)
		}
	}
	// Inferred var types keep compiler origins: int[][] is a constructed
	// array over the int intrinsic, List over the external interface.
	inferred := map[string]bool{}
	for _, l := range f.lookups("A") {
		if l.Kind != semantic.LookupType || string(src[l.Evidence.Span.Start.ByteOffset:l.Evidence.Span.End.ByteOffset]) != "var" {
			continue
		}
		sym := f.w.symbols[l.SelectedSymbolID]
		switch {
		case sym.Constructed != nil && sym.Constructed.CanonicalSignature == "int[][]" && sym.Constructed.ComponentSymbolIDs[0] == intrinsic("int").ID:
			inferred["values"] = true
		case sym.External != nil && sym.Name == "List":
			inferred["list"] = true
		case sym.Intrinsic != nil && sym.Name == "int":
			inferred["number"] = true
		}
	}
	if len(inferred) != 3 {
		t.Fatalf("inferred provenance: %v", inferred)
	}
	// The compact constructor's parameter references bind to derived
	// parameters contributed by the constructor and the record component.
	compact := 0
	for _, l := range f.lookups("A") {
		if l.Kind != semantic.LookupMember {
			continue
		}
		if sym := f.w.symbols[l.SelectedSymbolID]; sym.Derived != nil && sym.Derived.Rule == "compact_record_constructor_parameter" {
			if len(sym.Derived.SourceSymbolIDs) != 2 {
				t.Fatalf("compact parameter contributors: %+v", sym)
			}
			compact++
		}
	}
	if compact < 3 {
		t.Fatalf("compact record parameter references: %d", compact)
	}
	// The static type decides: action.run() is Runnable.run() from the JDK,
	// while the local class member binds to its own source declaration.
	if l := f.occurrenceLookup("A", semantic.LookupCall, "action.run()", src); f.w.symbols[l.SelectedSymbolID].External == nil {
		t.Fatalf("interface-typed call: %+v", l)
	}
	if l := f.occurrenceLookup("A", semantic.LookupCall, "new Local().work()", src); l.SelectedSymbolID != symbolID("A", f.declaration("A", ir.DeclarationMethod, "work").ID) {
		t.Fatalf("local class member: %+v", l)
	}
}

// An error inside a call's arguments, or in a lambda's body, is not the
// call's or the lambda's own: javac still attributed the method and the
// functional interface, and those bindings stand. The erroneous argument
// itself stays unresolved.
func TestErrorsInArgumentsAndBodiesKeepTheirCallsBound(t *testing.T) {
	home := testJDK(t)
	f := newFixture(t, home, nil)
	src := []byte(`class B {
  void process(String s) {}
  String name() { return ""; }
  void run() {
    process(unknownVar);
    process(name().nope());
    Runnable r = () -> { undefinedCall(); };
    java.util.function.Function<String, String> g = s -> s.missing();
  }
  static void still() { process("static context"); }
  int peek(C c) { return c.secret; }
}
class C { private int secret; }`)
	f.addParsed("B", "main", "src/B.java", src, true)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	if _, err := r.Resolve(context.Background(), f.req, f.w); err != nil {
		t.Fatal(err)
	}
	for _, text := range []string{"process(unknownVar)", "process(name().nope())"} {
		if l := f.occurrenceLookup("B", semantic.LookupCall, text, src); l.Status != semantic.LookupResolved {
			t.Fatalf("%s: %+v", text, l)
		}
	}
	for _, text := range []string{"name()"} {
		if l := f.occurrenceLookup("B", semantic.LookupCall, text, src); l.Status != semantic.LookupResolved {
			t.Fatalf("%s: %+v", text, l)
		}
	}
	for _, text := range []string{"name().nope()", "undefinedCall()", "s.missing()"} {
		if l := f.occurrenceLookup("B", semantic.LookupCall, text, src); l.Status == semantic.LookupResolved {
			t.Fatalf("an erroneous call bound: %s %+v", text, l)
		}
	}
	for _, text := range []string{"() -> { undefinedCall(); }", "s -> s.missing()"} {
		if l := f.occurrenceLookup("B", semantic.LookupImplements, text, src); l.Status != semantic.LookupResolved {
			t.Fatalf("lambda %s: %+v", text, l)
		}
	}
	// A method used from a static context, a private member used from
	// outside: errors, but the code refers to exactly that declaration.
	if l := f.occurrenceLookup("B", semantic.LookupCall, `process("static context")`, src); l.Status != semantic.LookupResolved {
		t.Fatalf("static context: %+v", l)
	}
	if l := f.occurrenceLookup("B", semantic.LookupMember, "c.secret", src); l.Status != semantic.LookupResolved {
		t.Fatalf("private access: %+v", l)
	}
}
