package parser

import "ei-aitiger-codegraph/pkg/ir"

// Input is one in-memory parse request, not a file locator. Content contains
// the original UTF-8 bytes, including any BOM, CRLF, and written escapes. A nil
// slice represents a valid empty file when the size/digest agree.
//
// Source.Language and Source.LanguageVersion are the sole language/version
// settings and are both required at this boundary. Source also identifies the
// repository, snapshot, file, content hash, and optional build context. Adapters
// must not infer settings from a filename, host JDK, or backend default.
// Callers must keep Content unchanged until Parse returns.
type Input struct {
	Source  ir.Source
	Content []byte
	Options Options
	Limits  Limits
}

// Options contains syntax settings beyond Source.Language/LanguageVersion.
// An adapter must reject unsupported combinations with ErrUnsupportedConfig.
// Preview syntax is disabled by default and never enabled implicitly.
type Options struct {
	EnablePreview bool
	// Settings holds adapter-defined syntax options. Adapters must reject unknown
	// keys and include effective settings in Producer.ConfigDigest.
	Settings map[string]string `json:",omitempty"`
}

// Limits are positive, per-call hard limits. Zero never means unlimited or
// "use defaults"; callers can explicitly start with DefaultLimits(). Deadlines
// are supplied through context, not a second timeout field.
//
// Syntax depth counts the root as one and includes engine trees and extraction
// traversal. Syntax-node count includes unnamed, recovery, and missing nodes.
// IR-record count is the sum of the thirteen SourceFile record-table lengths,
// plus Package, Module and each module directive when present. Diagnostics and Coverage.Issues share their own
// combined count limit. Output bytes measure json.Marshal of the entire file.
//
// These budgets do not cap total process memory, native allocations, or
// concurrent calls. An adapter must check syntax budgets before IR extraction
// and output budgets while building records, with a final envelope-size check.
// Engines that cannot count nodes/depth during native tree construction must
// document that boundary and support cancellation of native parsing.
type Limits struct {
	MaxSourceBytes uint64
	MaxSyntaxNodes uint64
	MaxSyntaxDepth uint32
	MaxIRRecords   uint64
	MaxDiagnostics uint32
	MaxOutputBytes uint64
}

// DefaultLimits provides initial application budgets, not engine guarantees.
// Callers can tune a copy; adapters reject budgets they cannot support.
func DefaultLimits() Limits {
	return Limits{
		MaxSourceBytes: 4 << 20,
		MaxSyntaxNodes: 250_000,
		MaxSyntaxDepth: 4096,
		MaxIRRecords:   100_000,
		MaxDiagnostics: 100,
		MaxOutputBytes: 32 << 20,
	}
}
