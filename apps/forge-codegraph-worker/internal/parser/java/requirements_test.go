package java

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"sort"
	"testing"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
)

func completeRequirement(t *testing.T, p *Parser, source string) ir.SourceFile {
	t.Helper()
	f := parseTest(t, p, inputFor(source))
	if f.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("unexpected extraction loss: %+v", f.Coverage.Issues)
	}
	return f
}
func statements(f ir.SourceFile) map[ir.StatementID]ir.Statement {
	out := map[ir.StatementID]ir.Statement{}
	for _, s := range f.Statements {
		out[s.ID] = s
	}
	return out
}
func declarationNamed(t *testing.T, f ir.SourceFile, name string) ir.Declaration {
	t.Helper()
	for _, d := range f.Declarations {
		if d.Name == name {
			return d
		}
	}
	t.Fatalf("missing declaration %s", name)
	return ir.Declaration{}
}

// E01 -> M01/M05/M09/M21: qualified super is never a class named Outer.super.
func TestRequirementE01QualifiedSuperReference(t *testing.T) {
	f := completeRequirement(t, newTestParser(t), `class Outer { class Inner { java.util.function.Supplier<String> r = Outer.super::toString; } }`)
	if len(f.CallableReferences) != 1 || len(f.Calls) != 0 {
		t.Fatal("deferred reference became invocation")
	}
	xs, ts := expressions(f), types(f)
	q := xs[f.CallableReferences[0].QualifierID]
	if q.Kind != ir.ExpressionSuper || q.Spelling != "Outer.super" || ts[q.TypeRefID].Spelling != "Outer" {
		t.Fatalf("qualified super lost: %+v", q)
	}
	for _, typ := range f.Types {
		if typ.Spelling == "Outer.super" {
			t.Fatal("super classified as a named type")
		}
	}
}

// E02 -> M03/M05/M09/M21: return/throw/branch links support lambda completion
// analysis without reparsing Spelling. No completion result is guessed here.
func TestRequirementE02StatementAndLambdaResults(t *testing.T) {
	p := newTestParser(t)
	for _, tc := range []struct {
		body string
		kind ir.StatementKind
	}{{`use(x);`, ir.StatementExpression}, {`return use(x);`, ir.StatementReturn}, {`throw failure();`, ir.StatementThrow}} {
		f := completeRequirement(t, p, `class P { void f() { accept(x -> { `+tc.body+` }); } }`)
		ss := statements(f)
		var lambda *ir.Lambda
		for _, x := range f.Expressions {
			if x.Lambda != nil {
				lambda = x.Lambda
			}
		}
		if lambda == nil {
			t.Fatal("lambda missing")
		}
		body := ss[lambda.BodyStatementID]
		if body.Kind != ir.StatementBlock || len(body.ChildIDs) != 1 || ss[body.ChildIDs[0]].Kind != tc.kind {
			t.Fatalf("body context missing: %+v", body)
		}
		stmt := ss[body.ChildIDs[0]]
		if len(stmt.ExpressionIDs) != 1 || expressions(f)[stmt.ExpressionIDs[0]].Kind != ir.ExpressionCall {
			t.Fatal("result expression link lost")
		}
		method := declarationNamed(t, f, "f")
		if method.Callable.BodyStatementID == "" || method.Callable.BodyStatementID == lambda.BodyStatementID {
			t.Fatal("method/lambda body boundary lost")
		}
	}
	f := completeRequirement(t, p, `class P { int f(boolean flag) { label: while(true) { if(flag) break label; else continue; } try { return left(); } catch(RuntimeException e) { throw e; } finally { cleanup(); } } }`)
	ss := statements(f)
	var branch, jump, tried bool
	for _, s := range f.Statements {
		if s.Kind == ir.StatementIf {
			branch = s.ConditionID != "" && s.BodyID != "" && s.AlternativeID != ""
		}
		if s.Kind == ir.StatementBreak {
			jump = s.Label != nil && s.Label.Segments[0].Text == "label"
		}
		if s.Kind == ir.StatementTry {
			tried = s.BodyID != "" && len(s.CatchIDs) == 1 && s.FinallyID != "" && ss[s.CatchIDs[0]].Kind == ir.StatementCatch
		}
	}
	if !branch || !jump || !tried {
		t.Fatalf("branch=%t jump=%t try=%t", branch, jump, tried)
	}
}

// E03 -> M04/M05/M06/M21: patterns are flow-gated declarations, not ordinary
// block locals. Negation, short circuiting and early exits stay inspectable.
func TestRequirementE03PatternsAndSwitch(t *testing.T) {
	source := `record Pair(Object left, Object right) {} class P {
 int f(Object o) {
   if (!(o instanceof String s)) return 0;
   if (o instanceof String text && text.isEmpty()) return 1;
   if (o instanceof Pair(String a, Pair(var b, Integer c))) use(a,b,c);
   return switch(o) {
     case String value when value.isEmpty() -> 1;
     case Pair(String left, Integer right) -> { yield right; }
     default -> 0;
   };
 }
}`
	f := completeRequirement(t, newTestParser(t), source)
	ss := statements(f)
	xs := expressions(f)
	patterns := map[ir.PatternID]ir.Pattern{}
	for _, p := range f.Patterns {
		patterns[p.ID] = p
	}
	want := []string{"s", "text", "a", "b", "c", "value", "left", "right"}
	for _, name := range want {
		d := declarationNamed(t, f, name)
		if d.Kind == ir.DeclarationRecordComponent { // same names on the record declaration
			for _, candidate := range f.Declarations {
				if candidate.Name == name && candidate.Kind == ir.DeclarationPatternVariable {
					d = candidate
					break
				}
			}
		}
		if d.Kind != ir.DeclarationPatternVariable {
			t.Errorf("%s is not flow-gated", name)
		}
	}
	var negated, shortCircuit, record, switchResult, guard, yield bool
	for _, x := range f.Expressions {
		if x.Kind == ir.ExpressionUnary && x.Operator == "!" {
			negated = true
		}
		if x.Kind == ir.ExpressionBinary && x.Operator == "&&" {
			shortCircuit = true
		}
		if x.PatternID != "" && patterns[x.PatternID].Kind == ir.PatternRecord {
			record = len(patterns[x.PatternID].ComponentIDs) == 2
		}
		if x.Switch != nil {
			switchResult = ss[x.Switch.BodyStatementID].Kind == ir.StatementBlock
		}
	}
	for _, s := range f.Statements {
		if s.Kind == ir.StatementSwitchLabel && s.ConditionID != "" {
			guard = xs[s.ConditionID].Kind == ir.ExpressionCall
		}
		if s.Kind == ir.StatementYield {
			yield = len(s.ExpressionIDs) == 1
		}
	}
	if !negated || !shortCircuit || !record || !switchResult || !guard || !yield {
		t.Fatalf("negated=%t short=%t record=%t switch=%t guard=%t yield=%t", negated, shortCircuit, record, switchResult, guard, yield)
	}
}

// E04 -> M03/M04/M06/M15/M21: valid annotated parameters must not disappear.
func TestRequirementE04AnnotatedParameters(t *testing.T) {
	f := completeRequirement(t, newTestParser(t), `import java.lang.annotation.*; @Target(ElementType.TYPE_USE) @interface Mark {} class First extends RuntimeException {} class Second extends RuntimeException {} class P { void f(String @Mark ... values) { try {} catch(@Mark First | @Mark Second failure) {} } }`)
	values := declarationNamed(t, f, "values")
	failure := declarationNamed(t, f, "failure")
	ts := types(f)
	if values.Kind != ir.DeclarationParameter || !values.Variable.Variadic || len(values.AnnotationIDs) != 1 || ts[values.Variable.DeclaredTypeID].Spelling != "String" {
		t.Fatalf("annotated varargs lost: %+v", values)
	}
	union := ts[failure.Variable.DeclaredTypeID]
	if union.Kind != ir.TypeUnion || len(union.MemberTypeIDs) != 2 || len(ts[union.MemberTypeIDs[1]].AnnotationIDs) != 1 {
		t.Fatalf("annotated catch lost: %+v", union)
	}
}

// E05 -> M03/M05/M08: diamond is explicit syntax, not a raw generic type.
func TestRequirementE05Diamond(t *testing.T) {
	f := completeRequirement(t, newTestParser(t), `class P { Object a = new java.util.ArrayList<>(); Object b = new java.util.ArrayList(); Object c = new java.util.ArrayList<String>(); }`)
	ts := types(f)
	states := map[string]ir.TypeArgumentSyntax{}
	for _, c := range f.Calls {
		typ := ts[c.ConstructedTypeID]
		states[typ.Spelling] = typ.Named.Segments[len(typ.Named.Segments)-1].ArgumentSyntax
	}
	if states["java.util.ArrayList<>"] != ir.TypeArgumentsDiamond || states["java.util.ArrayList"] != "" || states["java.util.ArrayList<String>"] != ir.TypeArgumentsExplicit {
		t.Fatalf("argument syntax: %v", states)
	}
}

// E06 -> M07/M11/M15: annotation defaults use the annotation value union.
func TestRequirementE06AnnotationDefaults(t *testing.T) {
	f := completeRequirement(t, newTestParser(t), `@interface Inner { String value(); } @interface Outer { Inner nested() default @Inner("x"); Inner[] array() default {@Inner("a"),@Inner("b")}; int constant() default 1+2; }`)
	nested := declarationNamed(t, f, "nested").Callable
	array := declarationNamed(t, f, "array").Callable
	constant := declarationNamed(t, f, "constant").Callable
	if nested.AnnotationDefault == nil || nested.AnnotationDefault.Kind != ir.AnnotationNested || nested.AnnotationDefault.AnnotationID == "" || nested.DefaultValueID != "" {
		t.Fatalf("nested default: %+v", nested)
	}
	if array.AnnotationDefault == nil || array.AnnotationDefault.Kind != ir.AnnotationArray || len(array.AnnotationDefault.Elements) != 2 {
		t.Fatalf("array default: %+v", array)
	}
	if constant.AnnotationDefault == nil || expressions(f)[constant.AnnotationDefault.ExpressionID].Kind != ir.ExpressionBinary {
		t.Fatal("constant default lost")
	}
}

// E07 -> M01/M06/M13/M18/M21: module syntax is kept separate from resolved
// module-path visibility; uses/provides retain all type dependencies.
func TestRequirementE07ModuleDirectives(t *testing.T) {
	f := completeRequirement(t, newTestParser(t), `@Mark module app.core { requires static transitive library.api; exports app.api to client.a, client.b; opens app.internal to framework; uses app.Service; provides app.Service with app.First, app.Second; }`)
	m := f.Module
	if m == nil || len(m.Directives) != 5 || len(m.AnnotationIDs) != 1 || m.Open {
		t.Fatalf("module: %+v", m)
	}
	if len(m.Directives[0].Modifiers) != 2 || len(m.Directives[1].TargetModules) != 2 || len(m.Directives[4].ProviderTypeIDs) != 2 {
		t.Fatalf("directive details: %+v", m.Directives)
	}
	uses, providers := 0, 0
	for _, u := range f.TypeUses {
		if u.Role == ir.TypeUseModuleService {
			uses++
		}
		if u.Role == ir.TypeUseModuleProvider {
			providers++
		}
	}
	if uses != 2 || providers != 2 {
		t.Fatalf("service=%d provider=%d", uses, providers)
	}
	open := completeRequirement(t, newTestParser(t), `open module app { requires library; }`)
	if !open.Module.Open {
		t.Fatal("open modifier lost")
	}
}

func TestRequirementE07UnicodeTranslationAndSpans(t *testing.T) {
	p := newTestParser(t)
	source := `// comment\u000aclass \u0050 { int \uuuu0061; void f() { \u0063all("\uD83D\uDE00", "\uD800", "\\u0061", "\u005c\u005c"); } }`
	f := completeRequirement(t, p, source)
	if declarationNamed(t, f, "P").Kind != ir.DeclarationClass || declarationNamed(t, f, "a").Kind != ir.DeclarationField {
		t.Fatal("translated identifiers lost")
	}
	if len(f.Calls) != 1 || f.Calls[0].Name != "call" {
		t.Fatalf("translated call: %+v", f.Calls)
	}
	if f.Calls[0].Occurrence.Span.Start.Line != 1 {
		t.Fatal("translated newline changed original line positions")
	}
	for _, x := range f.Expressions {
		if x.Spelling != source[x.Span.Start.ByteOffset:x.Span.End.ByteOffset] {
			t.Fatalf("raw expression span/spelling mismatch: %+v", x)
		}
	}
	for _, typ := range f.Types {
		if typ.Spelling != source[typ.Span.Start.ByteOffset:typ.Span.End.ByteOffset] {
			t.Fatal("raw type spelling changed")
		}
	}
	bad := parseTest(t, p, inputFor(`class P { int \u00Q0; }`))
	if bad.LanguageValidation.Status != ir.LanguageInvalid || bad.Coverage.Status == ir.ExtractionComplete {
		t.Fatal("malformed eligible escape not reported")
	}
	// No recursive escape translation; parity and escape-origin eligibility use
	// the translated stream, as required by JLS 3.3.
	for _, tc := range []struct {
		raw   string
		units []uint16
	}{
		{`\u005cu005a`, []uint16{'\\', 'u', '0', '0', '5', 'a'}},
		{`\\u005a`, []uint16{'\\', '\\', 'u', '0', '0', '5', 'a'}},
		{`\\\u006e`, []uint16{'\\', '\\', 'n'}},
		{`\u005c\u005c\u006e`, []uint16{'\\', '\\', 'n'}},
		{`\uD800`, []uint16{0xd800}},
	} {
		got, err := translateJava(context.Background(), []byte(tc.raw))
		if err != nil || !reflect.DeepEqual(got.units, tc.units) {
			t.Errorf("translate %q: %v %v", tc.raw, got.units, err)
		}
	}
}

// E08 -> M18/M21: known violations are invalid; all other output explicitly
// says full language validity is not checked by this syntax frontend.
func TestRequirementE08ReleaseValidity(t *testing.T) {
	p := newTestParser(t)
	for _, source := range []string{`interface P { private void hidden() {} }`, `class P { void f(java.io.Closeable c) throws Exception { try(c) {} } }`, `module app { requires library; }`} {
		in := inputFor(source)
		in.Source.LanguageVersion = "8"
		f := parseTest(t, p, in)
		if f.LanguageValidation.Status != ir.LanguageInvalid || f.Coverage.Status == ir.ExtractionComplete {
			t.Fatalf("release violation accepted: %s", source)
		}
		in.Source.LanguageVersion = "11"
		f = parseTest(t, p, in)
		if f.Coverage.Status != ir.ExtractionComplete || f.LanguageValidation.Status != ir.LanguageNotChecked {
			t.Fatalf("release validity overclaimed: %+v", f.LanguageValidation)
		}
	}
}

// E09 -> M13: documentation belongs to a field group, not its declarator node.
func TestRequirementE09FieldDocumentation(t *testing.T) {
	f := completeRequirement(t, newTestParser(t), `class P { /** Fields. */ public static final int A=1, B=2; /* ordinary */ int C; }`)
	for _, name := range []string{"A", "B"} {
		d := declarationNamed(t, f, name)
		if d.DocComment != "/** Fields. */" || len(d.Modifiers) != 3 {
			t.Fatalf("field metadata lost: %+v", d)
		}
	}
	if declarationNamed(t, f, "A").SignatureText != "A=1" || declarationNamed(t, f, "B").SignatureText != "B=2" {
		t.Fatal("field declarator signature convention changed")
	}
	if declarationNamed(t, f, "C").DocComment != "" {
		t.Fatal("ordinary comment became Javadoc")
	}
}

func TestRequirementSyntaxRecordsRoundTripAndBudgets(t *testing.T) {
	p := newTestParser(t)
	in := inputFor(`class P { int f(Object o) { Runnable r = () -> {}; if(o instanceof String s) return 1; return switch(o) { default -> 2; }; } }`)
	f := parseTest(t, p, in)
	if len(f.Lambdas) != 1 {
		t.Fatalf("lambda site missing from budgeted output: %+v", f.Lambdas)
	}
	data, _ := json.Marshal(f)
	var copy ir.SourceFile
	if err := json.Unmarshal(data, &copy); err != nil {
		t.Fatal(err)
	}
	if err := copy.Validate(); err != nil {
		t.Fatal(err)
	}
	next, _ := json.Marshal(copy)
	if string(data) != string(next) {
		t.Fatal("typed syntax changed on round trip")
	}
	records := len(f.Scopes) + len(f.Declarations) + len(f.Imports) + len(f.Types) + len(f.Expressions) + len(f.Calls) + len(f.CallableReferences) + len(f.Lambdas) + len(f.References) + len(f.TypeUses) + len(f.Annotations) + len(f.Statements) + len(f.Patterns)
	in.Limits.MaxIRRecords = uint64(records)
	parseTest(t, p, in)
	in.Limits.MaxIRRecords--
	_, err := p.Parse(context.Background(), in)
	if err == nil || !errors.Is(err, parser.ErrLimitExceeded) {
		t.Fatalf("syntax records not budgeted: %v", err)
	}
}

// Lambda sites are binding occurrences. The enclosing declaration is the
// innermost declaration around the lambda (a lambda is never its own owner;
// a variable initializer is owned by that variable), and the scope is where
// the lambda is written, exactly as calls record it.
func TestRequirementLambdaBindingSites(t *testing.T) {
	f := completeRequirement(t, newTestParser(t), `class P {
  Runnable field = () -> {};
  static { run(() -> {}); }
  P() { run(() -> {}); }
  void f() {
    run(a -> b -> a + b);
    run(() -> { use(); });
  }
  void g() { Runnable local = () -> {}; }
}`)
	if len(f.Lambdas) != 7 {
		t.Fatalf("lambda sites: %+v", f.Lambdas)
	}
	assertUniqueOccurrences(t, f)
	xs, ds := expressions(f), declarations(f)
	scopes := map[ir.ScopeID]ir.Scope{}
	for _, s := range f.Scopes {
		scopes[s.ID] = s
	}
	owners := map[ir.DeclarationKind]int{}
	bySpelling := map[string]ir.LambdaSite{}
	for _, l := range f.Lambdas {
		x := xs[l.ExpressionID]
		if x.Kind != ir.ExpressionLambda || x.Lambda == nil || x.OccurrenceID != l.Occurrence.ID || x.ScopeID != l.Occurrence.ScopeID || x.Span != l.Occurrence.Span {
			t.Fatalf("site and expression disagree: %+v", l)
		}
		owner := ds[l.Occurrence.EnclosingDeclarationID]
		owners[owner.Kind]++
		bySpelling[x.Spelling] = l
		// A local initializer names the local; its scope is still the body
		// scope owned by the callable declaring that local.
		context := owner
		if owner.Kind == ir.DeclarationLocal {
			context = ds[owner.OwnerID]
			if owner.Name != "local" || context.Name != "g" {
				t.Fatalf("local initializer lambda owner: %+v", owner)
			}
		}
		if scopes[l.Occurrence.ScopeID].OwnerDeclarationID != context.ID {
			t.Fatalf("site scope is not owned by the enclosing declaration: %+v", l.Occurrence)
		}
		if owner.Kind == ir.DeclarationField && (owner.Name != "field" || scopes[l.Occurrence.ScopeID].Kind != ir.ScopeInitializer) {
			t.Fatalf("field initializer lambda context: %+v", l.Occurrence)
		}
		for _, id := range l.ParameterIDs {
			p := ds[id]
			if p.Kind != ir.DeclarationParameter || p.OwnerID != owner.ID || p.DeclaringScopeID != x.Lambda.ScopeID {
				t.Fatalf("lambda parameter context: %+v", p)
			}
		}
		if l.BodyScopeID != x.Lambda.BodyScopeID || (l.BodyScopeID == "") != (x.Lambda.BodyStatementID == "") {
			t.Fatalf("body scope: %+v", l)
		}
		if l.BodyScopeID != "" && scopes[l.BodyScopeID].ParentID != x.Lambda.ScopeID {
			t.Fatalf("block body scope is not nested in the lambda scope: %+v", scopes[l.BodyScopeID])
		}
	}
	if owners[ir.DeclarationField] != 1 || owners[ir.DeclarationInitializer] != 1 || owners[ir.DeclarationConstructor] != 1 || owners[ir.DeclarationMethod] != 3 || owners[ir.DeclarationLocal] != 1 {
		t.Fatalf("enclosing declarations: %v", owners)
	}
	method := declarationNamed(t, f, "f")
	outer, inner, block := bySpelling["a -> b -> a + b"], bySpelling["b -> a + b"], bySpelling["() -> { use(); }"]
	if outer.Occurrence.EnclosingDeclarationID != method.ID || inner.Occurrence.EnclosingDeclarationID != method.ID || block.Occurrence.EnclosingDeclarationID != method.ID {
		t.Fatal("lambdas inside f must name f, never a lambda, as the enclosing declaration")
	}
	if inner.Occurrence.ScopeID != xs[outer.ExpressionID].Lambda.ScopeID || scopes[inner.Occurrence.ScopeID].Kind != ir.ScopeLambda || outer.Occurrence.ScopeID != block.Occurrence.ScopeID {
		t.Fatal("nested lambda scope context lost")
	}
	if len(outer.ParameterIDs) != 1 || ds[outer.ParameterIDs[0]].Name != "a" || len(inner.ParameterIDs) != 1 || ds[inner.ParameterIDs[0]].Name != "b" || len(block.ParameterIDs) != 0 {
		t.Fatal("lambda parameters lost")
	}
	if block.BodyScopeID == "" || inner.BodyScopeID != "" || outer.BodyScopeID != "" {
		t.Fatal("body scope reporting lost")
	}
	// Calls inside a lambda body share the site's enclosing declaration.
	var used bool
	for _, c := range f.Calls {
		if c.Name == "use" {
			used = c.Occurrence.EnclosingDeclarationID == block.Occurrence.EnclosingDeclarationID && c.Occurrence.ScopeID == block.BodyScopeID
		}
	}
	if !used {
		t.Fatal("call inside lambda body lost the site's context")
	}
}

// The report's retained corpus is an extraction conservation regression. This
// does not certify semantic targets, framework behavior or build visibility.
func TestMasterReportCorpusExtraction(t *testing.T) {
	data, err := os.ReadFile("testdata/master-review-fixtures.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name  string
		Files map[string]string
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	p := newTestParser(t)
	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			paths := make([]string, 0, len(c.Files))
			for path := range c.Files {
				paths = append(paths, path)
			}
			sort.Strings(paths)
			for _, path := range paths {
				source := c.Files[path]
				in := inputFor(source)
				in.Source.Path = path
				f := parseTest(t, p, in)
				if f.Coverage.Status != ir.ExtractionComplete {
					t.Fatalf("%s: %+v", path, f.Coverage.Issues)
				}
			}
		})
	}
}
