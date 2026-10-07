package spannerstore

import (
	"context"
	"fmt"
	"math"

	"cloud.google.com/go/spanner"
	"ei-aitiger-codegraph/pkg/deployment"
)

// SubmissionRun returns the run a submission was admitted as: what
// AdmitIngestion recorded for SubmissionID, whatever request it was. A worker
// that admitted a job's run and died before recording it finds the run here
// instead of admitting the job again, even when its own configuration
// changed in between. deployment.ErrNotFound when nothing was admitted for it.
func (s *Store) SubmissionRun(ctx context.Context, submissionID string) (deployment.RunKey, error) {
	if !validText(submissionID, 256) {
		return deployment.RunKey{}, fmt.Errorf("%w: submission id", deployment.ErrInvalidRequest)
	}
	row, err := s.client.Single().ReadRow(ctx, "CGSubmissions", spanner.Key{submissionID}, []string{"RepositoryID", "RunID"})
	if err != nil {
		return deployment.RunKey{}, mapRead(err, deployment.ErrNotFound)
	}
	var key deployment.RunKey
	if err = row.Columns(&key.RepositoryID, &key.RunID); err != nil {
		return deployment.RunKey{}, err
	}
	return key, nil
}

// FailRun records a run that will not be resumed as FAILED, with code, when
// the worker gives up on its job (its attempts are spent, or it failed in a
// way it can't retry) before the run recorded an end of its own. A terminal
// run is left alone, and so is one whose repository lease is held for it
// and unexpired: another worker is running it. A running index becomes
// INCOMPLETE. The next ingestion of the repository resumes or supersedes it.
func (s *Store) FailRun(ctx context.Context, key deployment.RunKey, code string, retryable bool) error {
	if err := key.Validate(); err != nil {
		return err
	}
	if !validText(code, 128) {
		return fmt.Errorf("%w: failure code", deployment.ErrInvalidRequest)
	}
	return s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		run, identity, err := readRun(ctx, t, key)
		if err != nil {
			return err
		}
		if run.Terminal() || run.Phase == deployment.Failed {
			return nil
		}
		repo, err := readRepo(ctx, t, key.RepositoryID)
		if err != nil {
			return err
		}
		n, err := now(ctx, t)
		if err != nil {
			return err
		}
		if repo.LeaseRunID == key.RunID && repo.LeaseExpiresAt.Valid && repo.LeaseExpiresAt.Time.After(n) {
			return nil
		}
		if run.Revision >= math.MaxInt64 {
			return deployment.ErrLimitExceeded
		}
		run.Phase, run.ErrorCode, run.FinishedAt, run.UpdatedAt = deployment.Failed, code, &n, n
		run.Revision++
		run.FailureRetryable = &retryable
		if run.Index != nil && run.Index.Status == deployment.IndexRunning {
			index := *run.Index
			index.Status, index.FinishedAt, index.Error = deployment.IndexIncomplete, &n, code
			run.Index = &index
		}
		m, err := runMutation(run, identity)
		if err != nil {
			return err
		}
		return t.BufferWrite([]*spanner.Mutation{m})
	})
}
