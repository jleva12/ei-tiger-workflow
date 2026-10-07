package buildcontext

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
)

var (
	ErrInvalidInput       = errors.New("buildcontext: invalid input")
	ErrUnsupportedVersion = errors.New("buildcontext: unsupported version")
	ErrIdentityMismatch   = errors.New("buildcontext: checkout identity mismatch")
	ErrLimitExceeded      = errors.New("buildcontext: limit exceeded")
	// ErrNoBuild reports a checkout without a build declaration for the
	// provider's language, so a caller composing several languages can cover
	// the language with a repository-wide syntax profile instead of failing.
	ErrNoBuild = errors.New("buildcontext: no build declared")
)

// Checkout is an immutable, caller-owned local snapshot. The caller supplies
// trusted repository/revision identity and keeps checkout and mounted inputs
// unchanged until Build returns. Path is deployment data, never persisted.
type Checkout struct {
	Path         string
	RepositoryID string
	SnapshotID   string
}

type Request struct {
	Checkout Checkout
	Limits   Limits
}

// Provider returns a sealed, validated context. Incomplete inventories return
// nil error with explicit observations/diagnostics and retain missing entries.
// Malformed input, unsupported schemas, identity conflicts, I/O failures, limits
// and cancellation return zero BuildContext and an identifiable/wrapped error.
// Providers own output data and must support concurrent calls. They do not
// change checkouts or invoke builds/downloads without separate explicit policy.
type Provider interface {
	Build(context.Context, Request) (BuildContext, error)
}

// Limits are positive per-build budgets. Input bytes bound configuration reads;
// records count every catalog/check/diagnostic/root/path entry. Files counts
// inspected filesystem entries, including directories. Hash bytes are aggregate
// bytes read for fingerprints. These are not total-process memory limits.
type Limits struct {
	MaxInputBytes  uint64
	MaxRecords     uint64
	MaxDiagnostics uint64
	MaxFiles       uint64
	MaxHashBytes   uint64
	MaxDepth       uint32
	MaxOutputBytes uint64
}

func DefaultLimits() Limits {
	return Limits{MaxInputBytes: 4 << 20, MaxRecords: 1_000_000, MaxDiagnostics: 20_000, MaxFiles: 2_000_000, MaxHashBytes: 8 << 30, MaxDepth: 128, MaxOutputBytes: 128 << 20}
}

func (l Limits) Validate() error {
	if l.MaxInputBytes == 0 || l.MaxRecords == 0 || l.MaxDiagnostics == 0 || l.MaxFiles == 0 || l.MaxHashBytes == 0 || l.MaxDepth == 0 || l.MaxOutputBytes == 0 {
		return fmt.Errorf("%w: every limit must be positive", ErrInvalidInput)
	}
	// Bound allocations and recursive directory walking even if a caller passes
	// huge unsigned values. Future providers may support different ceilings.
	if l.MaxInputBytes > 16<<20 || l.MaxRecords > 1_000_000 || l.MaxDiagnostics > 100_000 || l.MaxDepth > 256 || l.MaxOutputBytes > 128<<20 {
		return fmt.Errorf("%w: unsupported limit configuration", ErrLimitExceeded)
	}
	return nil
}

func (r Request) Validate() error {
	if err := r.Limits.Validate(); err != nil {
		return err
	}
	if !validID(r.Checkout.RepositoryID) || !validID(r.Checkout.SnapshotID) || !filepath.IsAbs(r.Checkout.Path) || !validText(r.Checkout.Path) {
		return fmt.Errorf("%w: explicit repository/snapshot identity and absolute checkout path required", ErrInvalidInput)
	}
	return nil
}
