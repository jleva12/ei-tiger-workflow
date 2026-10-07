// Package retry defines the worker's conservative automatic retry policy.
package retry

import (
	"context"
	"errors"
	"io"
	"net"
	"syscall"

	"ei-aitiger-codegraph/pkg/deployment"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Transient returns true only for recognized recoverable failures. All causes
// of a joined error must be recoverable: a canceled sibling or unavailable
// cleanup must never hide a permanent provider, validation, or integrity error.
func Transient(err error) bool {
	if err == nil {
		return false
	}
	if e, ok := err.(interface{ Retryable() bool }); ok {
		return e.Retryable()
	}
	if e, ok := err.(interface{ Unwrap() []error }); ok {
		causes := e.Unwrap()
		if len(causes) == 0 {
			return false
		}
		for _, cause := range causes {
			if cause != nil && !Transient(cause) {
				return false
			}
		}
		return true
	}
	// Inspect a status before unwrapping its transport-specific implementation.
	if e, ok := err.(interface{ GRPCStatus() *status.Status }); ok {
		switch e.GRPCStatus().Code() {
		case codes.Unavailable, codes.Aborted, codes.DeadlineExceeded, codes.Canceled:
			return true
		case codes.Unknown:
			// Spanner can wrap application callback errors in an Unknown
			// status. Preserve the underlying typed domain error below.
		default:
			return false
		}
	}
	if e, ok := err.(*net.DNSError); ok {
		return e.IsTimeout || e.IsTemporary
	}
	if e, ok := err.(interface{ Unwrap() error }); ok {
		return Transient(e.Unwrap())
	}
	switch err {
	case context.Canceled, context.DeadlineExceeded, deployment.ErrFenceLost,
		deployment.ErrRepositoryBusy, deployment.ErrUncertainCommit,
		io.EOF, io.ErrUnexpectedEOF, syscall.ECONNRESET, syscall.ECONNREFUSED,
		syscall.ETIMEDOUT, syscall.EPIPE, syscall.ENETUNREACH, syscall.EHOSTUNREACH,
		// A full disk is an operational condition; cleanup can free it.
		syscall.ENOSPC:
		return true
	}
	var timeout net.Error
	return errors.As(err, &timeout) && timeout.Timeout()
}
