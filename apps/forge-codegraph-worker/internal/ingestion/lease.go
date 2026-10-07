package ingestion

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/worker/internal/retry"
)

// leaseRenewer renews a repository lease until Stop. Transient renewal
// errors are retried until the lease actually expires; only a definitive
// fence loss or expiry cancels the work context. Expiry is compared using
// the database-issued ExpiresAt against the moment the renewal succeeded, so
// host clock drift cannot expire a lease the database still honours.
type leaseRenewer interface {
	RenewLease(context.Context, deployment.Lease, time.Duration) (deployment.Lease, error)
}

type leaseGuard struct {
	work       context.Context
	cancelWork context.CancelCauseFunc
	stop       chan struct{}
	done       chan struct{}
	mu         sync.Mutex
	lease      deployment.Lease
	err        error
}

func startLease(parent context.Context, store leaseRenewer, lease deployment.Lease, ttl, interval time.Duration) *leaseGuard {
	work, cancel := context.WithCancelCause(parent)
	g := &leaseGuard{work: work, cancelWork: cancel, stop: make(chan struct{}), done: make(chan struct{}), lease: lease}
	go g.loop(store, ttl, interval)
	return g
}

func (g *leaseGuard) loop(store leaseRenewer, ttl, interval time.Duration) {
	defer close(g.done)
	// remaining tracks lease validity as a duration from the last successful
	// renewal, never as an absolute host timestamp.
	renewedAt := time.Now()
	remaining := time.Until(g.lease.ExpiresAt)
	if remaining <= 0 {
		remaining = ttl
	}
	failures := 0
	var lastErr error
	for {
		delay := min(interval, remaining/3)
		if failures > 0 {
			// A lease in danger is retried quickly rather than on the
			// normal cadence; expiry is still checked before each attempt.
			delay = max(250*time.Millisecond, delay/4)
		}
		timer := time.NewTimer(delay)
		select {
		case <-g.stop:
			timer.Stop()
			return
		case <-timer.C:
		}
		elapsed := time.Since(renewedAt)
		if elapsed >= remaining {
			// Renewal failures are transient, and so is the expiry: the job
			// is retried.
			g.lose(errors.Join(fmt.Errorf("the lease expired after it could not be renewed for %s: %w", elapsed.Round(time.Second), context.DeadlineExceeded), lastErr))
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), min(20*time.Second, remaining-elapsed))
		next, err := store.RenewLease(ctx, g.current(), ttl)
		cancel()
		if err == nil {
			if next.Validate() != nil || next.Fence != g.lease.Fence {
				g.lose(errors.Join(deployment.ErrIntegrity, errors.New("renewed lease differs")))
				return
			}
			g.mu.Lock()
			g.lease = next
			g.mu.Unlock()
			renewedAt = time.Now()
			remaining = ttl
			failures = 0
			continue
		}
		if errors.Is(err, deployment.ErrFenceLost) || !retry.Transient(err) {
			g.lose(err)
			return
		}
		// Transient failure: keep looping; expiry is checked at the top of the loop.
		failures++
		lastErr = err
	}
}

func (g *leaseGuard) current() deployment.Lease {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.lease
}

func (g *leaseGuard) lose(err error) {
	g.mu.Lock()
	g.err = errors.Join(deployment.ErrFenceLost, err)
	cause := g.err
	g.mu.Unlock()
	g.cancelWork(cause)
}

func (g *leaseGuard) Context() context.Context { return g.work }
func (g *leaseGuard) Lease() deployment.Lease  { return g.current() }
func (g *leaseGuard) Err() error {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.err
}
func (g *leaseGuard) Stop() {
	close(g.stop)
	<-g.done
	g.cancelWork(context.Canceled)
}
