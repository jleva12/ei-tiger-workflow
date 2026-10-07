package github

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"

	"ei-aitiger-codegraph/pkg/deployment"
)

// ValidateRevision refreshes the branch and checks immutable commit ancestry
// before any graph writes. Callers hold the repository's ingestion lease.
func (s *Service) ValidateRevision(ctx context.Context, request Request, branch, current string) error {
	if err := request.validate(); err != nil {
		return err
	}
	if !deployment.ValidBranch(branch) || (current != "" && !deployment.ValidCommit(current)) {
		return fmt.Errorf("%w: tracked branch and valid baseline required", deployment.ErrInvalidRequest)
	}
	ctx = context.WithValue(ctx, repositoryKey{}, request)
	m := s.mirrorFor(request)
	release, err := s.lock(ctx, m)
	if err != nil {
		return err
	}
	defer release()
	if err := s.syncMirror(ctx, m, request, true); err != nil {
		return err
	}
	head, err := s.git(ctx, m.path, "rev-parse", "--verify", "refs/heads/"+branch+"^{commit}")
	if err != nil {
		return fmt.Errorf("%w: tracked branch could not be resolved", deployment.ErrInvalidRequest)
	}
	head = strings.TrimSpace(head)
	if !deployment.ValidCommit(head) {
		return fmt.Errorf("%w: invalid tracked branch head", deployment.ErrIntegrity)
	}
	if err := s.ensureCommit(ctx, m, request.CommitSHA); err != nil {
		return err
	}
	if err := s.ancestor(ctx, m.path, request.CommitSHA, head); err != nil {
		return fmt.Errorf("commit is outside the tracked branch: %w", err)
	}
	if current == "" {
		return nil
	}
	if err := s.ensureCommit(ctx, m, current); err != nil {
		if errors.Is(err, ErrCommitNotFound) && ctx.Err() == nil {
			// The published commit is gone: the branch was rewritten and
			// the old history collected.
			return fmt.Errorf("%w: %s", ErrBaselineGone, current)
		}
		return err
	}
	err = s.ancestor(ctx, m.path, current, request.CommitSHA)
	if !errors.Is(err, deployment.ErrStaleDeployment) {
		return err // forward, or a failure
	}
	// Not a descendant of the published commit: older, or on a rewritten
	// history. Only an older commit is stale; a rewritten branch goes on
	// from the published graph.
	switch older := s.ancestor(ctx, m.path, request.CommitSHA, current); {
	case older == nil:
		return fmt.Errorf("commit is older than the published graph: %w", deployment.ErrStaleDeployment)
	case errors.Is(older, deployment.ErrStaleDeployment):
		return fmt.Errorf("%w: %s does not descend from %s", ErrHistoryRewritten, request.CommitSHA, current)
	default:
		return older
	}
}

func (s *Service) ancestor(ctx context.Context, mirror, base, head string) error {
	if base == head {
		return nil
	}
	_, err := s.git(ctx, mirror, "merge-base", "--is-ancestor", base, head)
	var exit *exec.ExitError
	if errors.As(err, &exit) && exit.ExitCode() == 1 {
		return deployment.ErrStaleDeployment
	}
	return err
}
