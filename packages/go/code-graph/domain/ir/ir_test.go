package ir_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"strconv"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/ir"
)

func fixture(t *testing.T) ir.SourceFile {
	t.Helper()
	data, err := os.ReadFile("testdata/parsed_file.json")
	if err != nil {
		t.Fatal(err)
	}
	var f ir.SourceFile
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&f); err != nil {
		t.Fatal(err)
	}
	if err := f.Validate(); err != nil {
		t.Fatalf("invalid fixture: %v", err)
	}
	return f
}

func TestSourceIdentityAndBytePositions(t *testing.T) {
	f := fixture(t)
	source, err := os.ReadFile("testdata/Demo.java")
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(source)
	if f.Source.ContentSHA256 != hex.EncodeToString(sum[:]) || f.Source.SizeBytes != uint64(len(source)) {
		t.Fatal("IR must refer to the exact original source bytes")
	}
	// The initial comment contains a multibyte character. Byte positions must
	// still recover exact receiver expressions after serialization.
	for _, e := range f.Expressions {
		if got := string(source[e.Span.Start.ByteOffset:e.Span.End.ByteOffset]); got != e.Spelling {
			t.Errorf("%s span selects %q, want %q", e.ID, got, e.Spelling)
		}
	}
}

func TestJSONRoundTripPreservesBindingInputs(t *testing.T) {
	original := fixture(t)
	data, err := json.Marshal(original)
	if err != nil {
		t.Fatal(err)
	}
	var f ir.SourceFile
	if err := json.Unmarshal(data, &f); err != nil {
		t.Fatal(err)
	}
	if err := f.Validate(); err != nil {
		t.Fatal(err)
	}
	reencoded, err := json.Marshal(f)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(data, reencoded) {
		t.Fatal("canonical JSON changed across a typed round trip")
	}

	if got := f.Imports[0].Name.Segments; len(got) != 2 || got[0].Text != "outside" || got[1].Text != "Client" {
		t.Fatal("explicit external import qualification was lost")
	}
	if f.Imports[0].Kind != ir.ImportSingleType {
		t.Fatal("explicit import kind lost")
	}
	if len(f.Calls) != 6 || len(f.CallableReferences) != 1 {
		t.Fatal("calls and deferred references must be separate occurrence tables")
	}
	calls := map[ir.OccurrenceID]ir.Call{}
	for _, c := range f.Calls {
		calls[c.Occurrence.ID] = c
	}
	before, inner, after := calls["call:save1"], calls["call:save2"], calls["call:save3"]
	if before.Occurrence.ScopeID != "scope:run" || inner.Occurrence.ScopeID != "scope:block" || after.Occurrence.ScopeID != "scope:run" {
		t.Fatal("block shadowing context was lost")
	}
	if before.Occurrence.Span.Start.ByteOffset >= inner.Occurrence.Span.Start.ByteOffset || inner.Occurrence.Span.Start.ByteOffset >= after.Occurrence.Span.Start.ByteOffset {
		t.Fatal("distinct call positions were lost")
	}
	if before.Arguments[0].ExpressionID == after.Arguments[0].ExpressionID {
		t.Fatal("distinct argument occurrences collapsed")
	}
	if calls["call:save4"].ReceiverID != "expr:next" || calls["call:next"].ReceiverID != "expr:receiver4" {
		t.Fatal("nested receiver expression was reduced to a call name")
	}
	if calls["call:this"].Kind != ir.CallThisConstructor {
		t.Fatal("constructor delegation became an ordinary call")
	}
	if f.CallableReferences[0].Kind != ir.CallableMethodReference || f.CallableReferences[0].Name != "accept" {
		t.Fatal("method reference identity was lost")
	}
	list := f.Types[0]
	if list.Named.Segments[0].Name != "java" || list.Named.Segments[1].Name != "util" || list.Named.Segments[2].Name != "List" || list.Named.Segments[2].TypeArguments[0] != "type:t-use" {
		t.Fatal("qualified generic type structure was lost")
	}
	if f.TypeUses[1].ParentTypeID != list.ID || f.TypeUses[1].Occurrence.EnclosingDeclarationID != "decl:values" {
		t.Fatal("generic field dependency lost its declaration owner")
	}
	args := f.Annotations[0].Arguments
	if len(args) != 2 || args[0].Name != "path" || args[0].Value.Kind != ir.AnnotationArray || len(args[0].Value.Elements) != 2 || args[1].Name != "produces" {
		t.Fatal("mapping paths and media-type arguments must remain distinct")
	}
}

func TestValidateRejectsCorruptContracts(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*ir.SourceFile)
		want   string
	}{
		{"version", func(f *ir.SourceFile) { f.SchemaVersion = "2.0.0" }, "schema_version"},
		{"path_escape", func(f *ir.SourceFile) { f.Source.Path = "../Demo.java" }, "source.path"},
		{"invalid_source_utf8", func(f *ir.SourceFile) { f.Source.RepositoryID = "\xff" }, "source.repository_id"},
		{"unknown_receiver", func(f *ir.SourceFile) { f.Calls[1].ReceiverID = "missing" }, "calls[1].receiver_id"},
		{"missing_invocation_callee", func(f *ir.SourceFile) { f.Calls[1].Kind = ir.CallInvocation }, "calls[1].callee_id"},
		{"callee_cycle", func(f *ir.SourceFile) { f.Calls[1].CalleeID = f.Calls[1].ExpressionID }, "expressions: cycle"},
		{"invalid_argument_expansion", func(f *ir.SourceFile) { f.Calls[1].Arguments[0].Expansion = "guessed" }, "expansion"},
		{"missing_decorator", func(f *ir.SourceFile) { f.Declarations[0].DecoratorExpressionIDs = []ir.ExpressionID{"missing"} }, "decorator_expression_ids"},
		{"duplicate_occurrence", func(f *ir.SourceFile) { f.Imports[0].Occurrence.ID = f.Calls[0].Occurrence.ID }, "duplicate occurrence"},
		{"dangling_type_argument", func(f *ir.SourceFile) { f.Types[0].Named.Segments[2].TypeArguments[0] = "missing" }, "type_arguments[0]"},
		{"scope_cycle", func(f *ir.SourceFile) { f.Scopes[1].ParentID = f.Scopes[1].ID }, "scopes.parent_id"},
		{"owner_cycle", func(f *ir.SourceFile) { f.Declarations[0].OwnerID = f.Declarations[0].ID }, "declarations.owner_id"},
		{"receiver_cycle", func(f *ir.SourceFile) { f.Calls[1].ReceiverID = f.Calls[1].ExpressionID }, "expressions: cycle"},
		{"type_cycle", func(f *ir.SourceFile) { f.Types[0].Named.Segments[2].TypeArguments[0] = f.Types[0].ID }, "types: cycle"},
		{"competing_declaration_payloads", func(f *ir.SourceFile) { f.Declarations[0].Callable = &ir.CallableDeclaration{} }, "exactly one detail payload"},
		{"constructor_return_type", func(f *ir.SourceFile) { f.Declarations[4].Callable.ReturnTypeID = "type:void:run" }, "constructors have no return type"},
		{"missing_call_record", func(f *ir.SourceFile) { f.Calls = f.Calls[1:] }, "matching call record"},
		{"wrong_call_reference_kind", func(f *ir.SourceFile) { f.Expressions[1].Kind = ir.ExpressionCallableReference }, "expression and occurrence must agree"},
		{"outside_source", func(f *ir.SourceFile) { f.Calls[0].Occurrence.Span.End.ByteOffset = f.Source.SizeBytes + 1 }, "outside the source"},
		{"wrong_type_payload", func(f *ir.SourceFile) { f.Types[0].Kind = ir.TypeVoid }, "named payload"},
		{"annotation_payload", func(f *ir.SourceFile) { f.Annotations[0].Arguments[0].Value.ExpressionID = "expr:a" }, "competing payloads"},
		{"unknown_kind", func(f *ir.SourceFile) { f.Calls[1].Kind = "guessed_http_call" }, "unsupported value"},
		{"silent_loss", func(f *ir.SourceFile) { f.Coverage.Status = ir.ExtractionComplete }, "complete extraction cannot"},
		{"unexplained_partial", func(f *ir.SourceFile) { f.Coverage.Issues = nil }, "incomplete extraction requires"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := fixture(t)
			tc.mutate(&f)
			err := f.Validate()
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v; want an error containing %q", err, tc.want)
			}
		})
	}
}

// Lambda sites are occurrence-bearing binding sites: their identities share
// the occurrence namespace with every other table and each site must agree
// with the lambda expression it names, exactly as calls do.
func TestLambdaSiteContract(t *testing.T) {
	withLambda := func(t *testing.T) ir.SourceFile {
		t.Helper()
		f := fixture(t)
		var span ir.Span
		for _, x := range f.Expressions {
			if x.ID == "expr:call3" {
				span = x.Span
			}
		}
		f.Scopes = append(f.Scopes, ir.Scope{ID: "scope:lambda", Kind: ir.ScopeLambda, Span: span, ParentID: "scope:run", OwnerDeclarationID: "decl:run"})
		f.Declarations = append(f.Declarations, ir.Declaration{ID: "decl:x", Kind: ir.DeclarationParameter, Name: "x", Span: span, DeclaringScopeID: "scope:lambda", OwnerID: "decl:run", Variable: &ir.VariableDeclaration{}})
		f.Expressions = append(f.Expressions, ir.Expression{ID: "expr:lambda", Kind: ir.ExpressionLambda, Span: span, ScopeID: "scope:run", Spelling: "x -> 3", OccurrenceID: "lambda:site", Lambda: &ir.Lambda{ScopeID: "scope:lambda", ParameterIDs: []ir.DeclarationID{"decl:x"}, BodyExpressionID: "expr:arg3"}})
		f.Lambdas = append(f.Lambdas, ir.LambdaSite{Occurrence: ir.Occurrence{ID: "lambda:site", Span: span, ScopeID: "scope:run", EnclosingDeclarationID: "decl:run"}, ExpressionID: "expr:lambda", ParameterIDs: []ir.DeclarationID{"decl:x"}})
		if err := f.Validate(); err != nil {
			t.Fatalf("well-formed lambda site rejected: %v", err)
		}
		return f
	}
	original := withLambda(t)
	data, err := json.Marshal(original)
	if err != nil {
		t.Fatal(err)
	}
	var decoded ir.SourceFile
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if err := decoded.Validate(); err != nil {
		t.Fatal(err)
	}
	if len(decoded.Lambdas) != 1 || decoded.Lambdas[0].Occurrence.ID != "lambda:site" || decoded.Lambdas[0].Occurrence.EnclosingDeclarationID != "decl:run" || decoded.Lambdas[0].ParameterIDs[0] != "decl:x" {
		t.Fatalf("lambda site identity lost across serialization: %+v", decoded.Lambdas)
	}
	last := len(original.Expressions) - 1
	cases := []struct {
		name   string
		mutate func(*ir.SourceFile)
		want   string
	}{
		{"missing_site", func(f *ir.SourceFile) { f.Lambdas = nil }, "matching lambda record required"},
		{"unowned_lambda_expression", func(f *ir.SourceFile) { f.Expressions[last].OccurrenceID = "" }, "expressions[" + strconv.Itoa(last) + "].occurrence_id"},
		{"dangling_expression", func(f *ir.SourceFile) { f.Lambdas[0].ExpressionID = "missing" }, "lambdas[0].expression_id"},
		{"site_names_a_call", func(f *ir.SourceFile) { f.Lambdas[0].ExpressionID = "expr:call3" }, "expression and occurrence must agree"},
		{"span_drift", func(f *ir.SourceFile) { f.Lambdas[0].Occurrence.Span.End = f.Lambdas[0].Occurrence.Span.Start }, "expression and occurrence must agree"},
		{"scope_drift", func(f *ir.SourceFile) { f.Lambdas[0].Occurrence.ScopeID = "scope:block" }, "expression and occurrence must agree"},
		{"duplicate_across_tables", func(f *ir.SourceFile) {
			f.Lambdas[0].Occurrence.ID = "call:save3"
			f.Expressions[last].OccurrenceID = "call:save3"
		}, "duplicate occurrence"},
		{"non_parameter", func(f *ir.SourceFile) {
			f.Lambdas[0].ParameterIDs = []ir.DeclarationID{"decl:local"}
			f.Expressions[last].Lambda.ParameterIDs = []ir.DeclarationID{"decl:local"}
		}, "lambda parameter must be a parameter declaration"},
		{"payload_disagreement", func(f *ir.SourceFile) { f.Lambdas[0].ParameterIDs = nil }, "must agree on parameters and body scope"},
		{"dangling_body_scope", func(f *ir.SourceFile) { f.Lambdas[0].BodyScopeID = "scope:missing" }, "lambdas[0].body_scope_id"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := withLambda(t)
			tc.mutate(&f)
			err := f.Validate()
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v; want an error containing %q", err, tc.want)
			}
		})
	}
}

func TestOptionalCountsAndLiteralPrecision(t *testing.T) {
	zero := uint64(0)
	type payload struct {
		Issues     []ir.CoverageIssue
		Literals   []ir.Literal
		EmptyArray ir.AnnotationValue
	}
	values := payload{
		Issues: []ir.CoverageIssue{{Count: nil}, {Count: &zero}},
		Literals: []ir.Literal{
			{Kind: ir.LiteralInteger, Lexeme: "9223372036854775807L"},
			{Kind: ir.LiteralFloating, Lexeme: "0x1.fffffffffffffp1023"},
			{Kind: ir.LiteralBoolean, Lexeme: "false"},
			{Kind: ir.LiteralNull, Lexeme: "null"},
		},
		EmptyArray: ir.AnnotationValue{Kind: ir.AnnotationArray},
	}
	data, err := json.Marshal(values)
	if err != nil {
		t.Fatal(err)
	}
	var decoded payload
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.Issues[0].Count != nil || decoded.Issues[1].Count == nil || *decoded.Issues[1].Count != 0 {
		t.Fatal("unknown and known-zero losses were conflated")
	}
	for i, l := range decoded.Literals {
		if l != values.Literals[i] {
			t.Fatal("literal syntax or precision was lost")
		}
	}
	if decoded.EmptyArray.Kind != ir.AnnotationArray || len(decoded.EmptyArray.Elements) != 0 {
		t.Fatal("empty annotation array lost its kind")
	}
}

func TestQualifiedNestedGenericType(t *testing.T) {
	typ := ir.TypeRef{
		Kind: ir.TypeNamed, Spelling: "demo.Outer<String>.Inner<T>",
		Named: &ir.NamedType{Segments: []ir.TypeSegment{
			{Name: "demo"},
			{Name: "Outer", TypeArguments: []ir.TypeRefID{"type:string"}},
			{Name: "Inner", TypeArguments: []ir.TypeRefID{"type:t"}},
		}},
	}
	data, err := json.Marshal(typ)
	if err != nil {
		t.Fatal(err)
	}
	var got ir.TypeRef
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatal(err)
	}
	if got.Named.Segments[1].TypeArguments[0] != "type:string" || got.Named.Segments[2].TypeArguments[0] != "type:t" {
		t.Fatal("outer/inner generic arguments were flattened")
	}
}
