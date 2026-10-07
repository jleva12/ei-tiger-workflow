package java

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
)

// E02/E03 -> M04/M05/M21: preserve ordered fallthrough, default/null labels,
// loop headers and boolean flow inputs. These assertions do not select targets.
func TestRequirementFlowStructure(t *testing.T) {
	p := newTestParser(t)
	f := completeRequirement(t, p, `class P {
 int f(Object o, int n) {
  if (!(o instanceof String s) || s.isEmpty()) return 0;
  for (int i=0, j=1; i<n; i++,j++) { if (j==2) continue; }
  do { n--; } while(n>0);
  while(n>0) { break; }
  switch(n) { case 0: ; case 1: n++; break; default: n--; }
  return switch(o) { case String t when !t.isEmpty() -> t.length(); case null, default -> { yield 0; } };
 }
}`)
	ss := statements(f)
	var colon, arrows, empty, nullDefault, forHeaders, doBody, whileBody bool
	for _, s := range f.Statements {
		switch s.Kind {
		case ir.StatementSwitchArm:
			if s.Arrow {
				arrows = true
			} else {
				colon = true
			}
		case ir.StatementEmpty:
			empty = true
		case ir.StatementSwitchLabel:
			if s.Default && len(s.ExpressionIDs) == 1 {
				x := expressions(f)[s.ExpressionIDs[0]]
				nullDefault = x.Literal != nil && x.Literal.Kind == ir.LiteralNull
			}
		case ir.StatementFor:
			forHeaders = len(s.InitializerIDs) == 1 && len(ss[s.InitializerIDs[0]].DeclarationIDs) == 2 && len(s.UpdateIDs) == 2 && s.ConditionID != "" && s.BodyID != ""
		case ir.StatementDo:
			doBody = s.BodyID != "" && s.ConditionID != ""
		case ir.StatementWhile:
			whileBody = s.BodyID != "" && s.ConditionID != ""
		}
	}
	if !colon || !arrows || !empty || !nullDefault || !forHeaders || !doBody || !whileBody {
		t.Fatalf("flow structure lost: colon=%v arrow=%v empty=%v null/default=%v for=%v do=%v while=%v", colon, arrows, empty, nullDefault, forHeaders, doBody, whileBody)
	}
	var or bool
	for _, x := range f.Expressions {
		if x.Operator == "||" && len(x.OperandIDs) == 2 {
			or = true
		}
	}
	if !or {
		t.Fatal("short-circuit disjunction lost")
	}
}

func TestRequirementNestedRecordPatterns(t *testing.T) {
	f := completeRequirement(t, newTestParser(t), `import java.lang.annotation.*;
 @Target(ElementType.TYPE_USE) @interface A {}
 class P { record R(Object x) {} record W(R r) {}
 boolean f(Object o) { return o instanceof P.W(P.R(final @A String s)); }
 }`)
	if len(f.Patterns) != 3 {
		t.Fatalf("nested components lost: %+v", f.Patterns)
	}
	var records int
	for _, p := range f.Patterns {
		if p.Kind == ir.PatternRecord {
			records++
			if len(p.ComponentIDs) != 1 {
				t.Fatal("component order lost")
			}
		}
	}
	d := declarationNamed(t, f, "s")
	if records != 2 || d.Kind != ir.DeclarationPatternVariable || len(d.AnnotationIDs) != 1 || len(d.Modifiers) != 1 || d.Modifiers[0].Keyword != "final" {
		t.Fatalf("pattern declaration metadata lost: %+v", d)
	}
}

// E07 -> M13/M21: every emitted module record contributes to the hard budget.
func TestRequirementModuleRecordBudget(t *testing.T) {
	p := newTestParser(t)
	in := inputFor(`@Deprecated module m { requires static transitive n; exports p to q; opens p; uses p.S; provides p.S with p.A,p.B; }`)
	f := parseTest(t, p, in)
	count := len(f.Scopes) + len(f.Declarations) + len(f.Imports) + len(f.Types) + len(f.Expressions) + len(f.Calls) + len(f.CallableReferences) + len(f.Lambdas) + len(f.References) + len(f.TypeUses) + len(f.Annotations) + len(f.Statements) + len(f.Patterns) + 1 + len(f.Module.Directives)
	in.Limits.MaxIRRecords = uint64(count)
	parseTest(t, p, in)
	in.Limits.MaxIRRecords--
	got, err := p.Parse(context.Background(), in)
	if !errors.Is(err, parser.ErrLimitExceeded) || !reflect.DeepEqual(got, ir.SourceFile{}) {
		t.Fatalf("module records escaped budget: %v", err)
	}
}

func TestRequirementUnicodeSupplementaryIdentity(t *testing.T) {
	// Deseret capital letter U+10400 is a valid supplementary Java identifier.
	source := "// 😀\r\n" + `cl\u0061ss \uD801\uDC00 { int 𐐀; void f(){ \uD801\uDC00 \u003d 1; } }`
	f := completeRequirement(t, newTestParser(t), source)
	var count int
	for _, d := range f.Declarations {
		if d.Name == "𐐀" {
			count++
			if d.NameSpan == nil || d.NameSpan.Start.Line != 2 {
				t.Fatal("original line lost")
			}
		}
	}
	if count != 2 {
		t.Fatalf("supplementary identifiers lost: %+v", f.Declarations)
	}
	for _, x := range f.Expressions {
		if x.Spelling != source[x.Span.Start.ByteOffset:x.Span.End.ByteOffset] {
			t.Fatal("original byte span mismatch")
		}
	}
	if len(f.References) != 1 || f.References[0].Access != ir.AccessWrite {
		t.Fatalf("escaped assignment not recognized: %+v", f.References)
	}
}

// Consumer-facing corruption checks cover new cross-table links and payloads.
func TestRequirementSyntaxIntegrity(t *testing.T) {
	p := newTestParser(t)
	const source = `@interface A { A2 value() default @A2; } @interface A2 {} class P { Object f(Object o) { Runnable r=()->{use();}; if(o instanceof String s) return s; return switch(o){default -> new Box<>();}; } }`
	tests := []struct {
		name   string
		mutate func(*ir.SourceFile)
		want   string
	}{
		{"missing_argument_syntax", func(f *ir.SourceFile) {
			f.Types = append(f.Types, ir.TypeRef{ID: "explicit-type", Kind: ir.TypeNamed, ScopeID: f.RootScopeID, Span: f.Types[0].Span, Spelling: "List<T>", Named: &ir.NamedType{Segments: []ir.TypeSegment{{Name: "List", TypeArguments: []ir.TypeRefID{f.Types[0].ID}}}}})
		}, "require explicit argument syntax"},
		{"competing_body", func(f *ir.SourceFile) {
			for i := range f.Declarations {
				if c := f.Declarations[i].Callable; c != nil && c.BodyStatementID != "" {
					c.BodyExpressionID = f.Expressions[0].ID
					return
				}
			}
		}, "statement body competes"},
		{"old_schema", func(f *ir.SourceFile) { f.SchemaVersion = "1.0.0" }, "schema_version"},
		{"dangling_statement", func(f *ir.SourceFile) { f.Statements[0].ChildIDs = []ir.StatementID{"missing"} }, "child_ids"},
		{"statement_cycle", func(f *ir.SourceFile) { f.Statements[0].ChildIDs = []ir.StatementID{f.Statements[0].ID} }, "syntax containment: cycle"},
		{"lambda_statement_cycle", func(f *ir.SourceFile) {
			for _, x := range f.Expressions {
				if x.Lambda != nil {
					for i := range f.Statements {
						if f.Statements[i].ID == x.Lambda.BodyStatementID {
							f.Statements[i].ExpressionIDs = append(f.Statements[i].ExpressionIDs, x.ID)
							return
						}
					}
				}
			}
		}, "syntax containment: cycle"},
		{"pattern_declaration", func(f *ir.SourceFile) {
			for i := range f.Declarations {
				if f.Declarations[i].Kind == ir.DeclarationPatternVariable {
					f.Declarations[i].Kind = ir.DeclarationLocal
				}
			}
		}, "pattern declaration"},
		{"pattern_components", func(f *ir.SourceFile) { f.Patterns[0].ComponentIDs = []ir.PatternID{f.Patterns[0].ID} }, "only record patterns"},
		{"switch_body", func(f *ir.SourceFile) {
			for i := range f.Expressions {
				if f.Expressions[i].Switch != nil {
					f.Expressions[i].Switch.BodyStatementID = "missing"
					return
				}
			}
		}, "body_statement_id"},
		{"diamond_arguments", func(f *ir.SourceFile) {
			for i := range f.Types {
				if f.Types[i].Named != nil {
					for j := range f.Types[i].Named.Segments {
						if f.Types[i].Named.Segments[j].ArgumentSyntax == ir.TypeArgumentsDiamond {
							f.Types[i].Named.Segments[j].TypeArguments = []ir.TypeRefID{f.Types[0].ID}
							return
						}
					}
				}
			}
		}, "diamond"},
		{"annotation_default", func(f *ir.SourceFile) {
			for i := range f.Declarations {
				if c := f.Declarations[i].Callable; c != nil && c.AnnotationDefault != nil {
					c.AnnotationDefault.AnnotationID = "missing"
					return
				}
			}
		}, "annotation_id"},
		{"missing_lambda_site", func(f *ir.SourceFile) { f.Lambdas = nil }, "matching lambda record required"},
		{"lambda_site_scope", func(f *ir.SourceFile) { f.Lambdas[0].Occurrence.ScopeID = f.RootScopeID }, "expression and occurrence must agree"},
		{"lambda_site_parameter_kind", func(f *ir.SourceFile) { f.Lambdas[0].ParameterIDs = []ir.DeclarationID{f.Declarations[0].ID} }, "lambda parameter must be a parameter declaration"},
		{"lambda_site_body_scope", func(f *ir.SourceFile) { f.Lambdas[0].BodyScopeID = "missing" }, "lambdas[0].body_scope_id"},
		{"duplicate_lambda_occurrence", func(f *ir.SourceFile) { f.Lambdas[0].Occurrence.ID = f.Calls[0].Occurrence.ID }, "duplicate occurrence"},
		{"invalid_default_flag", func(f *ir.SourceFile) { f.Statements[0].Default = true }, "default requires switch label"},
		{"language_status", func(f *ir.SourceFile) { f.LanguageValidation.Status = "valid" }, "language_validation.status"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			f := completeRequirement(t, p, source)
			tc.mutate(&f)
			err := f.Validate()
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v, expected %q", err, tc.want)
			}
		})
	}
	f := completeRequirement(t, p, `module m { provides p.S with p.Impl; }`)
	f.Module.Directives[0].Kind = ir.ModuleUses
	if err := f.Validate(); err == nil || !strings.Contains(err.Error(), "providers only valid") {
		t.Fatalf("incompatible module payload accepted: %v", err)
	}
}

func TestRequirementStructuralQueryGuard(t *testing.T) {
	newTestParser(t)
	for _, source := range []string{`((identifier) @name (#eq? @name "x"))`, `((identifier) @name (#custom? @name))`, `((identifier) @name (#is? local))`} {
		query, err := structuralQuery(shared.language, source)
		if query != nil {
			query.Close()
			t.Fatal("unsafe query accepted")
		}
		if err == nil {
			t.Fatal("missing query diagnostic")
		}
	}
}
