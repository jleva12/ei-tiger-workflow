package parser

import "errors"

// Adapters may wrap these sentinels with details while preserving errors.Is.
// Cancellation and deadlines must preserve context.Canceled and
// context.DeadlineExceeded. Other engine failures may wrap ordinary Go errors.
var (
	ErrInvalidInput      = errors.New("parser: invalid input")
	ErrUnsupportedConfig = errors.New("parser: unsupported configuration")
	ErrLimitExceeded     = errors.New("parser: resource limit exceeded")
	// ErrGeneratedSource is returned by an adapter that declines a file it
	// recognizes as generated or minified output (a bundle, a compiled
	// module); callers skip the file and count it rather than fail.
	ErrGeneratedSource = errors.New("parser: generated source")
)
