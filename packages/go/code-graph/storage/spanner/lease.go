package spannerstore

import (
	"context"
	"fmt"
	"math"
	"time"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/deployment"
)

const maxLeaseTTL = 24 * time.Hour

var leaseColumns = []string{"RepositoryID", "LeaseOwner", "LeaseRunID", "LeaseToken", "LeaseExpiresAt"}

// AcquireLease makes (owner, run) the repository's sole writer until ttl
// elapses on database time. An unexpired lease held by another owner or run
// fails with deployment.ErrRepositoryBusy; an expired one is taken over. Every
// acquisition, including a re-acquisition by the same holder, issues a higher
// fence token, so earlier tokens stop working.
func (s *Store) AcquireLease(ctx context.Context, key deployment.RunKey, owner string, ttl time.Duration) (deployment.Lease, error) {
	if key.Validate() != nil || !validText(owner, 128) || ttl <= 0 || ttl > maxLeaseTTL {
		return deployment.Lease{}, fmt.Errorf("%w: lease request", deployment.ErrInvalidRequest)
	}
	var out deployment.Lease
	err := s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		r, err := readRepo(ctx, t, key.RepositoryID)
		if err != nil {
			return err
		}
		n, err := now(ctx, t)
		if err != nil {
			return err
		}
		if r.LeaseExpiresAt.Valid && r.LeaseExpiresAt.Time.After(n) && (r.LeaseOwner != owner || r.LeaseRunID != key.RunID) {
			return fmt.Errorf("%w: run %s held by %s until %s", deployment.ErrRepositoryBusy, r.LeaseRunID, r.LeaseOwner, r.LeaseExpiresAt.Time.UTC().Format(time.RFC3339))
		}
		if r.LeaseToken >= math.MaxInt64 {
			return deployment.ErrLimitExceeded
		}
		token := r.LeaseToken + 1
		out = deployment.Lease{Fence: deployment.Fence{Key: key, Token: uint64(token)}, OwnerID: owner, ExpiresAt: n.Add(ttl)}
		return t.BufferWrite([]*spanner.Mutation{spanner.Update("CGRepositories", leaseColumns, []any{key.RepositoryID, owner, key.RunID, token, spanner.NullTime{Time: out.ExpiresAt, Valid: true}})})
	})
	if err != nil {
		return deployment.Lease{}, err
	}
	return out, nil
}

// RenewLease extends an unexpired lease held by exactly this fence and owner.
// It writes LeaseExpiresAt alone: the identity cells are unchanged, and not
// touching them keeps the renewal off the locks that concurrent fenced commits
// hold (see checkFence), so a renewal during a load neither waits for the
// loader's in-flight commits nor stalls the ones behind it.
func (s *Store) RenewLease(ctx context.Context, lease deployment.Lease, ttl time.Duration) (deployment.Lease, error) {
	if lease.Validate() != nil || ttl <= 0 || ttl > maxLeaseTTL {
		return deployment.Lease{}, fmt.Errorf("%w: lease renewal", deployment.ErrInvalidRequest)
	}
	out := lease
	err := s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		_, n, err := checkLease(ctx, t, lease)
		if err != nil {
			return err
		}
		out.ExpiresAt = n.Add(ttl)
		return t.BufferWrite([]*spanner.Mutation{spanner.Update("CGRepositories", []string{"RepositoryID", "LeaseExpiresAt"}, []any{lease.Fence.Key.RepositoryID, spanner.NullTime{Time: out.ExpiresAt, Valid: true}})})
	})
	if err != nil {
		return deployment.Lease{}, err
	}
	return out, nil
}

// ReleaseLease clears the lease if this fence still holds it, expired or not.
// A lease that was taken over reports deployment.ErrFenceLost.
func (s *Store) ReleaseLease(ctx context.Context, lease deployment.Lease) error {
	if err := lease.Validate(); err != nil {
		return err
	}
	return s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		key := lease.Fence.Key
		r, err := readRepo(ctx, t, key.RepositoryID)
		if err != nil {
			return err
		}
		if r.LeaseToken == int64(lease.Fence.Token) && r.LeaseOwner == "" {
			return nil // already released by this fence
		}
		if r.LeaseToken != int64(lease.Fence.Token) || r.LeaseOwner != lease.OwnerID || r.LeaseRunID != key.RunID {
			return fmt.Errorf("%w: lease token %d no longer current", deployment.ErrFenceLost, lease.Fence.Token)
		}
		return t.BufferWrite([]*spanner.Mutation{spanner.Update("CGRepositories", leaseColumns, []any{key.RepositoryID, "", "", r.LeaseToken, spanner.NullTime{}})})
	})
}
