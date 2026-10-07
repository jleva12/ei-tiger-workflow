package github

import (
	"context"
	"errors"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/deployment"
)

func TestValidateRevisionUsesGitAncestry(t *testing.T) {
	repo := fixture(t)
	s, _ := localService(t, repo)
	for _, tc := range []struct {
		name, branch, commit, current string
		want                          error
	}{
		{"initial", "main", repo.base, "", nil},
		{"forward", "main", repo.head, repo.base, nil},
		{"identical", "main", repo.head, repo.head, nil},
		{"backward", "main", repo.base, repo.head, deployment.ErrStaleDeployment},
		{"outside branch", "main", repo.orphan, repo.head, deployment.ErrStaleDeployment},
		{"missing branch", "missing", repo.head, repo.base, deployment.ErrInvalidRequest},
		{"ref expression", "main~1", repo.head, repo.base, deployment.ErrInvalidRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := s.ValidateRevision(context.Background(), Request{Owner: "Example", Name: "Demo", CommitSHA: tc.commit}, tc.branch, tc.current)
			if !errors.Is(err, tc.want) {
				t.Fatalf("got %v want %v", err, tc.want)
			}
		})
	}
	// Force-pushing the tracked branch behind the live graph must not permit
	// rollback or allow the old head to be ingested under the branch label.
	localGit(t, repo.path, "update-ref", "refs/heads/main", repo.base)
	if err := s.ValidateRevision(context.Background(), Request{Owner: "Example", Name: "Demo", CommitSHA: repo.base}, "main", repo.head); !errors.Is(err, deployment.ErrStaleDeployment) {
		t.Fatalf("force-pushed rollback: %v", err)
	}
	if err := s.ValidateRevision(context.Background(), Request{Owner: "Example", Name: "Demo", CommitSHA: repo.head}, "main", repo.head); !errors.Is(err, deployment.ErrStaleDeployment) {
		t.Fatalf("removed from branch: %v", err)
	}
}

// A force-push that rewrites the branch is not a rollback: the new history
// goes on from the published graph, and a published commit the rewrite
// took away is reported as gone rather than failing every later run.
func TestRewrittenHistoryGoesOn(t *testing.T) {
	repo := fixture(t)
	s, _ := localService(t, repo)
	localGit(t, repo.path, "checkout", "--quiet", "-B", "main", repo.base)
	writeFile(t, repo.path, "rewritten.txt", "after the force-push\n")
	localGit(t, repo.path, "add", ".")
	localGit(t, repo.path, "commit", "--quiet", "-m", "rewritten")
	rewritten := localGit(t, repo.path, "rev-parse", "HEAD")

	err := s.ValidateRevision(context.Background(), Request{Owner: "Example", Name: "Demo", CommitSHA: rewritten}, "main", repo.head)
	if !errors.Is(err, ErrHistoryRewritten) || errors.Is(err, deployment.ErrStaleDeployment) {
		t.Fatalf("rewritten branch: %v", err)
	}
	// The old history is still an older commit, not a rewrite.
	if err := s.ValidateRevision(context.Background(), Request{Owner: "Example", Name: "Demo", CommitSHA: repo.base}, "main", rewritten); !errors.Is(err, deployment.ErrStaleDeployment) {
		t.Fatalf("older commit: %v", err)
	}
	gone := strings.Repeat("0123456789abcdef", 2) + "01234567"
	if err := s.ValidateRevision(context.Background(), Request{Owner: "Example", Name: "Demo", CommitSHA: rewritten}, "main", gone); !errors.Is(err, ErrBaselineGone) {
		t.Fatalf("published commit gone: %v", err)
	}
}
