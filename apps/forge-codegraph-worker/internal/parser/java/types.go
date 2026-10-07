package java

import (
	"ei-aitiger-codegraph/pkg/ir"

	sitter "github.com/tree-sitter/go-tree-sitter"
)

func isType(kind string) bool {
	switch kind {
	case "type_identifier", "scoped_type_identifier", "generic_type", "array_type", "annotated_type", "wildcard", "integral_type", "floating_point_type", "boolean_type", "void_type", "catch_type":
		return true
	}
	return false
}

func (b *builder) typeRef(n *sitter.Node, env environment, role ir.TypeUseRole, parent ir.TypeRefID) ir.TypeRefID {
	if !valid(n) {
		return ""
	}
	if n.Kind() == "catch_type" && len(named(n)) == 1 {
		return b.typeRef(named(n)[0], env, role, parent)
	}
	t := ir.TypeRef{ID: ir.TypeRefID(b.id("type", n)), ScopeID: env.scope, Span: b.span(n), Spelling: b.spelling(n)}
	b.fillType(&t, n, env, role)
	b.record()
	b.account(string(t.ID), t)
	b.file.Types = append(b.file.Types, t)
	b.typeUse(t, env, role, parent)
	return t.ID
}

func (b *builder) fillType(t *ir.TypeRef, n *sitter.Node, env environment, role ir.TypeUseRole) {
	b.check()
	switch n.Kind() {
	case "annotated_type":
		for _, child := range named(n) {
			if isAnnotation(child) {
				t.AnnotationIDs = b.appendAnnotation(t.AnnotationIDs, child, env)
			} else {
				b.fillType(t, child, env, role)
			}
		}
		if t.Kind != "" {
			return
		}
	case "integral_type", "floating_point_type", "boolean_type":
		t.Kind, t.Primitive = ir.TypePrimitive, b.syntaxText(n)
		return
	case "void_type":
		t.Kind = ir.TypeVoid
		return
	case "identifier", "type_identifier", "scoped_identifier", "scoped_type_identifier", "generic_type":
		if b.syntaxText(n) == "var" && b.release >= 10 && (role == ir.TypeUseLocal || role == ir.TypeUseParameter || role == ir.TypeUsePattern) {
			t.Kind = ir.TypeInferred
			return
		}
		t.Kind, t.Named = ir.TypeNamed, &ir.NamedType{}
		b.typeSegments(t, n, env)
		if len(t.Named.Segments) > 0 {
			return
		}
		t.Named = nil
	case "array_type":
		t.Kind = ir.TypeArray
		t.ElementTypeID = b.typeRef(field(n, "element"), env, role, t.ID)
		t.Dimensions = b.dimensions(field(n, "dimensions"), env)
		if t.ElementTypeID != "" && len(t.Dimensions) > 0 {
			return
		}
		t.ElementTypeID, t.Dimensions = "", nil
	case "wildcard":
		t.Kind = ir.TypeWildcard
		bound := ir.BoundExtends
		for i := uint(0); i < n.ChildCount(); i++ {
			child := n.Child(i)
			if child.Kind() == "super" {
				bound = ir.BoundSuper
			}
			if isAnnotation(child) {
				t.AnnotationIDs = b.appendAnnotation(t.AnnotationIDs, child, env)
			} else if isType(child.Kind()) {
				if id := b.typeRef(child, env, ir.TypeUseBound, t.ID); id != "" {
					t.Bounds = append(t.Bounds, ir.TypeBound{Kind: bound, TypeRefID: id})
				}
			}
		}
		return
	case "catch_type":
		t.Kind = ir.TypeUnion
		for _, child := range named(n) {
			if id := b.typeRef(child, env, role, t.ID); id != "" {
				t.MemberTypeIDs = append(t.MemberTypeIDs, id)
			}
		}
		if len(t.MemberTypeIDs) >= 2 {
			return
		}
		t.MemberTypeIDs = nil
	}
	t.Kind = ir.TypeUnknown
	b.issue(n, "type_syntax", "Incomplete or unsupported written type: "+n.Kind(), ir.CoverageUnsupported)
}

// Traverse the type AST, attaching arguments to the segment they follow.
func (b *builder) typeSegments(t *ir.TypeRef, n *sitter.Node, env environment) {
	if !valid(n) {
		return
	}
	if n.Kind() == "identifier" || n.Kind() == "type_identifier" {
		span := b.span(n)
		t.Named.Segments = append(t.Named.Segments, ir.TypeSegment{Name: b.syntaxText(n), Span: &span})
		return
	}
	for _, child := range named(n) {
		switch {
		case isAnnotation(child):
			t.AnnotationIDs = b.appendAnnotation(t.AnnotationIDs, child, env)
		case child.Kind() == "type_arguments":
			if len(t.Named.Segments) > 0 {
				i := len(t.Named.Segments) - 1
				t.Named.Segments[i].ArgumentSyntax = ir.TypeArgumentsDiamond
				if len(named(child)) > 0 {
					t.Named.Segments[i].ArgumentSyntax = ir.TypeArgumentsExplicit
				}
			}
			for _, argument := range named(child) {
				if id := b.typeRef(argument, env, ir.TypeUseGenericArgument, t.ID); id != "" && len(t.Named.Segments) > 0 {
					i := len(t.Named.Segments) - 1
					t.Named.Segments[i].TypeArguments = append(t.Named.Segments[i].TypeArguments, id)
				}
			}
		default:
			b.typeSegments(t, child, env)
		}
	}
}

func (b *builder) typeUse(t ir.TypeRef, env environment, role ir.TypeUseRole, parent ir.TypeRefID) {
	use := ir.TypeUse{Occurrence: ir.Occurrence{ID: ir.OccurrenceID(b.id("occ", nil)), Span: t.Span, ScopeID: env.scope, EnclosingDeclarationID: env.owner}, Role: role, TypeRefID: t.ID, ParentTypeID: parent}
	b.record()
	b.account(string(use.Occurrence.ID), use)
	b.file.TypeUses = append(b.file.TypeUses, use)
}

func (b *builder) dimensions(n *sitter.Node, env environment) []ir.ArrayDimension {
	if n == nil {
		return nil
	}
	var result []ir.ArrayDimension
	var annotations []ir.AnnotationID
	var start *sitter.Node
	for i := uint(0); i < n.ChildCount(); i++ {
		child := n.Child(i)
		if isAnnotation(child) {
			annotations = b.appendAnnotation(annotations, child, env)
		}
		if child.Kind() == "[" {
			start = child
		}
		if child.Kind() == "]" && valid(child) && start != nil {
			result = append(result, ir.ArrayDimension{Span: b.spanBytes(start.StartByte(), child.EndByte()), AnnotationIDs: annotations})
			annotations, start = nil, nil
		}
	}
	return result
}

// Post-name dimensions are a discontiguous type fragment (int value[]).
// Keep their own exact span/spelling and link the leading element type.
func (b *builder) arraySuffix(base ir.TypeRefID, n *sitter.Node, env environment, role ir.TypeUseRole) ir.TypeRefID {
	if base == "" || !valid(n) {
		return base
	}
	dimensions := b.dimensions(n, env)
	if len(dimensions) == 0 {
		return base
	}
	t := ir.TypeRef{ID: ir.TypeRefID(b.id("type", n)), Kind: ir.TypeArray, ScopeID: env.scope, Span: b.span(n), Spelling: b.spelling(n), ElementTypeID: base, Dimensions: dimensions}
	b.record()
	b.account(string(t.ID), t)
	b.file.Types = append(b.file.Types, t)
	b.typeUse(t, env, role, "")
	return t.ID
}

func (b *builder) intersection(nodes []*sitter.Node, env environment) ir.TypeRefID {
	var present []*sitter.Node
	for _, n := range nodes {
		if valid(n) {
			present = append(present, n)
		}
	}
	if len(present) == 0 {
		return ""
	}
	if len(present) == 1 {
		return b.typeRef(present[0], env, ir.TypeUseCast, "")
	}
	start, end := present[0].StartByte(), present[len(present)-1].EndByte()
	t := ir.TypeRef{ID: ir.TypeRefID(b.id("type", present[0])), Kind: ir.TypeIntersection, ScopeID: env.scope, Span: b.spanBytes(start, end), Spelling: b.rawSlice(start, end)}
	for _, n := range present {
		t.MemberTypeIDs = append(t.MemberTypeIDs, b.typeRef(n, env, ir.TypeUseCast, t.ID))
	}
	b.record()
	b.account(string(t.ID), t)
	b.file.Types = append(b.file.Types, t)
	b.typeUse(t, env, ir.TypeUseCast, "")
	return t.ID
}
