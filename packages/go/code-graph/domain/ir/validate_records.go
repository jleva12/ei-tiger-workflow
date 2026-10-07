package ir

import (
	"fmt"
	"slices"
)

func (v *validator) declaration(p string, d Declaration) {
	payloads := 0
	for _, present := range []bool{d.Type != nil, d.Callable != nil, d.Variable != nil, d.TypeParameter != nil, d.Initializer != nil} {
		if present {
			payloads++
		}
	}
	if payloads != 1 {
		v.problem(p, "declaration requires exactly one detail payload")
	}
	switch d.Kind {
	case DeclarationClass, DeclarationInterface, DeclarationEnum, DeclarationRecord, DeclarationAnnotationType, DeclarationTypeAlias, DeclarationNamespace:
		if d.Type == nil {
			v.problem(p+".type", "type payload required")
		}
	case DeclarationMethod, DeclarationConstructor, DeclarationFunction:
		if d.Callable == nil {
			v.problem(p+".callable", "callable payload required")
		}
	case DeclarationField, DeclarationParameter, DeclarationReceiver, DeclarationLocal, DeclarationRecordComponent, DeclarationEnumConstant, DeclarationPatternVariable, DeclarationVariable:
		if d.Variable == nil {
			v.problem(p+".variable", "variable payload required")
		}
	case DeclarationTypeParameter:
		if d.TypeParameter == nil {
			v.problem(p+".type_parameter", "type parameter payload required")
		}
	case DeclarationInitializer:
		if d.Initializer == nil {
			v.problem(p+".initializer", "initializer payload required")
		}
	default:
		v.problem(p+".kind", "unsupported declaration kind")
	}
	if d.Kind != DeclarationInitializer && !(d.Type != nil && d.Type.Form == TypeAnonymous) {
		v.required(p+".name", d.Name)
	}
	if d.Type != nil {
		v.choice(p+".type.form", string(d.Type.Form), "top_level", "member", "local", "anonymous")
		refs(v, "declaration", p+".type.type_parameter_ids", d.Type.TypeParameterIDs)
		refs(v, "declaration", p+".type.record_component_ids", d.Type.RecordComponentIDs)
		for i, h := range d.Type.Heritage {
			hp := fmt.Sprintf("%s.type.heritage[%d]", p, i)
			v.choice(hp+".kind", string(h.Kind), "extends", "implements", "permits")
			v.ref("type", string(h.TypeRefID), hp+".type_ref_id", true)
			v.span(hp+".span", h.Span)
		}
	}
	if c := d.Callable; c != nil {
		refs(v, "declaration", p+".callable.parameter_ids", c.ParameterIDs)
		v.ref("statement", string(c.BodyStatementID), p+".callable.body_statement_id", false)
		if c.BodyStatementID != "" && c.BodyExpressionID != "" {
			v.problem(p+".callable", "statement body competes with expression body")
		}
		if c.AnnotationDefault != nil {
			v.annotationValue(p+".callable.annotation_default", *c.AnnotationDefault, 0)
			if c.DefaultValueID != "" {
				v.problem(p+".callable", "annotation default competes with expression default")
			}
		}
		refs(v, "declaration", p+".callable.type_parameter_ids", c.TypeParameterIDs)
		refs(v, "type", p+".callable.throws_type_ids", c.ThrowsTypeIDs)
		v.ref("type", string(c.ReturnTypeID), p+".callable.return_type_id", false)
		v.ref("expression", string(c.DefaultValueID), p+".callable.default_value_id", false)
		v.ref("expression", string(c.BodyExpressionID), p+".callable.body_expression_id", false)
		refs(v, "expression", p+".callable.return_expression_ids", c.ReturnExpressionIDs)
		if d.Kind == DeclarationConstructor {
			if c.ReturnTypeID != "" {
				v.problem(p+".callable.return_type_id", "constructors have no return type")
			}
			v.choice(p+".callable.constructor_form", string(c.ConstructorForm), "normal", "compact")
		} else if c.ConstructorForm != "" {
			v.problem(p+".callable.constructor_form", "only constructors have a constructor form")
		}
	}
	if x := d.Variable; x != nil {
		if x.ParameterKind != "" {
			v.choice(p+".variable.parameter_kind", x.ParameterKind, "positional_only", "positional_or_keyword", "keyword_only", "var_positional", "var_keyword")
			if d.Kind != DeclarationParameter {
				v.problem(p+".variable.parameter_kind", "only parameters have a parameter kind")
			}
		}
		v.ref("type", string(x.DeclaredTypeID), p+".variable.declared_type_id", false)
		v.ref("expression", string(x.InitializerID), p+".variable.initializer_id", false)
		if x.Variadic && d.Kind != DeclarationParameter && d.Kind != DeclarationRecordComponent {
			v.problem(p+".variable.variadic", "only parameters and record components may be variadic")
		}
	}
	if d.TypeParameter != nil {
		refs(v, "type", p+".type_parameter.bound_type_ids", d.TypeParameter.BoundTypeIDs)
	}
	if d.Initializer != nil {
		v.choice(p+".initializer.kind", string(d.Initializer.Kind), "static", "instance")
		v.ref("statement", string(d.Initializer.BodyStatementID), p+".initializer.body_statement_id", false)
	}
}

func (v *validator) typeRef(p string, t TypeRef) {
	if t.Kind == TypeUnknown && v.file.Coverage.Status == ExtractionComplete {
		v.problem("coverage.status", "unknown types require incomplete extraction coverage")
	}
	v.span(p+".span", t.Span)
	v.ref("scope", string(t.ScopeID), p+".scope_id", true)
	v.required(p+".spelling", t.Spelling)
	v.choice(p+".kind", string(t.Kind), "named", "primitive", "void", "array", "wildcard", "intersection", "union", "inferred", "function", "structural", "literal", "operator", "unknown")
	if (t.Named != nil) != (t.Kind == TypeNamed) {
		v.problem(p+".named", "named payload is required only for named types")
	}
	if (t.Primitive != "") != (t.Kind == TypePrimitive) {
		v.problem(p+".primitive", "primitive name is required only for primitive types")
	}
	if t.Kind == TypeArray {
		v.ref("type", string(t.ElementTypeID), p+".element_type_id", true)
		if len(t.Dimensions) == 0 {
			v.problem(p+".dimensions", "array dimensions required")
		}
	} else if t.ElementTypeID != "" || len(t.Dimensions) > 0 {
		v.problem(p, "array payload on a non-array type")
	}
	if t.Kind != TypeWildcard && len(t.Bounds) > 0 {
		v.problem(p+".bounds", "only wildcard types carry bounds here")
	}
	if t.Kind == TypeUnion || t.Kind == TypeIntersection {
		if len(t.MemberTypeIDs) < 2 {
			v.problem(p+".member_type_ids", "at least two members required")
		}
	} else if len(t.MemberTypeIDs) > 0 {
		v.problem(p+".member_type_ids", "members require a union or intersection")
	}
	if t.Named != nil {
		if len(t.Named.Segments) == 0 {
			v.problem(p+".named.segments", "segments required")
		}
		for i, s := range t.Named.Segments {
			sp := fmt.Sprintf("%s.named.segments[%d]", p, i)
			v.required(sp+".name", s.Name)
			v.optionalSpan(sp+".span", s.Span)
			refs(v, "type", sp+".type_arguments", s.TypeArguments)
			v.choice(sp+".argument_syntax", string(s.ArgumentSyntax), "", "explicit", "diamond")
			if s.ArgumentSyntax == TypeArgumentsDiamond && len(s.TypeArguments) != 0 {
				v.problem(sp, "diamond cannot have explicit arguments")
			}
			if s.ArgumentSyntax == TypeArgumentsExplicit && len(s.TypeArguments) == 0 {
				v.problem(sp, "explicit type arguments must not be empty")
			}
			if s.ArgumentSyntax == "" && len(s.TypeArguments) != 0 {
				v.problem(sp, "type arguments require explicit argument syntax")
			}
		}
	}
	for i, d := range t.Dimensions {
		dp := fmt.Sprintf("%s.dimensions[%d]", p, i)
		v.span(dp+".span", d.Span)
		refs(v, "annotation", dp+".annotation_ids", d.AnnotationIDs)
	}
	for i, b := range t.Bounds {
		bp := fmt.Sprintf("%s.bounds[%d]", p, i)
		v.choice(bp+".kind", string(b.Kind), "extends", "super")
		v.ref("type", string(b.TypeRefID), bp+".type_ref_id", true)
	}
	refs(v, "type", p+".member_type_ids", t.MemberTypeIDs)
	refs(v, "annotation", p+".annotation_ids", t.AnnotationIDs)
}

func (v *validator) expression(p string, x Expression) {
	if x.Kind == ExpressionUnknown && v.file.Coverage.Status == ExtractionComplete {
		v.problem("coverage.status", "unknown expressions require incomplete extraction coverage")
	}
	v.ref("pattern", string(x.PatternID), p+".pattern_id", false)
	if x.PatternID != "" && x.Kind != ExpressionInstanceOf {
		v.problem(p, "pattern requires instanceof expression")
	}
	if (x.Switch != nil) != (x.Kind == ExpressionSwitch) {
		v.problem(p+".switch", "switch payload required only on switch expression")
	}
	if x.Switch != nil {
		v.ref("statement", string(x.Switch.BodyStatementID), p+".switch.body_statement_id", true)
	}
	v.span(p+".span", x.Span)
	v.ref("scope", string(x.ScopeID), p+".scope_id", true)
	v.required(p+".spelling", x.Spelling)
	v.choice(p+".kind", string(x.Kind), "name", "literal", "this", "super", "member_access", "call", "callable_reference", "parenthesized", "cast", "array_access", "array_creation", "array_initializer", "assignment", "unary", "binary", "conditional", "lambda", "class_literal", "instanceof", "switch", "object_literal", "markup", "unknown", "python_expression")
	if x.Kind == ExpressionPython {
		v.required(p+".syntax_kind", x.SyntaxKind)
	}
	if x.Name != nil {
		v.name(p+".name", *x.Name)
	}
	if (x.Literal != nil) != (x.Kind == ExpressionLiteral) {
		v.problem(p+".literal", "literal payload is required only for literals")
	}
	if (x.Lambda != nil) != (x.Kind == ExpressionLambda) {
		v.problem(p+".lambda", "lambda payload is required only for lambdas")
	}
	refs(v, "expression", p+".operand_ids", x.OperandIDs)
	v.ref("type", string(x.TypeRefID), p+".type_ref_id", false)
	v.ref("occurrence", string(x.OccurrenceID), p+".occurrence_id", x.Kind == ExpressionCall || x.Kind == ExpressionCallableReference || x.Kind == ExpressionLambda)
	if x.Literal != nil {
		v.choice(p+".literal.kind", string(x.Literal.Kind), "string", "character", "integer", "floating", "boolean", "null")
		v.required(p+".literal.lexeme", x.Literal.Lexeme)
	}
	if l := x.Lambda; l != nil {
		v.ref("scope", string(l.ScopeID), p+".lambda.scope_id", true)
		refs(v, "declaration", p+".lambda.parameter_ids", l.ParameterIDs)
		v.ref("expression", string(l.BodyExpressionID), p+".lambda.body_expression_id", false)
		v.ref("scope", string(l.BodyScopeID), p+".lambda.body_scope_id", false)
		v.ref("statement", string(l.BodyStatementID), p+".lambda.body_statement_id", false)
		if l.BodyStatementID != "" && l.BodyScopeID == "" {
			v.problem(p+".lambda", "statement body needs block scope")
		}
		if (l.BodyExpressionID == "") == (l.BodyScopeID == "") {
			v.problem(p+".lambda", "exactly one expression or block body required")
		}
	}
}

func (v *validator) call(p string, c Call) {
	v.occurrence(p+".occurrence", c.Occurrence)
	v.choice(p+".kind", string(c.Kind), "method", "object_creation", "this_constructor", "super_constructor", "enum_constant", "invocation")
	v.ref("expression", string(c.CalleeID), p+".callee_id", c.Kind == CallInvocation)
	v.ref("expression", string(c.ExpressionID), p+".expression_id", true)
	v.ref("expression", string(c.ReceiverID), p+".receiver_id", false)
	v.ref("type", string(c.ConstructedTypeID), p+".constructed_type_id", c.Kind == CallObjectCreation)
	v.ref("declaration", string(c.AnonymousTypeDeclarationID), p+".anonymous_type_declaration_id", false)
	refs(v, "type", p+".type_argument_ids", c.TypeArgumentIDs)
	for i, a := range c.Arguments {
		v.choice(fmt.Sprintf("%s.arguments[%d].expansion", p, i), a.Expansion, "", "iterable", "mapping")
		v.ref("expression", string(a.ExpressionID), fmt.Sprintf("%s.arguments[%d].expression_id", p, i), true)
	}
	if c.Kind == CallMethod {
		v.required(p+".name", c.Name)
		if c.ConstructedTypeID != "" || c.AnonymousTypeDeclarationID != "" {
			v.problem(p, "method invocation cannot carry construction payload")
		}
	}
}

// lambdaSite checks one lambda binding site. Agreement with the lambda
// expression it names is checked in expressionLinks alongside calls.
func (v *validator) lambdaSite(p string, l LambdaSite, declarations map[DeclarationID]Declaration) {
	v.occurrence(p+".occurrence", l.Occurrence)
	v.ref("expression", string(l.ExpressionID), p+".expression_id", true)
	refs(v, "declaration", p+".parameter_ids", l.ParameterIDs)
	for i, id := range l.ParameterIDs {
		if d, ok := declarations[id]; ok && d.Kind != DeclarationParameter {
			v.problem(fmt.Sprintf("%s.parameter_ids[%d]", p, i), "lambda parameter must be a parameter declaration")
		}
	}
	v.ref("scope", string(l.BodyScopeID), p+".body_scope_id", false)
}

func (v *validator) annotationValue(p string, a AnnotationValue, depth int) {
	if depth > 256 {
		v.problem(p, "annotation nesting exceeds validation limit (256)")
		return
	}
	v.span(p+".span", a.Span)
	switch a.Kind {
	case AnnotationExpression:
		v.ref("expression", string(a.ExpressionID), p+".expression_id", true)
		if len(a.Elements) > 0 || a.AnnotationID != "" {
			v.problem(p, "expression value has competing payloads")
		}
	case AnnotationArray:
		if a.ExpressionID != "" || a.AnnotationID != "" {
			v.problem(p, "array value has competing payloads")
		}
		for i, e := range a.Elements {
			v.annotationValue(fmt.Sprintf("%s.elements[%d]", p, i), e, depth+1)
		}
	case AnnotationNested:
		v.ref("annotation", string(a.AnnotationID), p+".annotation_id", true)
		if len(a.Elements) > 0 || a.ExpressionID != "" {
			v.problem(p, "nested annotation has competing payloads")
		}
	default:
		v.problem(p+".kind", "unsupported annotation value kind")
	}
}

func (v *validator) expressionLinks() {
	expressions := map[ExpressionID]Expression{}
	adj, types := map[string][]string{}, map[string][]string{}
	calls := map[OccurrenceID]Call{}
	references := map[OccurrenceID]CallableReference{}
	lambdas := map[OccurrenceID]LambdaSite{}
	for _, x := range v.file.Expressions {
		expressions[x.ID] = x
	}
	link := func(p string, id ExpressionID, o Occurrence, kind ExpressionKind) {
		x, ok := expressions[id]
		if ok && (x.Kind != kind || x.OccurrenceID != o.ID || x.ScopeID != o.ScopeID || x.Span != o.Span) {
			v.problem(p, "expression and occurrence must agree on kind, ID, scope, and span")
		}
	}
	for i, c := range v.file.Calls {
		calls[c.Occurrence.ID] = c
		link(fmt.Sprintf("calls[%d].expression_id", i), c.ExpressionID, c.Occurrence, ExpressionCall)
		if c.CalleeID != "" {
			adj[string(c.ExpressionID)] = append(adj[string(c.ExpressionID)], string(c.CalleeID))
		}
		if c.ReceiverID != "" {
			adj[string(c.ExpressionID)] = append(adj[string(c.ExpressionID)], string(c.ReceiverID))
		}
		for _, a := range c.Arguments {
			adj[string(c.ExpressionID)] = append(adj[string(c.ExpressionID)], string(a.ExpressionID))
		}
	}
	for i, r := range v.file.CallableReferences {
		references[r.Occurrence.ID] = r
		link(fmt.Sprintf("callable_references[%d].expression_id", i), r.ExpressionID, r.Occurrence, ExpressionCallableReference)
		adj[string(r.ExpressionID)] = append(adj[string(r.ExpressionID)], string(r.QualifierID))
	}
	for i, l := range v.file.Lambdas {
		lambdas[l.Occurrence.ID] = l
		p := fmt.Sprintf("lambdas[%d].expression_id", i)
		link(p, l.ExpressionID, l.Occurrence, ExpressionLambda)
		if x, ok := expressions[l.ExpressionID]; ok && x.Lambda != nil && (!slices.Equal(x.Lambda.ParameterIDs, l.ParameterIDs) || x.Lambda.BodyScopeID != l.BodyScopeID) {
			v.problem(p, "lambda site and expression payload must agree on parameters and body scope")
		}
	}
	for i, x := range v.file.Expressions {
		if x.Kind == ExpressionCall {
			if c, ok := calls[x.OccurrenceID]; !ok || c.ExpressionID != x.ID {
				v.problem(fmt.Sprintf("expressions[%d].occurrence_id", i), "matching call record required")
			}
		}
		if x.Kind == ExpressionCallableReference {
			if r, ok := references[x.OccurrenceID]; !ok || r.ExpressionID != x.ID {
				v.problem(fmt.Sprintf("expressions[%d].occurrence_id", i), "matching callable-reference record required")
			}
		}
		if x.Kind == ExpressionLambda {
			if l, ok := lambdas[x.OccurrenceID]; !ok || l.ExpressionID != x.ID {
				v.problem(fmt.Sprintf("expressions[%d].occurrence_id", i), "matching lambda record required")
			}
		}
		for _, id := range x.OperandIDs {
			adj[string(x.ID)] = append(adj[string(x.ID)], string(id))
		}
		if x.Lambda != nil && x.Lambda.BodyExpressionID != "" {
			adj[string(x.ID)] = append(adj[string(x.ID)], string(x.Lambda.BodyExpressionID))
		}
	}
	for _, t := range v.file.Types {
		add := func(id TypeRefID) {
			if id != "" {
				types[string(t.ID)] = append(types[string(t.ID)], string(id))
			}
		}
		add(t.ElementTypeID)
		for _, id := range t.MemberTypeIDs {
			add(id)
		}
		for _, b := range t.Bounds {
			add(b.TypeRefID)
		}
		if t.Named != nil {
			for _, s := range t.Named.Segments {
				for _, id := range s.TypeArguments {
					add(id)
				}
			}
		}
	}
	v.cycles("expressions", adj)
	v.cycles("types", types)
}
