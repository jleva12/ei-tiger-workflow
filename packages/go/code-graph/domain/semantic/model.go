// Package semantic defines the typed facts a language resolver proves about a
// commit (symbols and lookups), the persistent identities the matcher assigns
// (per-file identity maps), and the run-local workspace those stages share.
// It has no storage or paging concepts: the workspace is an in-process
// contract backed by a disposable local index.
package semantic

import (
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
)

// SourceInput is one file in one compilation context. Lineage is the
// persistent identity of that (path, module, source set) triple and doubles as
// the source_file node ID and the anchor lineage. Affected marks files whose
// syntax, bindings, identities and projection are recomputed in this run.
type SourceInput struct {
	Source   ir.Source `json:"source"`
	Lineage  string    `json:"lineage"`
	Affected bool      `json:"affected,omitempty"`
}

// DeclarationKey is available only after semantic resolution proves a
// canonical binding. SignatureText from the parser is never a CanonicalSignature.
type DeclarationKey struct {
	OwnerKey           string             `json:"owner_key"`
	Kind               ir.DeclarationKind `json:"kind"`
	Name               string             `json:"name"`
	CanonicalSignature string             `json:"canonical_signature"`
}

type SourceSymbol struct {
	FileID        ir.FileID          `json:"file_id"`
	DeclarationID ir.DeclarationID   `json:"declaration_id"`
	Evidence      graph.SourceAnchor `json:"evidence"`
}

// ModuleSymbol denotes a source file's module namespace, which has no written
// declaration. Its graph identity is the source file lineage.
type ModuleSymbol struct {
	FileID ir.FileID `json:"file_id"`
}

type ExternalSymbol struct {
	ArtifactID          string `json:"artifact_id"`
	ArtifactFingerprint string `json:"artifact_fingerprint"`
}

// IntrinsicSymbol identifies a language-defined entity, such as a primitive
// type, that has no source declaration or dependency artifact.
type IntrinsicSymbol struct {
	Language         string `json:"language"`
	Name             string `json:"name"`
	DefinitionDigest string `json:"definition_digest"`
}

// ConstructedSymbol records an attributed type expression (array, wildcard,
// intersection, parameterized type) rather than pretending it is its element.
type ConstructedSymbol struct {
	Language           string   `json:"language"`
	Kind               string   `json:"kind"`
	CanonicalSignature string   `json:"canonical_signature"`
	ComponentSymbolIDs []string `json:"component_symbol_ids,omitempty"`
	DefinitionDigest   string   `json:"definition_digest"`
}

// DerivedSymbol is a compiler-established member with no written declaration
// span (for example an implicit constructor).
type DerivedSymbol struct {
	Language         string   `json:"language"`
	Rule             string   `json:"rule"`
	SourceSymbolIDs  []string `json:"source_symbol_ids"`
	DefinitionDigest string   `json:"definition_digest"`
}

// Symbol IDs are scoped to one run and are not graph entity IDs. Exactly one
// of Source, Module, External, Intrinsic, Constructed or Derived is present.
type Symbol struct {
	ID            string             `json:"id"`
	Name          string             `json:"name,omitempty"`
	Key           *DeclarationKey    `json:"key,omitempty"`
	OwnerSymbolID string             `json:"owner_symbol_id,omitempty"`
	Source        *SourceSymbol      `json:"source,omitempty"`
	Module        *ModuleSymbol      `json:"module,omitempty"`
	External      *ExternalSymbol    `json:"external,omitempty"`
	Intrinsic     *IntrinsicSymbol   `json:"intrinsic,omitempty"`
	Constructed   *ConstructedSymbol `json:"constructed,omitempty"`
	Derived       *DerivedSymbol     `json:"derived,omitempty"`
}

type LookupStatus string

const (
	LookupResolved    LookupStatus = "resolved"
	LookupUnresolved  LookupStatus = "unresolved"
	LookupAmbiguous   LookupStatus = "ambiguous"
	LookupUnsupported LookupStatus = "unsupported"
)

// LookupCause is an analyzer-proven diagnostic category.
type LookupCause string

const (
	CauseExternalDependency LookupCause = "external_dependency"
	CauseSourceDiagnostic   LookupCause = "source_diagnostic"
	CauseAnalysisLimitation LookupCause = "analysis_limitation"
	CauseAmbiguousBinding   LookupCause = "ambiguous_binding"
	CauseUnknown            LookupCause = "unknown"
)

type LookupKind string

const (
	LookupCall        LookupKind = "call"
	LookupType        LookupKind = "type"
	LookupMember      LookupKind = "member"
	LookupInheritance LookupKind = "inheritance"
	LookupFramework   LookupKind = "framework"
	// LookupOverride binds a method declaration to a method it overrides.
	// Its site is the declaration (DeclarationID), not an occurrence.
	LookupOverride LookupKind = "override"
	// LookupImplements binds a lambda or method reference occurrence to the
	// single abstract method of its target functional interface.
	LookupImplements LookupKind = "implements"
)

// EdgeKind maps a resolved lookup kind to the graph edge it projects to.
func (k LookupKind) EdgeKind() string {
	switch k {
	case LookupCall:
		return graph.EdgeCalls
	case LookupType:
		return graph.EdgeUsesType
	case LookupMember:
		return graph.EdgeReferences
	case LookupInheritance:
		return graph.EdgeInherits
	case LookupFramework:
		return graph.EdgeFrameworkBinding
	case LookupOverride:
		return graph.EdgeOverrides
	case LookupImplements:
		return graph.EdgeImplements
	}
	return ""
}

// Lookup is the binding result for one site in one file. Sites are occurrences
// (calls, references, type uses, lambdas, method references, imports, ...) or,
// for override lookups, a method declaration.
type Lookup struct {
	ID               string           `json:"id"`
	FileID           ir.FileID        `json:"file_id"`
	OccurrenceID     ir.OccurrenceID  `json:"occurrence_id,omitempty"`
	DeclarationID    ir.DeclarationID `json:"declaration_id,omitempty"`
	Kind             LookupKind       `json:"kind"`
	Status           LookupStatus     `json:"status"`
	SelectedSymbolID string           `json:"selected_symbol_id,omitempty"`
	CandidateIDs     []string         `json:"candidate_ids,omitempty"`
	// CandidateRole distinguishes viable binding alternatives from a broad
	// search set (Java may include rejected overloads). Empty means unspecified.
	CandidateRole        string      `json:"candidate_role,omitempty"`
	HasUnknownCandidates bool        `json:"has_unknown_candidates,omitempty"`
	Reason               string      `json:"reason,omitempty"`
	Cause                LookupCause `json:"cause,omitempty"`
	DiagnosticCode       string      `json:"diagnostic_code,omitempty"`
	// Provenance names how the binding was established: "compiler" when a
	// language toolchain attributed it, "type_analyzer" for Pyright's static
	// declaration/type analysis, "syntax" when a syntax-tier resolver
	// derived it from declarations, scopes and imports alone.
	Provenance string             `json:"provenance,omitempty"`
	Evidence   graph.SourceAnchor `json:"evidence"`
}

// DeclarationIdentity maps a file-local declaration to its persistent entity.
// Span, Name, Owner and Key are retained so the next run can apply continuity
// rules and resolve javac targets in this file without loading its IR.
type DeclarationIdentity struct {
	DeclarationID ir.DeclarationID   `json:"declaration_id"`
	EntityID      string             `json:"entity_id"`
	Kind          ir.DeclarationKind `json:"kind"`
	Name          string             `json:"name,omitempty"`
	Span          ir.Span            `json:"span"`
	OwnerID       ir.DeclarationID   `json:"owner_id,omitempty"`
	Key           *DeclarationKey    `json:"key,omitempty"`
}

// OccurrenceIdentity maps a file-local occurrence to its persistent ID and
// the entity that encloses it. Kind and Name describe the site (its table and
// the name it spells) so a later revision of the file can continue the ID of
// the same site inside the same entity even after the bytes moved.
type OccurrenceIdentity struct {
	OccurrenceID           ir.OccurrenceID  `json:"occurrence_id"`
	PersistentID           string           `json:"persistent_id"`
	EnclosingDeclarationID ir.DeclarationID `json:"enclosing_declaration_id,omitempty"`
	EnclosingEntityID      string           `json:"enclosing_entity_id"`
	Kind                   string           `json:"kind,omitempty"`
	Name                   string           `json:"name,omitempty"`
}

// FileIdentities is the complete persistent identity map of one file variant
// at one generation. It is the unit stored per lineage in Spanner and the
// only previous-generation state the matcher and projector ever read.
type FileIdentities struct {
	Lineage       string                `json:"lineage"`
	FileID        ir.FileID             `json:"file_id"`
	Path          string                `json:"path"`
	ContentSHA256 string                `json:"content_sha256"`
	Declarations  []DeclarationIdentity `json:"declarations,omitempty"`
	Occurrences   []OccurrenceIdentity  `json:"occurrences,omitempty"`
}

// DeclarationAt returns the innermost declaration whose span contains
// [start, end), if any. It is how javac targets are mapped back to local
// declarations for files whose IR is not loaded.
func (f FileIdentities) DeclarationAt(start, end uint64) (DeclarationIdentity, bool) {
	var best DeclarationIdentity
	found := false
	for _, d := range f.Declarations {
		if d.Span.Start.ByteOffset <= start && d.Span.End.ByteOffset >= end {
			if !found || d.Span.End.ByteOffset-d.Span.Start.ByteOffset < best.Span.End.ByteOffset-best.Span.Start.ByteOffset {
				best, found = d, true
			}
		}
	}
	return best, found
}

// Declaration finds a declaration identity by local ID.
func (f FileIdentities) Declaration(id ir.DeclarationID) (DeclarationIdentity, bool) {
	for _, d := range f.Declarations {
		if d.DeclarationID == id {
			return d, true
		}
	}
	return DeclarationIdentity{}, false
}
