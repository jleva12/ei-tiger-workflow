package java

import (
	"os"
	"reflect"
	"sort"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/ir"
)

// These fixtures assert independently written syntax expectations. Neither the
// original resolver's output nor serialized/generated IR is the test oracle.
// M01/M04/M05/M13/M21: extraction facts, not selected semantic targets.
func TestIndependentRegressionFixtures(t *testing.T) {
	p := newTestParser(t)
	load := func(t *testing.T, name string, complete bool) (ir.SourceFile, string) {
		t.Helper()
		data, err := os.ReadFile("testdata/regression/" + name + ".java")
		if err != nil {
			t.Fatal(err)
		}
		source := string(data)
		f := parseTest(t, p, inputFor(source))
		if complete && f.Coverage.Status != ir.ExtractionComplete {
			t.Fatalf("unexpected loss: %+v", f.Coverage)
		}
		return f, source
	}
	orderedCalls := func(f ir.SourceFile) []ir.Call {
		calls := append([]ir.Call(nil), f.Calls...)
		sort.Slice(calls, func(i, j int) bool {
			return calls[i].Occurrence.Span.Start.ByteOffset < calls[j].Occurrence.Span.Start.ByteOffset
		})
		return calls
	}
	t.Run("shadowing", func(t *testing.T) {
		f, _ := load(t, "Shadowing", true)
		calls := orderedCalls(f)
		if len(calls) != 3 {
			t.Fatalf("expected three save occurrences, got %d", len(calls))
		}
		xs := expressions(f)
		method := declarationNamed(t, f, "run")
		var field, local ir.Declaration
		for _, d := range f.Declarations {
			if d.Name == "service" {
				switch d.Kind {
				case ir.DeclarationField:
					field = d
				case ir.DeclarationLocal:
					local = d
				}
			}
		}
		if field.ID == "" || local.ID == "" || field.ID == local.ID || field.DeclaringScopeID == local.DeclaringScopeID || local.OwnerID != method.ID {
			t.Fatal("distinct shadowing declarations/owners lost")
		}
		if calls[0].Occurrence.ScopeID == calls[1].Occurrence.ScopeID || calls[0].Occurrence.ScopeID != calls[2].Occurrence.ScopeID || calls[1].Occurrence.ScopeID != local.DeclaringScopeID {
			t.Fatal("inner block or restored outer scope lost")
		}
		var inner ir.Scope
		for _, s := range f.Scopes {
			if s.ID == local.DeclaringScopeID {
				inner = s
			}
		}
		if inner.Kind != ir.ScopeBlock || inner.ParentID != calls[0].Occurrence.ScopeID {
			t.Fatal("shadowing scope parent lost")
		}
		for _, c := range calls {
			if c.Name != "save" || xs[c.ReceiverID].Spelling != "service" || c.Occurrence.EnclosingDeclarationID != method.ID || xs[c.ReceiverID].ScopeID != c.Occurrence.ScopeID {
				t.Fatal("receiver occurrence context lost")
			}
		}
	})
	t.Run("repeated_calls", func(t *testing.T) {
		f, source := load(t, "Repeated", true)
		calls := orderedCalls(f)
		if len(calls) != 3 {
			t.Fatalf("identical calls collapsed or fabricated: got %d", len(calls))
		}
		xs := expressions(f)
		occurrences := map[ir.OccurrenceID]bool{}
		expressionIDs := map[ir.ExpressionID]bool{}
		lineStart := strings.Index(source, "  void run()")
		for i, column := range []uint32{15, 23, 31} {
			c := calls[i]
			want := ir.Position{Line: 3, Column: column, ByteOffset: uint64(lineStart) + uint64(column)}
			end := ir.Position{Line: 3, Column: column + 6, ByteOffset: want.ByteOffset + 6}
			if c.Occurrence.Span != (ir.Span{Start: want, End: end}) {
				t.Fatalf("call %d: span=%+v, want [%+v,%+v)", i, c.Occurrence.Span, want, end)
			}
			if c.Kind != ir.CallMethod || c.Name != "ping" || c.ReceiverID != "" || len(c.Arguments) != 0 || xs[c.ExpressionID].Spelling != "ping()" {
				t.Fatalf("call %d changed shape: %+v", i, c)
			}
			if c.Occurrence.ID == "" || occurrences[c.Occurrence.ID] || expressionIDs[c.ExpressionID] {
				t.Fatal("repeated occurrences share an identity")
			}
			occurrences[c.Occurrence.ID], expressionIDs[c.ExpressionID] = true, true
		}
		method := declarationNamed(t, f, "run")
		ss := statements(f)
		body := ss[method.Callable.BodyStatementID]
		if len(body.ChildIDs) != 3 {
			t.Fatal("repeated statement occurrences lost")
		}
		for i, id := range body.ChildIDs {
			if !reflect.DeepEqual(ss[id].ExpressionIDs, []ir.ExpressionID{calls[i].ExpressionID}) {
				t.Fatal("statement-to-call order or identity lost")
			}
		}
	})
	t.Run("qualified_names", func(t *testing.T) {
		f, _ := load(t, "Qualified", true)
		if len(f.Imports) != 3 {
			t.Fatal("import count changed")
		}
		for i, want := range []struct {
			kind  ir.ImportKind
			names []string
		}{{ir.ImportSingleType, []string{"outside", "Client"}}, {ir.ImportSingleStatic, []string{"outside", "Tools", "save"}}, {ir.ImportTypeOnDemand, []string{"other"}}} {
			var names []string
			for _, s := range f.Imports[i].Name.Segments {
				names = append(names, s.Text)
			}
			if f.Imports[i].Kind != want.kind || !reflect.DeepEqual(names, want.names) {
				t.Fatalf("qualified import %d: %+v", i, f.Imports[i])
			}
		}
		ts := types(f)
		assertType := func(id ir.TypeRefID, names ...string) ir.TypeRef {
			t.Helper()
			typ := ts[id]
			if typ.Kind != ir.TypeNamed || typ.Named == nil {
				t.Fatalf("missing named type: %+v", typ)
			}
			var got []string
			for _, s := range typ.Named.Segments {
				got = append(got, s.Name)
			}
			if !reflect.DeepEqual(got, names) {
				t.Fatalf("qualified type=%v, want %v", got, names)
			}
			return typ
		}
		m := assertType(declarationNamed(t, f, "values").Variable.DeclaredTypeID, "java", "util", "Map")
		args := m.Named.Segments[2]
		if args.ArgumentSyntax != ir.TypeArgumentsExplicit || len(args.TypeArguments) != 2 {
			t.Fatal("generic argument association lost")
		}
		assertType(args.TypeArguments[0], "outside", "Key")
		assertType(args.TypeArguments[1], "other", "Value")
		assertType(declarationNamed(t, f, "client").Variable.DeclaredTypeID, "outside", "Client")
		if len(f.Calls) != 1 || f.Calls[0].Kind != ir.CallObjectCreation {
			t.Fatal("construction changed kind")
		}
		assertType(f.Calls[0].ConstructedTypeID, "outside", "Client")
	})
	t.Run("receiver_chains", func(t *testing.T) {
		f, _ := load(t, "Receivers", true)
		if len(f.Calls) != 3 {
			t.Fatalf("expected three invocations: %+v", f.Calls)
		}
		byName := map[string]ir.Call{}
		for _, c := range f.Calls {
			byName[c.Name] = c
		}
		xs := expressions(f)
		next, client, save := byName["next"], byName["client"], byName["save"]
		if next.ExpressionID == "" || client.ExpressionID == "" || save.ExpressionID == "" {
			t.Fatal("chain segment missing")
		}
		if client.ReceiverID != next.ExpressionID || save.ReceiverID != client.ExpressionID {
			t.Fatal("receiver chain flattened")
		}
		if xs[next.ReceiverID].Kind != ir.ExpressionName || xs[next.ReceiverID].Spelling != "service" || xs[client.ReceiverID].Kind != ir.ExpressionCall || xs[save.ReceiverID].Kind != ir.ExpressionCall {
			t.Fatal("receiver kinds changed")
		}
		for name, want := range map[string]string{"next": "service.next()", "client": "service.next().client()", "save": "service.next().client().save()"} {
			if xs[byName[name].ExpressionID].Spelling != want {
				t.Fatalf("%s lost full source expression", name)
			}
		}
	})
	t.Run("lambda_site", func(t *testing.T) {
		f, source := load(t, "Lambda", true)
		if len(f.Lambdas) != 1 {
			t.Fatalf("expected one lambda site, got %d", len(f.Lambdas))
		}
		site := f.Lambdas[0]
		xs, ds := expressions(f), declarations(f)
		x := xs[site.ExpressionID]
		if x.Kind != ir.ExpressionLambda || x.Lambda == nil || x.OccurrenceID != site.Occurrence.ID {
			t.Fatalf("lambda expression does not link back to its site: %+v", x)
		}
		if x.Span != site.Occurrence.Span || x.ScopeID != site.Occurrence.ScopeID || source[site.Occurrence.Span.Start.ByteOffset:site.Occurrence.Span.End.ByteOffset] != "item -> transform(item)" {
			t.Fatalf("site span/scope disagree with the expression: %+v", site.Occurrence)
		}
		method := declarationNamed(t, f, "run")
		if site.Occurrence.EnclosingDeclarationID != method.ID {
			t.Fatalf("enclosing declaration %s, want method %s", site.Occurrence.EnclosingDeclarationID, method.ID)
		}
		calls := orderedCalls(f)
		if len(calls) != 2 || calls[0].Name != "accept" || site.Occurrence.ScopeID != calls[0].Occurrence.ScopeID {
			t.Fatalf("lambda scope differs from the call it is an argument of: %+v", calls)
		}
		if len(site.ParameterIDs) != 1 {
			t.Fatalf("lambda parameters: %+v", site.ParameterIDs)
		}
		item := ds[site.ParameterIDs[0]]
		if item.Kind != ir.DeclarationParameter || item.Name != "item" || item.OwnerID != method.ID || item.DeclaringScopeID != x.Lambda.ScopeID {
			t.Fatalf("lambda parameter binding lost: %+v", item)
		}
		if site.BodyScopeID != "" || x.Lambda.BodyExpressionID == "" || xs[x.Lambda.BodyExpressionID].ScopeID != x.Lambda.ScopeID {
			t.Fatal("expression body context lost")
		}
		if calls[1].Name != "transform" || calls[1].Occurrence.ScopeID != x.Lambda.ScopeID || calls[1].Occurrence.EnclosingDeclarationID != method.ID {
			t.Fatalf("call inside the lambda body lost its context: %+v", calls[1])
		}
		assertUniqueOccurrences(t, f)
	})
	t.Run("unsupported_syntax", func(t *testing.T) {
		f, source := load(t, "Unsupported", false)
		if f.Coverage.Status != ir.ExtractionPartial {
			t.Fatalf("unsupported syntax must be partial: %+v", f.Coverage)
		}
		var issue, diagnostic, unknown bool
		for _, i := range f.Coverage.Issues {
			if i.Feature == "preview_syntax" && i.Reason == ir.CoverageUnsupported && i.Span != nil {
				issue = true
			}
		}
		for _, d := range f.Diagnostics {
			if d.Code == "java.preview_syntax" && d.Severity == ir.SeverityWarning && d.Span != nil {
				diagnostic = true
			}
		}
		for _, x := range f.Expressions {
			if x.Kind == ir.ExpressionUnknown && x.SyntaxKind == "template_expression" && x.Spelling == `STR."hello \{name}"` && x.Spelling == source[x.Span.Start.ByteOffset:x.Span.End.ByteOffset] {
				unknown = true
			}
		}
		if !issue || !diagnostic || !unknown {
			t.Fatalf("unsupported evidence lost: issue=%v diagnostic=%v unknown=%v; %+v", issue, diagnostic, unknown, f.Coverage)
		}
		if len(f.Calls) != 0 {
			t.Fatal("unsupported expression fabricated an invocation")
		}
	})
}
