package ir

// SchemaVersion changes when the persisted IR contract changes.
// Consumers must reject unsupported versions.
const SchemaVersion = "1.3.0"

// SourceFile is the parser output for one immutable source file. There are
// deliberately no resolved target IDs, confidence scores, or storage edges.
type SourceFile struct {
	SchemaVersion      string              `json:"schema_version"`
	Source             Source              `json:"source"`
	Producer           Producer            `json:"producer"`
	RootScopeID        ScopeID             `json:"root_scope_id"`
	Package            *PackageClause      `json:"package,omitempty"`
	Scopes             []Scope             `json:"scopes"`
	Declarations       []Declaration       `json:"declarations,omitempty"`
	Imports            []Import            `json:"imports,omitempty"`
	Types              []TypeRef           `json:"types,omitempty"`
	Expressions        []Expression        `json:"expressions,omitempty"`
	Calls              []Call              `json:"calls,omitempty"`
	CallableReferences []CallableReference `json:"callable_references,omitempty"`
	Lambdas            []LambdaSite        `json:"lambdas,omitempty"`
	References         []Reference         `json:"references,omitempty"`
	TypeUses           []TypeUse           `json:"type_uses,omitempty"`
	Annotations        []Annotation        `json:"annotations,omitempty"`
	Statements         []Statement         `json:"statements,omitempty"`
	Patterns           []Pattern           `json:"patterns,omitempty"`
	Module             *ModuleDeclaration  `json:"module,omitempty"`
	LanguageValidation *LanguageValidation `json:"language_validation,omitempty"`
	Diagnostics        []Diagnostic        `json:"diagnostics,omitempty"`
	Coverage           ExtractionCoverage  `json:"coverage"`
}

type PackageClause struct {
	Name          Name           `json:"name"`
	Span          Span           `json:"span"`
	AnnotationIDs []AnnotationID `json:"annotation_ids,omitempty"`
}

type ImportKind string

const (
	ImportSingleType     ImportKind = "single_type"
	ImportTypeOnDemand   ImportKind = "type_on_demand"
	ImportSingleStatic   ImportKind = "single_static"
	ImportStaticOnDemand ImportKind = "static_on_demand"
	ImportModule         ImportKind = "module"
	// ImportReExport is an export that forwards another module's binding
	// without creating a local one: `export { X as Y } from "m"` (Name X,
	// Alias Y), `export * from "m"` (Name *, no alias) and
	// `export * as ns from "m"` (Name *, Alias ns). A module-level
	// `export { X }` or `export default X` of an imported X is recorded the
	// same way, with the import's module.
	ImportReExport ImportKind = "re_export"
)

// Import includes unresolved explicit imports. An on-demand import stores its
// written prefix; it never masquerades as a local binding to the last segment.
// Module is the written module specifier for languages whose imports name a
// module path rather than a package (TypeScript: "./api", "react"); Name then
// holds the imported binding ("default", "*" or a named export).
type Import struct {
	// Python import syntax distinguishes the module from the local binding.
	BindingName   string     `json:"binding_name,omitempty"`
	RelativeLevel uint32     `json:"relative_level,omitempty"`
	Occurrence    Occurrence `json:"occurrence"`
	Kind          ImportKind `json:"kind"`
	Name          Name       `json:"name"`
	Alias         string     `json:"alias,omitempty"`
	Spelling      string     `json:"spelling"`
	Module        string     `json:"module,omitempty"`
}
