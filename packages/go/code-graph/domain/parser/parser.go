package parser

import (
	"context"

	"ei-aitiger-codegraph/pkg/ir"
)

// Parser extracts syntax for one immutable source file. Implementations must
// be safe for concurrent calls, honor cancellation/deadlines, validate inputs,
// and enforce all requested limits. They must not read paths from disk, mutate
// input buffers, or retain references to mutable caller-owned data after return.
//
// A nil error means the returned file passes SourceFile.Validate, preserves
// Input.Source exactly, identifies its Producer and Coverage.FeatureSet, and
// obeys output limits. Recoverable syntax errors/unsupported constructs belong
// in Diagnostics and Coverage with partial/failed status, not a Go error.
// Failed coverage must still have a valid file/root scope and explain the loss.
// Effective syntax options must contribute to Producer.ConfigDigest.
//
// A non-nil error means no usable artifact: return the zero ir.SourceFile.
// Invalid inputs, unsupported configurations, hard resource limits, context
// cancellation, and engine failures use this path. Never silently truncate IR
// or change the requested language version/options to make extraction succeed.
//
// For identical input/configuration and producer version, successful extraction
// must produce deterministic IDs, record order, diagnostics, and coverage.
// Lifecycle/configuration of an engine belongs to its concrete adapter.
type Parser interface {
	Parse(ctx context.Context, input Input) (ir.SourceFile, error)
}
