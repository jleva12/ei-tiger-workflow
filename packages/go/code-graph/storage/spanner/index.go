package spannerstore

import (
	"context"
	"fmt"
	"math"

	"cloud.google.com/go/spanner"
	"ei-aitiger-codegraph/pkg/deployment"
)

func validateRunTransition(stored, next deployment.Run) error {
	if stored.Index != nil && next.Index == nil {
		return fmt.Errorf("%w: required index cannot be removed", deployment.ErrInvalidTransition)
	}
	if next.Phase == deployment.Succeeded && next.Index != nil && next.Index.Status != deployment.IndexComplete {
		return fmt.Errorf("%w: required embeddings are incomplete", deployment.ErrInvalidTransition)
	}
	// Repair records produced before embeddings became part of success.
	if stored.Phase == deployment.Succeeded && !stored.Terminal() && (next.Phase == deployment.Running || next.Phase == deployment.Failed) {
		return nil
	}
	return deployment.ValidateTransition(stored.Phase, next.Phase)
}

// UpdateIndex records progress or settles a published run. Success requires a
// completed index, the current repository lease, and the same live generation.
// A late callback from an expired worker cannot overwrite its successor.
func (s *Store) UpdateIndex(ctx context.Context, lease deployment.Lease, run deployment.Run) (deployment.Run, error) {
	if err := run.Validate(); err != nil {
		return deployment.Run{}, err
	}
	if err := lease.Validate(); err != nil {
		return deployment.Run{}, err
	}
	if run.Key != lease.Fence.Key || run.Index == nil || run.Generation == 0 {
		return deployment.Run{}, fmt.Errorf("%w: index run and lease", deployment.ErrInvalidRequest)
	}
	if (run.Phase == deployment.Succeeded && run.Index.Status != deployment.IndexComplete) ||
		(run.Phase == deployment.Failed && run.Index.Status != deployment.IndexIncomplete) ||
		(run.Phase == deployment.Running && run.Index.Status != deployment.IndexRunning) ||
		(run.Phase != deployment.Running && run.Phase != deployment.Failed && run.Phase != deployment.Succeeded) {
		return deployment.Run{}, fmt.Errorf("%w: index and run phase disagree", deployment.ErrInvalidTransition)
	}
	var out deployment.Run
	err := s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		repo, n, err := checkLease(ctx, t, lease)
		if err != nil {
			return err
		}
		if repo.LiveRunID != run.Key.RunID || uint64(repo.LiveGeneration) != run.Generation {
			return deployment.ErrStaleDeployment
		}
		stored, identity, err := readRun(ctx, t, run.Key)
		if err != nil {
			return err
		}
		if stored.Revision != run.Revision {
			return deployment.ErrConflictingReplay
		}
		if stored.Revision >= math.MaxInt64 {
			return deployment.ErrLimitExceeded
		}
		if err = validateRunTransition(stored, run); err != nil {
			return err
		}
		out = run
		out.SchemaVersion, out.Key, out.Request, out.AcceptedAt = stored.SchemaVersion, stored.Key, stored.Request, stored.AcceptedAt
		out.Revision, out.UpdatedAt = stored.Revision+1, n
		out.FinishedAt = nil
		if out.Phase != deployment.Running {
			out.FinishedAt = &n
		}
		m, err := runMutation(out, identity)
		if err != nil {
			return err
		}
		return t.BufferWrite([]*spanner.Mutation{m})
	})
	if err != nil {
		return deployment.Run{}, err
	}
	return out, nil
}
