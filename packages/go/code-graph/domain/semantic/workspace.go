package semantic

import (
	"context"
	"errors"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/ir"
)

var (
	ErrNotFound  = errors.New("semantic: not found")
	ErrIntegrity = errors.New("semantic: integrity failure")
	ErrInvalid   = errors.New("semantic: invalid request")
)

// Workspace is the run-local index shared by the resolver, matcher and
// projector. It is disposable: every fact in it is derived from the checkout,
// the build context and the previous generation's identity maps. Reads return
// exactly what was written, without re-validation; callers validate at their
// own boundaries. Implementations are safe for concurrent use.
type Workspace interface {
	// Build returns the evaluated build context for the commit.
	Build() bc.BuildContext
	// Files lists every discovered source variant, ordered by module, source
	// set and path. Affected files carry Affected=true.
	Files(ctx context.Context) ([]SourceInput, error)
	// File and FileByPath resolve inventory entries. FileByPath matches the
	// repository-relative path within one source set.
	File(ctx context.Context, id ir.FileID) (SourceInput, error)
	FileByPath(ctx context.Context, sourceSetID bc.SourceSetID, path string) (SourceInput, error)
	// Syntax returns the parsed IR of an affected file from the syntax cache.
	Syntax(ctx context.Context, in SourceInput) (ir.SourceFile, error)
	// SourceBytes returns the exact bytes of a file in the checkout.
	SourceBytes(ctx context.Context, in SourceInput) ([]byte, error)

	// Symbols are written by the resolver in bulk and read by ID or by file.
	PutSymbols(ctx context.Context, symbols []Symbol) error
	Symbol(ctx context.Context, id string) (Symbol, error)
	Symbols(ctx context.Context, ids []string) (map[string]Symbol, error)
	SymbolsByFile(ctx context.Context, id ir.FileID) ([]Symbol, error)
	// EachSymbol visits every symbol once, in ID order.
	EachSymbol(ctx context.Context, fn func(Symbol) error) error
	// Lookups are written per affected file and read back per file.
	PutLookups(ctx context.Context, id ir.FileID, lookups []Lookup) error
	Lookups(ctx context.Context, id ir.FileID) ([]Lookup, error)

	// Identities of affected files are written by the matcher. Previous
	// identities come from the baseline generation in Spanner and are cached.
	PutIdentities(ctx context.Context, f FileIdentities) error
	Identities(ctx context.Context, id ir.FileID) (FileIdentities, error)
	PreviousIdentities(ctx context.Context, lineage string) (FileIdentities, error)
	// Entity resolves a (file, declaration) pair to its persistent entity ID,
	// from this run's identities for affected files or from the previous
	// generation's map for unchanged files.
	Entity(ctx context.Context, id ir.FileID, declaration ir.DeclarationID) (string, error)
}

// PreviousIdentityReader supplies baseline identity maps by lineage. A missing
// lineage is ErrNotFound. Implementations read one generation and never a
// generation above it.
type PreviousIdentityReader interface {
	PreviousIdentities(ctx context.Context, lineage string) (FileIdentities, error)
}
