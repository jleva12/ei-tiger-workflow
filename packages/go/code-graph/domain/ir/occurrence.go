package ir

type CallKind string

const (
	// CallInvocation preserves languages where syntax cannot distinguish a
	// function, class or callable value. CalleeID retains the full expression.
	CallInvocation       CallKind = "invocation"
	CallMethod           CallKind = "method"
	CallObjectCreation   CallKind = "object_creation"
	CallThisConstructor  CallKind = "this_constructor"
	CallSuperConstructor CallKind = "super_constructor"
	CallEnumConstant     CallKind = "enum_constant"
)

// Call records invocation syntax. Constructor calls retain the constructed
// type; the selected declaration is a future binding result. ReceiverID points
// to the complete receiver expression, including earlier calls in a chain.
type Call struct {
	CalleeID                   ExpressionID  `json:"callee_id,omitempty"`
	Occurrence                 Occurrence    `json:"occurrence"`
	Kind                       CallKind      `json:"kind"`
	ExpressionID               ExpressionID  `json:"expression_id"`
	Name                       string        `json:"name,omitempty"`
	ReceiverID                 ExpressionID  `json:"receiver_id,omitempty"`
	ConstructedTypeID          TypeRefID     `json:"constructed_type_id,omitempty"`
	TypeArgumentIDs            []TypeRefID   `json:"type_argument_ids,omitempty"`
	Arguments                  []Argument    `json:"arguments"`
	AnonymousTypeDeclarationID DeclarationID `json:"anonymous_type_declaration_id,omitempty"`
}

type Argument struct {
	// Expansion is "iterable" for *args or "mapping" for **kwargs.
	Expansion    string       `json:"expansion,omitempty"`
	ExpressionID ExpressionID `json:"expression_id"`
	// Keyword is available to language adapters with named arguments. Java leaves
	// it empty. Slice order is the argument order; no competing Index is stored.
	Keyword string `json:"keyword,omitempty"`
}

type CallableReferenceKind string

const (
	CallableMethodReference      CallableReferenceKind = "method"
	CallableConstructorReference CallableReferenceKind = "constructor"
)

// CallableReference is deferred callable syntax, never a zero-argument Call.
// QualifierID preserves the written expression; deciding whether a qualifier
// names a type or a value is binding work. Target typing comes from its context.
type CallableReference struct {
	Occurrence      Occurrence            `json:"occurrence"`
	Kind            CallableReferenceKind `json:"kind"`
	ExpressionID    ExpressionID          `json:"expression_id"`
	QualifierID     ExpressionID          `json:"qualifier_id"`
	Name            string                `json:"name"` // "new" for constructor references.
	TypeArgumentIDs []TypeRefID           `json:"type_argument_ids,omitempty"`
}

// LambdaSite is one lambda expression as a binding site. Its occurrence gets a
// persistent identity so the implements edge to the functional interface method
// the lambda implements can be anchored and continued across commits.
type LambdaSite struct {
	Occurrence   Occurrence      `json:"occurrence"`
	ExpressionID ExpressionID    `json:"expression_id"`
	ParameterIDs []DeclarationID `json:"parameter_ids,omitempty"`
	BodyScopeID  ScopeID         `json:"body_scope_id,omitempty"`
}

type ReferenceKind string

const (
	ReferenceName   ReferenceKind = "name"
	ReferenceMember ReferenceKind = "member"
)

type AccessKind string

const (
	AccessRead      AccessKind = "read"
	AccessWrite     AccessKind = "write"
	AccessReadWrite AccessKind = "read_write"
	AccessUnknown   AccessKind = "unknown"
)

// Reference describes a value/name use without claiming it is a field,
// parameter, package, or external symbol. Type occurrences use TypeUse.
type Reference struct {
	Occurrence   Occurrence    `json:"occurrence"`
	Kind         ReferenceKind `json:"kind"`
	ExpressionID ExpressionID  `json:"expression_id"`
	Name         Name          `json:"name"`
	ReceiverID   ExpressionID  `json:"receiver_id,omitempty"`
	Access       AccessKind    `json:"access"`
}

type TypeUseRole string

const (
	TypeUseField             TypeUseRole = "field"
	TypeUseParameter         TypeUseRole = "parameter"
	TypeUseReturn            TypeUseRole = "return"
	TypeUseLocal             TypeUseRole = "local"
	TypeUseGenericArgument   TypeUseRole = "generic_argument"
	TypeUseBound             TypeUseRole = "bound"
	TypeUseHeritage          TypeUseRole = "heritage"
	TypeUsePermittedType     TypeUseRole = "permitted_type"
	TypeUseThrows            TypeUseRole = "throws"
	TypeUseCatch             TypeUseRole = "catch"
	TypeUseCast              TypeUseRole = "cast"
	TypeUseConstruction      TypeUseRole = "construction"
	TypeUseCallableReference TypeUseRole = "callable_reference"
	TypeUseClassLiteral      TypeUseRole = "class_literal"
	TypeUseAnnotation        TypeUseRole = "annotation"
	TypeUseRecordComponent   TypeUseRole = "record_component"
	TypeUsePattern           TypeUseRole = "pattern"
	TypeUseModuleService     TypeUseRole = "module_service"
	TypeUseModuleProvider    TypeUseRole = "module_provider"
	// TypeUseAlias is the written type a type alias declaration names.
	TypeUseAlias TypeUseRole = "alias"
	// TypeUseComponent is a written type nested in a function, structural,
	// operator or literal type; ParentTypeID is the containing type.
	TypeUseComponent TypeUseRole = "component"
)

// TypeUse attributes each type occurrence, including nested generic arguments,
// to its enclosing declaration. ParentTypeID links an argument/bound to the
// written type that contains it; it is not an inheritance or binding target.
type TypeUse struct {
	Occurrence   Occurrence  `json:"occurrence"`
	Role         TypeUseRole `json:"role"`
	TypeRefID    TypeRefID   `json:"type_ref_id"`
	ParentTypeID TypeRefID   `json:"parent_type_id,omitempty"`
}
