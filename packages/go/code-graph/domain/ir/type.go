package ir

type TypeKind string

const (
	TypeNamed        TypeKind = "named"
	TypePrimitive    TypeKind = "primitive"
	TypeVoid         TypeKind = "void"
	TypeArray        TypeKind = "array"
	TypeWildcard     TypeKind = "wildcard"
	TypeIntersection TypeKind = "intersection"
	TypeUnion        TypeKind = "union"
	TypeInferred     TypeKind = "inferred"
	// TypeFunction is a written function or constructor type; TypeStructural
	// an object type literal, tuple or mapped type; TypeLiteral a literal or
	// template literal type; TypeOperator a type computed from other types or
	// values (keyof, typeof, indexed access, conditional, infer, this, or a
	// heritage expression that is not a name). None names a declaration
	// itself: the written types they contain are separate TypeUse rows with
	// the component role and this type as their parent.
	TypeFunction   TypeKind = "function"
	TypeStructural TypeKind = "structural"
	TypeLiteral    TypeKind = "literal"
	TypeOperator   TypeKind = "operator"
	TypeUnknown    TypeKind = "unknown"
)

// TypeRef is a written type occurrence, not an interned semantic type.
// A type variable T is initially TypeNamed; a later binder decides whether its
// declaration is a type parameter, a class, or unavailable.
//
// Named is used for TypeNamed; Primitive for TypePrimitive; ElementTypeID and
// Dimensions for TypeArray; Bounds for TypeWildcard; MemberTypeIDs for unions
// and intersections. Inferred/unknown types retain their original Spelling.
type TypeRef struct {
	ID            TypeRefID        `json:"id"`
	Kind          TypeKind         `json:"kind"`
	Span          Span             `json:"span"`
	ScopeID       ScopeID          `json:"scope_id"`
	Spelling      string           `json:"spelling"`
	Named         *NamedType       `json:"named,omitempty"`
	Primitive     string           `json:"primitive,omitempty"`
	ElementTypeID TypeRefID        `json:"element_type_id,omitempty"`
	Dimensions    []ArrayDimension `json:"dimensions,omitempty"`
	Bounds        []TypeBound      `json:"bounds,omitempty"`
	MemberTypeIDs []TypeRefID      `json:"member_type_ids,omitempty"`
	AnnotationIDs []AnnotationID   `json:"annotation_ids,omitempty"`
}

// NamedType preserves a.Outer<T>.Inner<U> without losing either qualifier or
// associating the inner type's generic arguments with the outer type.
type NamedType struct {
	Segments []TypeSegment `json:"segments"`
}

type TypeSegment struct {
	Name          string      `json:"name"`
	Span          *Span       `json:"span,omitempty"`
	TypeArguments []TypeRefID `json:"type_arguments,omitempty"`
	// Absent, explicit arguments, and diamond are different syntax states.
	ArgumentSyntax TypeArgumentSyntax `json:"argument_syntax,omitempty"`
}

type TypeArgumentSyntax string

const (
	TypeArgumentsExplicit TypeArgumentSyntax = "explicit"
	TypeArgumentsDiamond  TypeArgumentSyntax = "diamond"
)

type ArrayDimension struct {
	Span          Span           `json:"span"`
	AnnotationIDs []AnnotationID `json:"annotation_ids,omitempty"`
}

type BoundKind string

const (
	BoundExtends BoundKind = "extends"
	BoundSuper   BoundKind = "super"
)

type TypeBound struct {
	Kind      BoundKind `json:"kind"`
	TypeRefID TypeRefID `json:"type_ref_id"`
}
