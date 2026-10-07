package ir

type ScopeKind string

const (
	ScopeComprehension ScopeKind = "comprehension"
	ScopeAnnotation    ScopeKind = "annotation"
	ScopeFile          ScopeKind = "file"
	ScopeType          ScopeKind = "type"
	ScopeCallable      ScopeKind = "callable"
	ScopeBlock         ScopeKind = "block"
	ScopeLambda        ScopeKind = "lambda"
	ScopeInitializer   ScopeKind = "initializer"
	ScopeLoop          ScopeKind = "loop"
	ScopeCatch         ScopeKind = "catch"
	ScopeResource      ScopeKind = "resource"
	ScopePattern       ScopeKind = "pattern"
)

// Scope records syntactic containment, not a precomputed name-to-type map.
// Declarations name the scope where they are introduced; positions retain the
// information a later language binder needs for declaration order and shadowing.
// Regions may record disjoint syntax regions for a flow-sensitive construct;
// their existence does not certify that a variable is definitely in scope.
type Scope struct {
	ID                 ScopeID       `json:"id"`
	Kind               ScopeKind     `json:"kind"`
	ParentID           ScopeID       `json:"parent_id,omitempty"`
	OwnerDeclarationID DeclarationID `json:"owner_declaration_id,omitempty"`
	Span               Span          `json:"span"`
	Regions            []Span        `json:"regions,omitempty"`
}
