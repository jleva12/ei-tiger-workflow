package ir

type DeclarationKind string

const (
	DeclarationClass           DeclarationKind = "class"
	DeclarationInterface       DeclarationKind = "interface"
	DeclarationEnum            DeclarationKind = "enum"
	DeclarationRecord          DeclarationKind = "record"
	DeclarationAnnotationType  DeclarationKind = "annotation_type"
	DeclarationEnumConstant    DeclarationKind = "enum_constant"
	DeclarationMethod          DeclarationKind = "method"
	DeclarationConstructor     DeclarationKind = "constructor"
	DeclarationFunction        DeclarationKind = "function"
	DeclarationField           DeclarationKind = "field"
	DeclarationParameter       DeclarationKind = "parameter"
	DeclarationReceiver        DeclarationKind = "receiver_parameter"
	DeclarationLocal           DeclarationKind = "local_variable"
	DeclarationRecordComponent DeclarationKind = "record_component"
	DeclarationTypeParameter   DeclarationKind = "type_parameter"
	DeclarationInitializer     DeclarationKind = "initializer"
	DeclarationPatternVariable DeclarationKind = "pattern_variable"
	// Kinds introduced for languages whose modules declare more than Java
	// does: a type alias and a namespace carry the type payload, a
	// module-level binding (const, let, var) the variable payload.
	DeclarationTypeAlias DeclarationKind = "type_alias"
	DeclarationNamespace DeclarationKind = "namespace"
	DeclarationVariable  DeclarationKind = "variable"
)

type TypeForm string

const (
	TypeTopLevel  TypeForm = "top_level"
	TypeMember    TypeForm = "member"
	TypeLocal     TypeForm = "local"
	TypeAnonymous TypeForm = "anonymous"
)

// Declaration carries syntax-backed properties only. OwnerID records the
// actual containing declaration, including anonymous/local types and fields.
// SignatureText is source text, never a canonical or erased signature.
//
// Exactly one detail payload is used according to Kind: Type, Callable,
// Variable, TypeParameter, or Initializer. Enum constants use Variable.
type Declaration struct {
	// DecoratorExpressionIDs preserve Python decorators in written order.
	DecoratorExpressionIDs []ExpressionID            `json:"decorator_expression_ids,omitempty"`
	ID                     DeclarationID             `json:"id"`
	Kind                   DeclarationKind           `json:"kind"`
	Name                   string                    `json:"name"` // Empty for anonymous types/initializers.
	NameSpan               *Span                     `json:"name_span,omitempty"`
	Span                   Span                      `json:"span"`
	DeclaringScopeID       ScopeID                   `json:"declaring_scope_id"`
	BodyScopeID            ScopeID                   `json:"body_scope_id,omitempty"`
	OwnerID                DeclarationID             `json:"owner_id,omitempty"`
	Modifiers              []Modifier                `json:"modifiers,omitempty"`
	AnnotationIDs          []AnnotationID            `json:"annotation_ids,omitempty"`
	SignatureText          string                    `json:"signature_text,omitempty"`
	DocComment             string                    `json:"doc_comment,omitempty"`
	Type                   *TypeDeclaration          `json:"type,omitempty"`
	Callable               *CallableDeclaration      `json:"callable,omitempty"`
	Variable               *VariableDeclaration      `json:"variable,omitempty"`
	TypeParameter          *TypeParameterDeclaration `json:"type_parameter,omitempty"`
	Initializer            *InitializerDeclaration   `json:"initializer,omitempty"`
}

// Modifier retains explicit syntax (e.g. public, static, final, non-sealed).
// Implicit Java modifiers are derived by binding, not synthesized by parsing.
type Modifier struct {
	Keyword string `json:"keyword"`
	Span    Span   `json:"span"`
}

type TypeDeclaration struct {
	Form               TypeForm        `json:"form"`
	TypeParameterIDs   []DeclarationID `json:"type_parameter_ids,omitempty"`
	RecordComponentIDs []DeclarationID `json:"record_component_ids,omitempty"`
	Heritage           []Heritage      `json:"heritage,omitempty"`
}

type HeritageKind string

const (
	HeritageExtends    HeritageKind = "extends"
	HeritageImplements HeritageKind = "implements"
	HeritagePermits    HeritageKind = "permits"
)

// Heritage retains the whole written type, including generic arguments.
type Heritage struct {
	Kind      HeritageKind `json:"kind"`
	TypeRefID TypeRefID    `json:"type_ref_id"`
	Span      Span         `json:"span"`
}

type ConstructorForm string

const (
	ConstructorNormal  ConstructorForm = "normal"
	ConstructorCompact ConstructorForm = "compact"
)

type CallableDeclaration struct {
	ParameterIDs     []DeclarationID `json:"parameter_ids,omitempty"`
	TypeParameterIDs []DeclarationID `json:"type_parameter_ids,omitempty"`
	ReturnTypeID     TypeRefID       `json:"return_type_id,omitempty"`
	ThrowsTypeIDs    []TypeRefID     `json:"throws_type_ids,omitempty"`
	ConstructorForm  ConstructorForm `json:"constructor_form,omitempty"`
	// Annotation element defaults and expression-bodied callables.
	DefaultValueID ExpressionID `json:"default_value_id,omitempty"`
	// Annotation defaults use the same value union as annotation arguments.
	AnnotationDefault *AnnotationValue `json:"annotation_default,omitempty"`
	BodyStatementID   StatementID      `json:"body_statement_id,omitempty"`
	BodyExpressionID  ExpressionID     `json:"body_expression_id,omitempty"`
	// ReturnExpressionIDs are the values the callable's own return
	// statements return (not those of functions nested in it), for a
	// language whose IR keeps no statements: its result type can then be
	// inferred when none is declared.
	ReturnExpressionIDs []ExpressionID `json:"return_expression_ids,omitempty"`
}

type VariableDeclaration struct {
	ParameterKind  string       `json:"parameter_kind,omitempty"`
	DeclaredTypeID TypeRefID    `json:"declared_type_id,omitempty"`
	InitializerID  ExpressionID `json:"initializer_id,omitempty"`
	Variadic       bool         `json:"variadic,omitempty"`
}

type TypeParameterDeclaration struct {
	BoundTypeIDs []TypeRefID `json:"bound_type_ids,omitempty"`
}

type InitializerKind string

const (
	InitializerStatic   InitializerKind = "static"
	InitializerInstance InitializerKind = "instance"
)

// Block initializers have their own Declaration/BodyScopeID. Field initializers
// use Variable.InitializerID and a field-owned initializer scope.
type InitializerDeclaration struct {
	Kind            InitializerKind `json:"kind"`
	BodyStatementID StatementID     `json:"body_statement_id,omitempty"`
}
