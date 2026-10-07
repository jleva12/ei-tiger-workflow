package github

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"

	"ei-aitiger-codegraph/pkg/deployment"
)

// ErrBranchNotFound reports that the remote has no such branch.
var ErrBranchNotFound = errors.New("github: branch not found")

// ResolveBranch returns the commit at the head of branch on GitHub, read with
// the credentials a clone of the repository would use. It asks the remote
// with git ls-remote and leaves the mirror alone, so it is cheap enough to run
// while a request is being admitted, and it fails when those credentials
// cannot read the repository.
func (s *Service) ResolveBranch(ctx context.Context, owner, name, branch string) (string, error) {
	request := Request{Owner: owner, Name: name, CommitSHA: strings.Repeat("0", 40)}
	if err := request.validate(); err != nil {
		return "", err
	}
	if !deployment.ValidBranch(branch) {
		return "", fmt.Errorf("%w: invalid branch", ErrInvalidArgument)
	}
	ref := "refs/heads/" + branch
	ctx = context.WithValue(ctx, repositoryKey{}, request)
	// --exit-code makes a missing branch exit 2, distinct from failures to
	// reach or read the repository.
	out, err := s.git(ctx, s.mirrorDir, "ls-remote", "--exit-code", "--heads", remoteURL(request), ref)
	var exit *exec.ExitError
	if errors.As(err, &exit) && exit.ExitCode() == 2 {
		return "", fmt.Errorf("%w: %s", ErrBranchNotFound, branch)
	}
	if err != nil {
		return "", fmt.Errorf("github: read %s/%s: %w", owner, name, err)
	}
	// Patterns match ref name suffixes, so pick the exact ref.
	for _, line := range strings.Split(out, "\n") {
		sha, name, ok := strings.Cut(strings.TrimSpace(line), "\t")
		if ok && name == ref {
			if !shaPattern.MatchString(sha) {
				return "", fmt.Errorf("%w: invalid head for %s", deployment.ErrIntegrity, branch)
			}
			return sha, nil
		}
	}
	return "", fmt.Errorf("%w: %s", ErrBranchNotFound, branch)
}
