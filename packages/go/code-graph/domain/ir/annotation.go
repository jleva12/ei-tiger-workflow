package ir

// Annotation preserves annotation identity syntax and named arguments. These
// facts support later Spring/Lombok enrichment without guessing from strings.
type Annotation struct {
	ID         AnnotationID         `json:"id"`
	Occurrence Occurrence           `json:"occurrence"`
	TypeRefID  TypeRefID            `json:"type_ref_id"`
	Arguments  []AnnotationArgument `json:"arguments,omitempty"`
}

type AnnotationArgument struct {
	// Empty Name means the shorthand unnamed value. It is not silently rewritten
	// to a language-specific default element name by this shared contract.
	Name     string          `json:"name,omitempty"`
	NameSpan *Span           `json:"name_span,omitempty"`
	Value    AnnotationValue `json:"value"`
}

type AnnotationValueKind string

const (
	AnnotationExpression AnnotationValueKind = "expression"
	AnnotationArray      AnnotationValueKind = "array"
	AnnotationNested     AnnotationValueKind = "annotation"
)

// AnnotationValue is a tagged union. Exactly one payload is used: ExpressionID,
// Elements (including an empty array), or AnnotationID. Expressions preserve
// constants, enum member accesses, class literals, and unevaluated expressions.
type AnnotationValue struct {
	Kind         AnnotationValueKind `json:"kind"`
	Span         Span                `json:"span"`
	ExpressionID ExpressionID        `json:"expression_id,omitempty"`
	Elements     []AnnotationValue   `json:"elements,omitempty"`
	AnnotationID AnnotationID        `json:"annotation_id,omitempty"`
}
