package ir

// ModuleDeclaration preserves module-info.java syntax. It does not assert
// resolved dependencies, readability, export visibility or provider validity.
type ModuleDeclaration struct {
	Name          Name              `json:"name"`
	Span          Span              `json:"span"`
	Open          bool              `json:"open"`
	AnnotationIDs []AnnotationID    `json:"annotation_ids,omitempty"`
	Directives    []ModuleDirective `json:"directives,omitempty"`
}

type ModuleDirectiveKind string

const (
	ModuleRequires ModuleDirectiveKind = "requires"
	ModuleExports  ModuleDirectiveKind = "exports"
	ModuleOpens    ModuleDirectiveKind = "opens"
	ModuleUses     ModuleDirectiveKind = "uses"
	ModuleProvides ModuleDirectiveKind = "provides"
)

type ModuleDirective struct {
	Occurrence      Occurrence          `json:"occurrence"`
	Kind            ModuleDirectiveKind `json:"kind"`
	Name            *Name               `json:"name,omitempty"`      // required module or exported/open package
	Modifiers       []Modifier          `json:"modifiers,omitempty"` // static/transitive on requires
	TargetModules   []Name              `json:"target_modules,omitempty"`
	ServiceTypeID   TypeRefID           `json:"service_type_id,omitempty"`
	ProviderTypeIDs []TypeRefID         `json:"provider_type_ids,omitempty"`
}

// LanguageValidation is separate from extraction coverage. A syntax adapter
// can identify release violations without certifying javac validity. NotChecked
// explicitly requires a later compiler/build validation step when validity is
// needed; even complete extraction may have this status.
type LanguageValidation struct {
	Status  LanguageValidationStatus `json:"status"`
	Method  string                   `json:"method"`
	Release string                   `json:"release"`
}

type LanguageValidationStatus string

const (
	LanguageNotChecked LanguageValidationStatus = "not_checked"
	LanguageInvalid    LanguageValidationStatus = "invalid"
)
