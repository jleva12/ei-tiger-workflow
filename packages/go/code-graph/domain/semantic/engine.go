package semantic

import (
	"context"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/parser"
)

// ResolveRequest names the compilation contexts to attribute. The resolver
// writes symbols for every file it attributes and lookups for affected files.
// It reads the checkout directly (javac needs real files on a sourcepath).
type ResolveRequest struct {
	Run          deployment.RunKey
	CommitSHA    string
	CheckoutPath string
	Contexts     []bc.SourceSetID
	SyntaxLimits parser.Limits
	BuildLimits  bc.Limits
}

type ResolutionResult struct {
	Symbols     uint64 `json:"symbols"`
	Resolved    uint64 `json:"resolved"`
	Unresolved  uint64 `json:"unresolved"`
	Ambiguous   uint64 `json:"ambiguous"`
	Unsupported uint64 `json:"unsupported"`
	// Skipped counts affected files the resolver could not bind because the
	// parse stage produced no syntax for them (a file beyond the parser's
	// limits); they contribute no symbols and no lookups.
	Skipped uint64 `json:"skipped,omitempty"`
	// Warnings say what the resolver could not use although it went on,
	// such as an annotation processor that would not run; one line each.
	Warnings []string `json:"warnings,omitempty"`
	// CompiledOutputs are the source sets the build declared without compiled
	// output (buildcontext.GapCompiledOutput) whose output the resolver
	// compiled itself: references into them resolve after all.
	CompiledOutputs []string `json:"compiled_outputs,omitempty"`
	// Degraded are the contexts whose resolution stopped short for a reason
	// that may not recur (the compiler or analyzer crashed, ran out of time
	// or memory): the next run resolves them again, unchanged files too.
	Degraded []string `json:"degraded,omitempty"`
}

// Resolver binds names with the language's own toolchain. Unresolved lookups
// are recorded, never hidden, and never fail the run by themselves; the audit
// carries the counts. Version and PolicyDigest fingerprint the implementation.
type Resolver interface {
	Resolve(context.Context, ResolveRequest, Workspace) (ResolutionResult, error)
	Version() string
	PolicyDigest() string
}

type MatchRequest struct {
	Run   deployment.RunKey
	Files []SourceInput // affected files only
}

type MatchResult struct {
	Allocated uint64 `json:"allocated"`
	Continued uint64 `json:"continued"`
	Ambiguous uint64 `json:"ambiguous"`
}

// Matcher assigns persistent entity and occurrence IDs to every declaration
// and site of each affected file, continuing IDs from the previous generation
// where its rules prove continuity, and writes one FileIdentities per file.
type Matcher interface {
	Match(context.Context, MatchRequest, Workspace) (MatchResult, error)
}

type ProjectRequest struct {
	Run          deployment.RunKey
	Files        []SourceInput // affected files only
	SyntaxLimits parser.Limits
	// Progress, when set, is called after each file's facts are emitted.
	Progress func()
}

type ProjectionResult struct {
	Nodes uint64 `json:"nodes"`
	Edges uint64 `json:"edges"`
}

// Emit receives one projected fact. The projector calls it per fact, in file
// order, and never buffers a repository's worth of facts.
type Emit func(context.Context, graph.Fact) error

// Projector turns identities and lookups into canonical nodes and edges.
// Facts for a file are emitted after that file's identities exist; facts that
// have no lineage (external, intrinsic, constructed symbols) are emitted once.
type Projector interface {
	Project(context.Context, ProjectRequest, Workspace, Emit) (ProjectionResult, error)
}
