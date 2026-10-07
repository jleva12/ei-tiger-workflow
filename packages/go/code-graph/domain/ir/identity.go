package ir

// IDs are opaque and snapshot-local unless their owning catalog specifies
// otherwise. Do not use these as stable declaration identities across snapshots.
type (
	FileID        string
	ScopeID       string
	DeclarationID string
	TypeRefID     string
	ExpressionID  string
	OccurrenceID  string
	AnnotationID  string
	StatementID   string
	PatternID     string
)

// Source identifies the exact bytes and analysis context represented by a file.
// BuildContextID identifies a separately stored build inventory; it is not a
// parsed dependency graph. An absent context is unknown, not the default JDK.
type Source struct {
	FileID          FileID `json:"file_id"`
	RepositoryID    string `json:"repository_id"`
	SnapshotID      string `json:"snapshot_id"`
	Path            string `json:"path"` // Repository-relative, slash-separated.
	ContentSHA256   string `json:"content_sha256"`
	SizeBytes       uint64 `json:"size_bytes"`
	Language        string `json:"language"`
	LanguageVersion string `json:"language_version,omitempty"`
	BuildContextID  string `json:"build_context_id,omitempty"`
	ModuleID        string `json:"module_id,omitempty"`
	SourceSetID     string `json:"source_set_id,omitempty"`
	ArtifactID      string `json:"artifact_id,omitempty"`
}

// Producer identifies the extraction implementation, not a binding authority.
type Producer struct {
	Name           string `json:"name"`
	Version        string `json:"version"`
	Grammar        string `json:"grammar,omitempty"`
	GrammarVersion string `json:"grammar_version,omitempty"`
	ConfigDigest   string `json:"config_digest,omitempty"`
}

// Position is measured against the original UTF-8 source bytes, including CRLF.
// ByteOffset and Column are zero-based byte counts. Line is one-based.
// Column is not a rune count, UTF-16 offset, or editor display column.
type Position struct {
	ByteOffset uint64 `json:"byte_offset"`
	Line       uint32 `json:"line"`
	Column     uint32 `json:"column"`
}

// Span is half-open [Start, End). An EOF position is valid. Missing positions
// must use a nil *Span rather than manufacturing a span at byte zero.
type Span struct {
	Start Position `json:"start"`
	End   Position `json:"end"`
}

// Occurrence preserves one source site, even when another site has the same
// name or eventually binds to the same declaration. ID is unique across all
// occurrence-bearing tables in this file.
type Occurrence struct {
	ID                     OccurrenceID  `json:"id"`
	Span                   Span          `json:"span"`
	ScopeID                ScopeID       `json:"scope_id"`
	EnclosingDeclarationID DeclarationID `json:"enclosing_declaration_id,omitempty"`
}

// Name is a sequence of identifiers after language lexical translation. Spans
// still select their original spelling (including Java Unicode escapes).
// Dots separate segments but do not
// imply a package/type boundary or prove that the name is fully qualified.
type Name struct {
	Segments []NameSegment `json:"segments"`
}

type NameSegment struct {
	Text string `json:"text"`
	Span *Span  `json:"span,omitempty"`
}
