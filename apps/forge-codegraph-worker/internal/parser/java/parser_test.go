package java

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
)

func inputFor(source string) parser.Input {
	content := []byte(source)
	digest := sha256.Sum256(content)
	return parser.Input{Source: ir.Source{FileID: "test-file", RepositoryID: "repo", SnapshotID: "snapshot", Path: "src/Test.java", ContentSHA256: hex.EncodeToString(digest[:]), SizeBytes: uint64(len(content)), Language: "java", LanguageVersion: "21"}, Content: content, Limits: parser.DefaultLimits()}
}
func newTestParser(t testing.TB) *Parser {
	t.Helper()
	p, err := New()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := p.Close(context.Background()); err != nil {
			t.Error(err)
		}
	})
	return p
}
func parseTest(t testing.TB, p *Parser, input parser.Input) ir.SourceFile {
	t.Helper()
	file, err := p.Parse(context.Background(), input)
	if err != nil {
		t.Fatal(err)
	}
	if err := file.Validate(); err != nil {
		t.Fatal(err)
	}
	if file.Source != input.Source {
		t.Fatal("source metadata changed")
	}
	return file
}
func fixture(t testing.TB) string {
	t.Helper()
	source, err := os.ReadFile("testdata/Resolution.java")
	if err != nil {
		t.Fatal(err)
	}
	return string(source)
}
func expressions(file ir.SourceFile) map[ir.ExpressionID]ir.Expression {
	result := map[ir.ExpressionID]ir.Expression{}
	for _, x := range file.Expressions {
		result[x.ID] = x
	}
	return result
}
func types(file ir.SourceFile) map[ir.TypeRefID]ir.TypeRef {
	result := map[ir.TypeRefID]ir.TypeRef{}
	for _, x := range file.Types {
		result[x.ID] = x
	}
	return result
}
func declarations(file ir.SourceFile) map[ir.DeclarationID]ir.Declaration {
	result := map[ir.DeclarationID]ir.Declaration{}
	for _, x := range file.Declarations {
		result[x.ID] = x
	}
	return result
}

// occurrences gathers every occurrence-bearing record so tests can assert
// identity uniqueness across all tables rather than within one.
func occurrences(file ir.SourceFile) []ir.Occurrence {
	var out []ir.Occurrence
	for _, x := range file.Imports {
		out = append(out, x.Occurrence)
	}
	for _, x := range file.Calls {
		out = append(out, x.Occurrence)
	}
	for _, x := range file.CallableReferences {
		out = append(out, x.Occurrence)
	}
	for _, x := range file.Lambdas {
		out = append(out, x.Occurrence)
	}
	for _, x := range file.References {
		out = append(out, x.Occurrence)
	}
	for _, x := range file.TypeUses {
		out = append(out, x.Occurrence)
	}
	for _, x := range file.Annotations {
		out = append(out, x.Occurrence)
	}
	if file.Module != nil {
		for _, d := range file.Module.Directives {
			out = append(out, d.Occurrence)
		}
	}
	return out
}
func assertUniqueOccurrences(t testing.TB, file ir.SourceFile) {
	t.Helper()
	seen := map[ir.OccurrenceID]bool{}
	for _, o := range occurrences(file) {
		if o.ID == "" || seen[o.ID] {
			t.Fatalf("occurrence identity missing or shared across tables: %q", o.ID)
		}
		seen[o.ID] = true
	}
	for _, l := range file.Lambdas {
		if !seen[l.Occurrence.ID] {
			t.Fatal("lambda site identity not counted among occurrences")
		}
	}
}

func TestResolutionFixture(t *testing.T) {
	p := newTestParser(t)
	input := inputFor(fixture(t))
	f := parseTest(t, p, input)
	if f.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("unexpected gaps: %+v", f.Coverage.Issues)
	}
	if f.Producer.Grammar != "tree-sitter-java" || f.Producer.GrammarVersion != GrammarVersion || f.Coverage.FeatureSet != FeatureSet {
		t.Fatalf("provenance: %+v", f.Producer)
	}
	if len(f.Imports) != 4 {
		t.Fatalf("imports: %+v", f.Imports)
	}
	wantImports := []ir.ImportKind{ir.ImportSingleType, ir.ImportTypeOnDemand, ir.ImportSingleStatic, ir.ImportStaticOnDemand}
	for i, want := range wantImports {
		if f.Imports[i].Kind != want {
			t.Errorf("import %d: %s", i, f.Imports[i].Kind)
		}
	}
	xs, ts, ds := expressions(f), types(f), declarations(f)
	callByText := map[string]ir.Call{}
	for _, c := range f.Calls {
		callByText[xs[c.ExpressionID].Spelling] = c
	}
	for text, receiver := range map[string]string{"service.next().save(4)": "service.next()", "((Client) service).save(5)": "((Client) service)", "clients[0].save(6)": "clients[0]", "new Client(7).save(8)": "new Client(7)", "Demo.super.run()": "Demo.super", "Demo.super.field.save()": "Demo.super.field", "outer.new Inner()": "outer"} {
		c, ok := callByText[text]
		if !ok || xs[c.ReceiverID].Spelling != receiver {
			t.Errorf("%s receiver: %+v", text, xs[c.ReceiverID])
		}
	}
	if callByText["this(1);"].Kind != ir.CallThisConstructor || callByText["super(n);"].Kind != ir.CallSuperConstructor {
		t.Errorf("constructor delegation missing")
	}
	if len(f.CallableReferences) != 4 {
		t.Fatalf("callable references: %+v", f.CallableReferences)
	}
	for _, r := range f.CallableReferences {
		if _, exists := callByText[xs[r.ExpressionID].Spelling]; exists {
			t.Error("method reference is also a call")
		}
	}
	save1 := callByText["service.save(1)"]
	if len(f.Lambdas) != 2 {
		t.Fatalf("lambda sites: %+v", f.Lambdas)
	}
	var expressionBody, blockBody bool
	for _, l := range f.Lambdas {
		x := xs[l.ExpressionID]
		if x.Kind != ir.ExpressionLambda || x.Lambda == nil || x.OccurrenceID != l.Occurrence.ID || x.ScopeID != l.Occurrence.ScopeID || x.Span != l.Occurrence.Span {
			t.Errorf("lambda site does not link back to its expression: %+v", l)
		}
		// Both lambdas initialize locals of run(): the innermost enclosing
		// declaration is that local, exactly as calls in the initializer record it.
		owner := ds[l.Occurrence.EnclosingDeclarationID]
		if owner.Kind != ir.DeclarationLocal || owner.OwnerID != save1.Occurrence.EnclosingDeclarationID || l.Occurrence.ScopeID != save1.Occurrence.ScopeID {
			t.Errorf("lambda site context differs from its sibling call: %+v", l.Occurrence)
		}
		if len(l.ParameterIDs) != 1 || ds[l.ParameterIDs[0]].Name != "item" || ds[l.ParameterIDs[0]].Kind != ir.DeclarationParameter || ds[l.ParameterIDs[0]].OwnerID != owner.ID {
			t.Errorf("lambda site parameters: %+v", l.ParameterIDs)
		}
		switch x.Spelling {
		case "item -> transform(item)":
			expressionBody = l.BodyScopeID == "" && owner.Name == "lambda" && callByText["transform(item)"].Occurrence.EnclosingDeclarationID == owner.ID
		case "(String item) -> { accept(item); }":
			blockBody = l.BodyScopeID != "" && l.BodyScopeID == x.Lambda.BodyScopeID && owner.Name == "block"
		}
	}
	if !expressionBody || !blockBody {
		t.Errorf("lambda bodies: expression=%t block=%t", expressionBody, blockBody)
	}
	assertUniqueOccurrences(t, f)
	if callByText["service.save(1)"].Occurrence.ScopeID == callByText["service.save(2)"].Occurrence.ScopeID {
		t.Error("shadowing block scope lost")
	}
	if callByText["service.save(1)"].Occurrence.ScopeID != callByText["service.save(3)"].Occurrence.ScopeID {
		t.Error("outer block scope not restored")
	}
	var forms = map[ir.TypeForm]int{}
	var compact, receiver, variadic, postfix, union, lambda, annotated bool
	for _, d := range f.Declarations {
		if d.Type != nil {
			forms[d.Type.Form]++
		}
		if d.Callable != nil && d.Callable.ConstructorForm == ir.ConstructorCompact {
			compact = true
		}
		if d.Kind == ir.DeclarationReceiver {
			receiver = true
		}
		if d.Kind == ir.DeclarationRecordComponent && d.Name == "tags" {
			variadic = d.Variable.Variadic
		}
		if d.Name == "second" {
			typ := ts[d.Variable.DeclaredTypeID]
			postfix = typ.Kind == ir.TypeArray && len(typ.Dimensions) == 2 && typ.Spelling == "[][]"
		}
		if d.Name == "failure" {
			typ := ts[d.Variable.DeclaredTypeID]
			union = typ.Kind == ir.TypeUnion && len(typ.MemberTypeIDs) == 2
		}
		if d.Name == "Demo" && d.Type != nil {
			annotated = len(d.AnnotationIDs) == 1 && strings.Contains(d.DocComment, "Dependency extraction")
		}
	}
	for _, x := range f.Expressions {
		if x.Lambda != nil && x.Lambda.BodyExpressionID != "" {
			lambda = len(x.Lambda.ParameterIDs) == 1 && ds[x.Lambda.ParameterIDs[0]].Name == "item"
		}
	}
	if forms[ir.TypeAnonymous] != 2 || forms[ir.TypeLocal] != 1 || !compact || !receiver || !variadic || !postfix || !union || !lambda || !annotated {
		t.Errorf("forms=%v compact=%t receiver=%t variadic=%t postfix=%t union=%t lambda=%t annotated=%t", forms, compact, receiver, variadic, postfix, union, lambda, annotated)
	}
	var route *ir.Annotation
	for i := range f.Annotations {
		a := &f.Annotations[i]
		if ts[a.TypeRefID].Spelling == "Route" {
			route = a
		}
	}
	if route == nil || len(route.Arguments) != 3 || route.Arguments[0].Value.Kind != ir.AnnotationArray || len(route.Arguments[0].Value.Elements) != 2 || xs[route.Arguments[1].Value.ExpressionID].Literal.Lexeme != "false" || route.Arguments[2].Value.Kind != ir.AnnotationNested {
		t.Fatalf("annotation: %+v", route)
	}
	for _, d := range f.Declarations {
		if d.Name == "resource" {
			for _, scope := range f.Scopes {
				if scope.ID == d.DeclaringScopeID && (scope.Kind != ir.ScopeResource || len(scope.Regions) != 2) {
					t.Errorf("resource scope: %+v", scope)
				}
			}
		}
	}
	original, _ := json.Marshal(f)
	again, _ := json.Marshal(parseTest(t, p, input))
	if !bytes.Equal(original, again) {
		t.Fatal("nondeterministic output")
	}
	for i := range input.Content {
		input.Content[i] = 'x'
	}
	retained, _ := json.Marshal(f)
	if !bytes.Equal(original, retained) {
		t.Fatal("output aliases input bytes")
	}
}

func TestTypesAndAccess(t *testing.T) {
	f := parseTest(t, newTestParser(t), inputFor("class A { a.Outer<String>.Inner<? super Number> value; int @Mark [] x @Mark []; void f() { Object x = (First & Second) value; this.value = null; value += 1; value++; Object a = new @Mark String @Mark [size()] @Mark []; } }"))
	if f.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("coverage: %+v", f.Coverage.Issues)
	}
	ts := types(f)
	var qualified, intersection, array bool
	for _, typ := range f.Types {
		if typ.Spelling == "a.Outer<String>.Inner<? super Number>" {
			segments := typ.Named.Segments
			qualified = len(segments) == 3 && len(segments[1].TypeArguments) == 1 && len(segments[2].TypeArguments) == 1 && ts[segments[2].TypeArguments[0]].Bounds[0].Kind == ir.BoundSuper
		}
		if typ.Kind == ir.TypeIntersection {
			intersection = len(typ.MemberTypeIDs) == 2
		}
		if strings.HasPrefix(typ.Spelling, "String @Mark [") {
			array = typ.Kind == ir.TypeArray && len(typ.Dimensions) == 2 && len(typ.AnnotationIDs) == 1 && len(typ.Dimensions[0].AnnotationIDs) == 1
		}
	}
	if !qualified || !intersection || !array {
		t.Errorf("qualified=%t intersection=%t array=%t", qualified, intersection, array)
	}
	var writes, updates int
	for _, r := range f.References {
		if r.Access == ir.AccessWrite {
			writes++
		}
		if r.Access == ir.AccessReadWrite {
			updates++
		}
	}
	if writes != 1 || updates != 2 {
		t.Errorf("writes=%d updates=%d", writes, updates)
	}
}

func TestAdditionalSyntax(t *testing.T) {
	p := newTestParser(t)
	source := `@PackageMark package sample;
sealed interface Parent<T> extends First<T>, Second permits Child {}
non-sealed class Child implements Parent<String> {
  @Mark(value = 0xFF) final int a = 0b10, b = 077;
  Child(Outer outer) { outer.<String>super(1); }
  void f(String... args) {
    label: while (ready()) { if (flag) break label; else continue; }
    do { tick(); } while (ready());
    assert flag : message();
    synchronized (lock) { notifyAll(); }
    java.util.function.BiFunction<String, String, String> f = (a, b) -> a + b;
    Object ref = this::<String>convert;
    Object x = object.<String>convert(1, false, null, 0x1.8p1, 'c');
    Object y = flag ? left() : right();
    boolean z = !(x instanceof String);
    try (existing) { use(existing); } catch (Exception e) { throw e; }
  }
}`
	f := parseTest(t, p, inputFor(source))
	if f.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("coverage: %+v", f.Coverage.Issues)
	}
	xs, ts := expressions(f), types(f)
	var delegated, explicitArgs, inferredLambda, importsPackage, permits bool
	importsPackage = f.Package != nil && len(f.Package.AnnotationIDs) == 1
	for _, c := range f.Calls {
		if c.Kind == ir.CallSuperConstructor {
			delegated = xs[c.ReceiverID].Spelling == "outer" && len(c.TypeArgumentIDs) == 1
		}
		if c.Name == "convert" {
			explicitArgs = len(c.Arguments) == 5 && len(c.TypeArgumentIDs) == 1 && ts[c.TypeArgumentIDs[0]].Spelling == "String"
		}
	}
	for _, d := range f.Declarations {
		if d.Name == "Parent" && d.Type != nil {
			permits = len(d.Type.Heritage) == 3 && d.Type.Heritage[2].Kind == ir.HeritagePermits
		}
	}
	for _, x := range f.Expressions {
		if x.Lambda != nil {
			inferredLambda = len(x.Lambda.ParameterIDs) == 2
		}
	}
	if !delegated || !explicitArgs || !inferredLambda || !importsPackage || !permits {
		t.Errorf("delegated=%t explicitArgs=%t lambda=%t package=%t permits=%t", delegated, explicitArgs, inferredLambda, importsPackage, permits)
	}
	if len(f.CallableReferences) != 1 || len(f.CallableReferences[0].TypeArgumentIDs) != 1 {
		t.Error("method reference type arguments missing")
	}
	// Shared declaration annotations have distinct owners and expression links.
	annotationOwners := map[ir.DeclarationID]bool{}
	for _, a := range f.Annotations {
		if ts[a.TypeRefID].Spelling == "Mark" {
			annotationOwners[a.Occurrence.EnclosingDeclarationID] = true
		}
	}
	if len(annotationOwners) != 2 {
		t.Errorf("group annotation ownership: %v", annotationOwners)
	}
}

func TestTreeSitterSessionsAndLegacyJava(t *testing.T) {
	p, other := newTestParser(t), newTestParser(t)
	if p.native == other.native || len(shared.queries) != 4 {
		t.Fatal("native sessions must be independent with cached query categories")
	}
	// Source from the original graph application's TestJavaExtraction. Assertions
	// target syntax facts; the original resolver's guesses are not the oracle.
	source := `package com.acme;
import java.util.List;
public class OwnerService extends BaseService {
    private OwnerRepo repo;
    public List<String> findAll(int limit) { return repo.findAll(); }
}`
	input := inputFor(source)
	f := parseTest(t, p, input)
	if f.Coverage.Status != ir.ExtractionComplete || len(f.Calls) != 1 || len(f.Declarations) != 4 {
		t.Fatalf("legacy fixture: %+v", f.Coverage)
	}
	xs, ts, ds := expressions(f), types(f), declarations(f)
	c := f.Calls[0]
	method := ds[c.Occurrence.EnclosingDeclarationID]
	if c.Name != "findAll" || xs[c.ReceiverID].Spelling != "repo" || method.Name != "findAll" || ds[method.OwnerID].Name != "OwnerService" || ts[method.Callable.ReturnTypeID].Spelling != "List<String>" {
		t.Fatal("legacy declaration/receiver facts changed")
	}
	var wg sync.WaitGroup
	for _, adapter := range []*Parser{p, other} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			got := parseTest(t, adapter, input)
			if !reflect.DeepEqual(f, got) {
				t.Error("worker output differs")
			}
		}()
	}
	wg.Wait()
}

func TestMalformedEdits(t *testing.T) {
	p := newTestParser(t)
	source := `@A(x=@B(v={1,2})) class C<T extends X<T>> { int n[] = new int[2]; C() { this(1); } void m() { Object f = String[]::new; call(a.b(), x -> x); } }`
	// Every single-byte deletion exercises native recovery and all forward links.
	for i := range len(source) {
		input := inputFor(source[:i] + source[i+1:])
		input.Limits.MaxDiagnostics = 1000
		parseTest(t, p, input)
	}
}

func TestPartialAndPositions(t *testing.T) {
	p := newTestParser(t)
	for _, source := range []string{"class A { void f( { save(); }", "@@@", "void main() {}"} {
		f := parseTest(t, p, inputFor(source))
		if f.Coverage.Status == ir.ExtractionComplete || len(f.Diagnostics) == 0 {
			t.Errorf("unreported loss: %q", source)
		}
	}
	source := "\ufeff// π\r\nclass A {\r  void f() {\n    save(\"π\");\r\n  }\r\n}\r\n"
	f := parseTest(t, p, inputFor(source))
	if len(f.Calls) != 1 {
		t.Fatal("call missing")
	}
	c := f.Calls[0]
	start := strings.Index(source, "save")
	if c.Occurrence.Span.Start != (ir.Position{ByteOffset: uint64(start), Line: 4, Column: 4}) {
		t.Fatalf("position: %+v", c.Occurrence.Span)
	}
	if f.Scopes[0].Span.End.ByteOffset != uint64(len(source)) {
		t.Fatal("file span omits trailing bytes")
	}
	for _, typ := range f.Types {
		if typ.Spelling != source[typ.Span.Start.ByteOffset:typ.Span.End.ByteOffset] {
			t.Errorf("type spelling changed")
		}
	}
	for _, x := range f.Expressions {
		if x.Spelling != source[x.Span.Start.ByteOffset:x.Span.End.ByteOffset] {
			t.Errorf("expression spelling changed")
		}
	}
	for _, source := range []string{"", " \r\n", "// empty"} {
		f := parseTest(t, p, inputFor(source))
		if f.Coverage.Status != ir.ExtractionComplete {
			t.Errorf("empty file: %+v", f.Coverage)
		}
	}
}

func TestLimitsAndConfiguration(t *testing.T) {
	p := newTestParser(t)
	cases := []struct {
		name   string
		change func(*parser.Input)
		want   error
	}{
		{"source", func(i *parser.Input) { i.Limits.MaxSourceBytes = 1 }, parser.ErrLimitExceeded},
		{"nodes", func(i *parser.Input) { i.Limits.MaxSyntaxNodes = 1 }, parser.ErrLimitExceeded},
		{"depth", func(i *parser.Input) { i.Limits.MaxSyntaxDepth = 1 }, parser.ErrLimitExceeded},
		{"records", func(i *parser.Input) { i.Limits.MaxIRRecords = 1 }, parser.ErrLimitExceeded},
		{"bytes", func(i *parser.Input) { i.Limits.MaxOutputBytes = 1 }, parser.ErrLimitExceeded},
		{"diagnostics", func(i *parser.Input) { *i = inputFor("class {"); i.Limits.MaxDiagnostics = 1 }, parser.ErrLimitExceeded},
		{"release", func(i *parser.Input) { i.Source.LanguageVersion = "7" }, parser.ErrUnsupportedConfig},
		{"release text", func(i *parser.Input) { i.Source.LanguageVersion = "021" }, parser.ErrUnsupportedConfig},
		{"language", func(i *parser.Input) { i.Source.Language = "go" }, parser.ErrUnsupportedConfig},
		{"unsupported budget", func(i *parser.Input) { i.Limits.MaxSyntaxDepth = 20000 }, parser.ErrUnsupportedConfig},
		{"bad hash", func(i *parser.Input) { i.Source.ContentSHA256 = strings.Repeat("0", 64) }, parser.ErrInvalidInput},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			input := inputFor("class A { void f() { save(); } }")
			tc.change(&input)
			f, err := p.Parse(context.Background(), input)
			if !errors.Is(err, tc.want) || !reflect.DeepEqual(f, ir.SourceFile{}) {
				t.Fatalf("file=%+v err=%v, want %v", f, err, tc.want)
			}
		})
	}
	// Releases the grammar predates and preview sources still parse: javac
	// judges what a release allows.
	for _, change := range []func(*parser.Input){func(i *parser.Input) { i.Source.LanguageVersion = "25" }, func(i *parser.Input) { i.Source.LanguageVersion = "16" }, func(i *parser.Input) { i.Options.EnablePreview = true }} {
		input := inputFor("class A { void f() { save(); } }")
		change(&input)
		if f, err := p.Parse(context.Background(), input); err != nil || len(f.Declarations) == 0 {
			t.Fatalf("release %s preview %v: %v", input.Source.LanguageVersion, input.Options.EnablePreview, err)
		}
	}
	input := inputFor("class A {}")
	f := parseTest(t, p, input)
	data, _ := json.Marshal(f)
	input.Limits.MaxOutputBytes = uint64(len(data))
	parseTest(t, p, input)
	input.Limits.MaxOutputBytes--
	if _, err := p.Parse(context.Background(), input); !errors.Is(err, parser.ErrLimitExceeded) {
		t.Errorf("exact output limit: %v", err)
	}
	older := inputFor("record A(int x) {}")
	older.Source.LanguageVersion = "11"
	if f := parseTest(t, p, older); f.Coverage.Status == ir.ExtractionComplete {
		t.Error("record accepted as Java 11")
	}
	modern := inputFor("class A {}")
	old := modern
	old.Source.LanguageVersion = "8"
	if parseTest(t, p, old).Producer.ConfigDigest == parseTest(t, p, modern).Producer.ConfigDigest {
		t.Error("release omitted from config digest")
	}
}

func TestConcurrentLifecycleAndCancellation(t *testing.T) {
	p := newTestParser(t)
	input := inputFor("class A { void f() { save(); } }")
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); parseTest(t, p, input) }()
	}
	wg.Wait()
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if f, err := p.Parse(canceled, input); !errors.Is(err, context.Canceled) || !reflect.DeepEqual(f, ir.SourceFile{}) {
		t.Errorf("canceled: %v", err)
	}
	// Occupy the native session to exercise cancellable queue admission without sleeps.
	<-p.gate
	deadline, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	if _, err := p.Parse(deadline, input); !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("queue cancellation: %v", err)
	}
	cancel()
	p.gate <- struct{}{}
	// Interrupt an active, sufficiently large native parse, then reuse the session.
	large := inputFor("class Large {" + strings.Repeat("void f() { call(); }", 100000) + "}")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	f, err := p.Parse(ctx, large)
	cancel()
	if !errors.Is(err, context.DeadlineExceeded) || !reflect.DeepEqual(f, ir.SourceFile{}) {
		t.Fatalf("active cancellation: %v", err)
	}
	parseTest(t, p, input)
	if err := p.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := p.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := p.Parse(context.Background(), input); !errors.Is(err, ErrClosed) {
		t.Errorf("closed: %v", err)
	}
}

func FuzzParse(f *testing.F) {
	for _, source := range []string{fixture(f), "class A {}", "class A { void f() { x = new int[; } }", "@A(v=@B()) class A {}", "class A { Runnable r = () -> {}; }", `module m { requires transitive n; uses p.S; provides p.S with p.A, p.B; }`, `class P { int f(Object o) { if (!(o instanceof String s)) return 0; return switch(o) { case String t when !t.isEmpty() -> { yield 1; } default -> 0; }; } }`, `class \u0050 { String s = "\uD800"; }`, `record R(Object o) { boolean f(Object x) { return x instanceof R(String s); } }`} {
		f.Add(source)
	}
	p := newTestParser(f)
	f.Fuzz(func(t *testing.T, source string) {
		if len(source) > 20000 {
			t.Skip()
		}
		input := inputFor(source)
		input.Limits.MaxDiagnostics = 20
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		file, err := p.Parse(ctx, input)
		if err != nil {
			if !reflect.DeepEqual(file, ir.SourceFile{}) {
				t.Fatal("artifact returned with error")
			}
			if !errors.Is(err, parser.ErrInvalidInput) && !errors.Is(err, parser.ErrLimitExceeded) && !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal(err)
			}
		} else if err := file.Validate(); err != nil {
			t.Fatal(err)
		}
	})
}

// A file with more syntax issues than the diagnostic budget keeps every
// declaration the parser could read: the issues are cut, not the file.
func TestIssuesPastTheBudgetAreCutNotTheFile(t *testing.T) {
	var source strings.Builder
	source.WriteString("class Kept {\n  void first() {}\n")
	for i := 0; i < 200; i++ {
		source.WriteString("  int @@ = ;\n")
	}
	source.WriteString("  void last() {}\n}\n")
	input := inputFor(source.String())
	input.Limits.MaxDiagnostics = 20
	file, err := newTestParser(t).Parse(context.Background(), input)
	if err != nil {
		t.Fatal(err)
	}
	names := map[string]bool{}
	for _, d := range file.Declarations {
		names[d.Name] = true
	}
	if !names["Kept"] || !names["first"] || !names["last"] {
		t.Fatalf("declarations = %v", names)
	}
	issues := file.Coverage.Issues
	if len(file.Diagnostics)+len(issues) > 20 || len(issues) == 0 || issues[len(issues)-1].Feature != "diagnostic_limit" || issues[len(issues)-1].Reason != ir.CoverageLimit || file.Coverage.Status != ir.ExtractionPartial {
		t.Fatalf("%d diagnostics, issues %+v", len(file.Diagnostics), issues)
	}
}
