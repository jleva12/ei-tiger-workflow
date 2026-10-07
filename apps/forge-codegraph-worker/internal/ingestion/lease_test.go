package ingestion

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/worker/internal/retry"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type fakeRenewer struct {
	calls atomic.Int64
	fail  atomic.Int64 // number of leading calls that fail transiently
	lost  atomic.Bool
}

func (f *fakeRenewer) RenewLease(_ context.Context, l deployment.Lease, ttl time.Duration) (deployment.Lease, error) {
	n := f.calls.Add(1)
	if f.lost.Load() {
		return deployment.Lease{}, deployment.ErrFenceLost
	}
	if n <= f.fail.Load() {
		return deployment.Lease{}, status.Error(codes.Unavailable, "transient unavailable")
	}
	l.ExpiresAt = time.Now().Add(ttl)
	return l, nil
}

func lease() deployment.Lease {
	return deployment.Lease{Fence: deployment.Fence{Key: deployment.RunKey{RepositoryID: "repo", RunID: "run"}, Token: 1}, OwnerID: "owner", ExpiresAt: time.Now().Add(3 * time.Second)}
}

func TestLeaseGuardToleratesTransientRenewalErrors(t *testing.T) {
	r := &fakeRenewer{}
	r.fail.Store(2)
	g := startLease(context.Background(), r, lease(), 3*time.Second, time.Second)
	deadline := time.Now().Add(6 * time.Second)
	for r.calls.Load() < 4 && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if g.Context().Err() != nil {
		t.Fatalf("work cancelled despite recovering renewals: %v", context.Cause(g.Context()))
	}
	g.Stop()
	if g.Err() != nil {
		t.Fatalf("unexpected guard error: %v", g.Err())
	}
}

func TestLeaseGuardCancelsOnFenceLoss(t *testing.T) {
	r := &fakeRenewer{}
	r.lost.Store(true)
	g := startLease(context.Background(), r, lease(), 3*time.Second, time.Second)
	select {
	case <-g.Context().Done():
	case <-time.After(5 * time.Second):
		t.Fatal("fence loss did not cancel work")
	}
	if !errors.Is(g.Err(), deployment.ErrFenceLost) {
		t.Fatalf("expected fence loss, got %v", g.Err())
	}
	g.Stop()
}

// A lease that cannot be renewed until it expires stops the work with a
// failure that says so, keeps the last renewal error and stays retryable.
func TestLeaseGuardExplainsExpiry(t *testing.T) {
	r := &fakeRenewer{}
	r.fail.Store(1 << 30)
	l := lease()
	l.ExpiresAt = time.Now().Add(time.Second)
	g := startLease(context.Background(), r, l, time.Second, 300*time.Millisecond)
	select {
	case <-g.Context().Done():
	case <-time.After(5 * time.Second):
		t.Fatal("an expired lease did not stop the work")
	}
	err := g.Err()
	if !errors.Is(err, deployment.ErrFenceLost) || !errors.Is(err, context.DeadlineExceeded) || !strings.Contains(err.Error(), "could not be renewed for") || !strings.Contains(err.Error(), "transient unavailable") || !retry.Transient(err) {
		t.Fatalf("expiry: %q", err.Error())
	}
	g.Stop()
}
