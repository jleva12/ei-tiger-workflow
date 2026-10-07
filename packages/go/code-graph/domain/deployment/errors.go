package deployment

import "errors"

// Adapters preserve these categories and context cancellation with errors.Is.
// Diagnostics must not contain credentials, raw source, or unbounded payloads.
var (
	ErrInvalidRequest    = errors.New("deployment: invalid request")
	ErrStaleDeployment   = errors.New("deployment: stale deployment")
	ErrFenceLost         = errors.New("deployment: lease or fence lost")
	ErrConflictingReplay = errors.New("deployment: conflicting replay")
	ErrRepositoryBusy    = errors.New("deployment: repository has an active run")
	ErrIntegrity         = errors.New("deployment: integrity failure")
	ErrUncertainCommit   = errors.New("deployment: uncertain commit outcome")
	ErrInvalidTransition = errors.New("deployment: invalid phase transition")
	ErrNotFound          = errors.New("deployment: not found")
	ErrLimitExceeded     = errors.New("deployment: limit exceeded")
	ErrUnavailable       = errors.New("deployment: required capability unavailable")
)
