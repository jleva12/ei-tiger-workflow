package typescript

import (
	"strings"

	"ei-aitiger-codegraph/pkg/ir"

	sitter "github.com/tree-sitter/go-tree-sitter"
)

// typeRef records a written type and its use. Annotations and predicates
// unwrap to the type they carry.
func (b *builder) typeRef(n *sitter.Node, env environment, role ir.TypeUseRole, parent ir.TypeRefID) ir.TypeRefID {
	if !valid(n) {
		return ""
	}
	switch n.Kind() {
	case "type_annotation", "opting_type_annotation", "adding_type_annotation", "omitting_type_annotation", "parenthesized_type", "readonly_type", "optional_type", "rest_type", "constraint", "default_type":
		return b.typeRef(firstNamed(n), env, role, parent)
	case "type_predicate", "type_predicate_annotation":
		if t := field(n, "type"); t != nil {
			return b.typeRef(t, env, role, parent)
		}
		return b.typeRef(firstNamed(n), env, role, parent)
	case "asserts_annotation", "asserts":
		return ""
	case "union_type", "intersection_type":
		// A leading | or & (`type T = | A | B`) is a one-member union
		// around its first member.
		if members := named(n); len(members) == 1 {
			return b.typeRef(members[0], env, role, parent)
		}
	}
	t := ir.TypeRef{ID: ir.TypeRefID(b.id("type", n)), ScopeID: env.scope, Span: b.span(n), Spelling: b.spelling(n)}
	b.fillType(&t, n, env, role)
	return b.saveType(t, env, role, parent)
}

func (b *builder) saveType(t ir.TypeRef, env environment, role ir.TypeUseRole, parent ir.TypeRefID) ir.TypeRefID {
	b.record()
	b.account(string(t.ID), t)
	b.file.Types = append(b.file.Types, t)
	use := ir.TypeUse{Occurrence: ir.Occurrence{ID: ir.OccurrenceID(b.id("occ", nil)), Span: t.Span, ScopeID: env.scope, EnclosingDeclarationID: env.owner}, Role: role, TypeRefID: t.ID, ParentTypeID: parent}
	b.record()
	b.account(string(use.Occurrence.ID), use)
	b.file.TypeUses = append(b.file.TypeUses, use)
	return t.ID
}

// returnType unwraps a return annotation.
func (b *builder) returnType(n *sitter.Node, env environment) ir.TypeRefID {
	return b.typeRef(n, env, ir.TypeUseReturn, "")
}

func (b *builder) fillType(t *ir.TypeRef, n *sitter.Node, env environment, role ir.TypeUseRole) {
	b.check()
	switch n.Kind() {
	case "type_identifier", "identifier":
		span := b.span(n)
		t.Kind, t.Named = ir.TypeNamed, &ir.NamedType{Segments: []ir.TypeSegment{{Name: b.spelling(n), Span: &span}}}
		return
	case "nested_type_identifier", "nested_identifier":
		t.Kind, t.Named = ir.TypeNamed, &ir.NamedType{}
		b.nestedSegments(t, n)
		if len(t.Named.Segments) > 0 {
			return
		}
		t.Named = nil
	case "generic_type":
		t.Kind, t.Named = ir.TypeNamed, &ir.NamedType{}
		b.nestedSegments(t, field(n, "name"))
		if len(t.Named.Segments) > 0 {
			last := &t.Named.Segments[len(t.Named.Segments)-1]
			last.TypeArguments = b.typeArgumentsOf(field(n, "type_arguments"), env, t.ID)
			last.ArgumentSyntax = ir.TypeArgumentsExplicit
			return
		}
		t.Named = nil
	case "predefined_type":
		t.Kind, t.Primitive = ir.TypePrimitive, b.spelling(n)
		return
	case "array_type":
		t.Kind = ir.TypeArray
		t.ElementTypeID = b.typeRef(firstNamed(n), env, role, t.ID)
		if t.ElementTypeID != "" && n.EndByte() >= n.StartByte()+2 {
			t.Dimensions = []ir.ArrayDimension{{Span: b.rawSpan(n.EndByte()-2, n.EndByte())}}
			return
		}
		t.ElementTypeID, t.Dimensions = "", nil
	case "union_type", "intersection_type":
		t.Kind = ir.TypeUnion
		if n.Kind() == "intersection_type" {
			t.Kind = ir.TypeIntersection
		}
		for _, child := range named(n) {
			if id := b.typeRef(child, env, role, t.ID); id != "" {
				t.MemberTypeIDs = append(t.MemberTypeIDs, id)
			}
		}
		if len(t.MemberTypeIDs) >= 2 {
			return
		}
		t.MemberTypeIDs = nil
	case "function_type", "constructor_type":
		t.Kind = ir.TypeFunction
		b.parameterTypes(field(n, "parameters"), env, t.ID)
		b.typeRef(field(n, "return_type"), env, ir.TypeUseComponent, t.ID)
		b.typeRef(field(n, "type"), env, ir.TypeUseComponent, t.ID)
		return
	case "object_type":
		t.Kind = ir.TypeStructural
		b.memberTypes(n, env, t.ID)
		return
	case "tuple_type":
		t.Kind = ir.TypeStructural
		for _, child := range named(n) {
			switch child.Kind() {
			case "required_parameter", "optional_parameter":
				b.typeRef(field(child, "type"), env, ir.TypeUseComponent, t.ID)
			default:
				b.typeRef(child, env, ir.TypeUseComponent, t.ID)
			}
		}
		return
	case "literal_type", "template_literal_type":
		t.Kind = ir.TypeLiteral
		return
	case "type_query":
		// typeof value: the value is an expression reference.
		t.Kind = ir.TypeOperator
		b.expression(firstNamed(n), env, ir.AccessRead)
		return
	case "index_type_query", "lookup_type", "conditional_type":
		t.Kind = ir.TypeOperator
		for _, child := range named(n) {
			if isTypeSyntax(child.Kind()) {
				b.typeRef(child, env, ir.TypeUseComponent, t.ID)
			}
		}
		return
	case "infer_type", "this_type":
		t.Kind = ir.TypeOperator
		return
	}
	t.Kind = ir.TypeUnknown
	b.issue(n, "type_syntax", "Unsupported type syntax "+n.Kind(), ir.CoverageUnsupported)
}

// isTypeSyntax reports whether a node kind is a written type.
func isTypeSyntax(kind string) bool {
	switch kind {
	case "type_identifier", "nested_type_identifier", "generic_type", "predefined_type", "array_type", "union_type", "intersection_type",
		"function_type", "constructor_type", "object_type", "tuple_type", "literal_type", "template_literal_type", "type_query",
		"index_type_query", "lookup_type", "conditional_type", "infer_type", "this_type", "parenthesized_type", "readonly_type",
		"optional_type", "rest_type":
		return true
	}
	return false
}

// parameterTypes records the written parameter types of a function type or
// signature as components of the containing type.
func (b *builder) parameterTypes(parameters *sitter.Node, env environment, parent ir.TypeRefID) {
	for _, p := range named(parameters) {
		switch p.Kind() {
		case "required_parameter", "optional_parameter":
			b.typeRef(field(p, "type"), env, ir.TypeUseComponent, parent)
		}
	}
}

// memberTypes records the member, index, signature and mapped-type types of
// an object type literal as components.
func (b *builder) memberTypes(n *sitter.Node, env environment, parent ir.TypeRefID) {
	for _, member := range named(n) {
		switch member.Kind() {
		case "property_signature":
			b.typeRef(field(member, "type"), env, ir.TypeUseComponent, parent)
		case "index_signature":
			b.typeRef(field(member, "index_type"), env, ir.TypeUseComponent, parent)
			b.typeRef(field(member, "type"), env, ir.TypeUseComponent, parent)
			for _, child := range named(member) {
				if child.Kind() == "mapped_type_clause" {
					b.typeRef(field(child, "type"), env, ir.TypeUseComponent, parent)
					b.typeRef(field(child, "alias"), env, ir.TypeUseComponent, parent)
				}
			}
		case "method_signature", "call_signature", "construct_signature":
			b.parameterTypes(field(member, "parameters"), env, parent)
			b.typeRef(field(member, "return_type"), env, ir.TypeUseComponent, parent)
		}
	}
}

// nestedSegments splits a dotted type name into segments with spans.
func (b *builder) nestedSegments(t *ir.TypeRef, n *sitter.Node) {
	if !valid(n) {
		return
	}
	switch n.Kind() {
	case "type_identifier", "identifier", "property_identifier":
		span := b.span(n)
		t.Named.Segments = append(t.Named.Segments, ir.TypeSegment{Name: b.spelling(n), Span: &span})
	case "nested_type_identifier", "nested_identifier", "member_expression":
		for _, child := range named(n) {
			b.nestedSegments(t, child)
		}
	}
}

// typeArgumentsOf records explicit type arguments as generic-argument uses.
func (b *builder) typeArgumentsOf(n *sitter.Node, env environment, parent ir.TypeRefID) []ir.TypeRefID {
	var ids []ir.TypeRefID
	for _, argument := range named(n) {
		if id := b.typeRef(argument, env, ir.TypeUseGenericArgument, parent); id != "" {
			ids = append(ids, id)
		}
	}
	return ids
}

// typeFromExpression writes an expression that names a type (a class in a
// heritage clause, a constructor, a decorator) as a named type when it is
// an identifier or member chain. Other expressions return "" and remain
// expressions.
func (b *builder) typeFromExpression(n *sitter.Node, env environment, role ir.TypeUseRole) ir.TypeRefID {
	if !valid(n) {
		return ""
	}
	switch n.Kind() {
	case "identifier", "type_identifier", "member_expression", "nested_identifier", "nested_type_identifier":
	default:
		return ""
	}
	t := ir.TypeRef{ID: ir.TypeRefID(b.id("type", n)), Kind: ir.TypeNamed, ScopeID: env.scope, Span: b.span(n), Spelling: b.spelling(n), Named: &ir.NamedType{}}
	b.nestedSegments(&t, n)
	if len(t.Named.Segments) == 0 || strings.Contains(t.Spelling, "(") {
		return ""
	}
	return b.saveType(t, env, role, "")
}

// computedType retains an expression that stands for a type without being
// a name (a mixin call in a heritage clause, a computed decorator or
// constructor) as an operator type; the caller extracts the expression.
func (b *builder) computedType(n *sitter.Node, env environment, role ir.TypeUseRole) ir.TypeRefID {
	if !valid(n) {
		return ""
	}
	t := ir.TypeRef{ID: ir.TypeRefID(b.id("type", n)), Kind: ir.TypeOperator, ScopeID: env.scope, Span: b.span(n), Spelling: b.spelling(n)}
	return b.saveType(t, env, role, "")
}
