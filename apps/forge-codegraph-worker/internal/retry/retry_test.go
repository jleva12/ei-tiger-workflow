package retry

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"syscall"
	"testing"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestTransient(t *testing.T) {
	for _, tc := range []struct {
		name string
		err  error
		want bool
	}{
		{"nil", nil, false},
		{"unknown", errors.New("temporary unavailable"), false},
		{"integrity", deployment.ErrIntegrity, false},
		{"missing capability", deployment.ErrUnavailable, false},
		{"bad input", deployment.ErrInvalidRequest, false},
		{"permission", status.Error(codes.PermissionDenied, "denied"), false},
		{"quota unknown", status.Error(codes.ResourceExhausted, "quota"), false},
		{"unavailable", status.Error(codes.Unavailable, "down"), true},
		{"aborted transaction", status.Error(codes.Aborted, "conflict"), true},
		{"timeout", context.DeadlineExceeded, true},
		{"truncated response", io.ErrUnexpectedEOF, true},
		{"reset", &net.OpError{Op: "read", Net: "tcp", Err: syscall.ECONNRESET}, true},
		{"unknown DNS host", &net.DNSError{IsNotFound: true}, false},
		{"temporary DNS", &net.DNSError{IsTemporary: true}, true},
		{"rate limit", &codesearch.ProviderError{Status: 429}, true},
		{"billing quota", &codesearch.ProviderError{Status: 429, QuotaExhausted: true}, false},
		{"bad credentials", &codesearch.ProviderError{Status: 401}, false},
		{"wrapped transient", fmt.Errorf("index: %w", status.Error(codes.Unavailable, "down")), true},
		{"lease cancellation", errors.Join(context.Canceled, deployment.ErrFenceLost), true},
		{"permanent plus canceled", errors.Join(context.Canceled, &codesearch.ProviderError{Status: 401}), false},
		{"permanent plus cleanup outage", errors.Join(deployment.ErrIntegrity, status.Error(codes.Unavailable, "down")), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := Transient(tc.err); got != tc.want {
				t.Fatalf("Transient(%v)=%v, want %v", tc.err, got, tc.want)
			}
		})
	}
}
