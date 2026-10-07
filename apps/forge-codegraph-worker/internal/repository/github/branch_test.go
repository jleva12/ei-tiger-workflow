package github

import (
	"context"
	"errors"
	"strings"
	"testing"
)

func branchService(t *testing.T, repo fixtureRepo, tokens func(context.Context, string, string) (string, error)) (*Service, *[]string) {
	t.Helper()
	s, err := New(Config{MirrorDir: t.TempDir(), TokenSource: tokens})
	if err != nil {
		t.Fatal(err)
	}
	previous := remoteURL
	remoteURL = func(Request) string { return "file://" + repo.path }
	t.Cleanup(func() { remoteURL = previous })
	var envs []string
	run := s.run
	s.run = func(ctx context.Context, dir string, env []string, args ...string) (string, error) {
		envs = append(envs, strings.Join(env, "\n"))
		// Only tests substitute a local transport. Production permits HTTPS only.
		return run(ctx, dir, env, append([]string{"-c", "protocol.file.allow=always"}, args...)...)
	}
	return s, &envs
}

func TestResolveBranchReadsTheRemoteHeadWithTheRequestCredentials(t *testing.T) {
	repo := fixture(t)
	var asked []string
	s, envs := branchService(t, repo, func(_ context.Context, owner, name string) (string, error) {
		asked = append(asked, owner+"/"+name)
		return "submitter-token", nil
	})
	sha, err := s.ResolveBranch(context.Background(), "team", "repo", "main")
	if err != nil {
		t.Fatal(err)
	}
	if sha != repo.head {
		t.Fatalf("head = %s, want %s", sha, repo.head)
	}
	if len(asked) != 1 || asked[0] != "team/repo" {
		t.Fatalf("credentials requested for %v", asked)
	}
	if len(*envs) != 1 || !strings.Contains((*envs)[0], "Authorization: Basic") {
		t.Fatal("ls-remote ran without the request credentials")
	}
	if mirrorExists(mirrorPath(s)) {
		t.Fatal("resolving a branch created a mirror")
	}
}

func TestResolveBranchFailures(t *testing.T) {
	repo := fixture(t)
	s, _ := branchService(t, repo, func(context.Context, string, string) (string, error) { return "", nil })
	if _, err := s.ResolveBranch(context.Background(), "team", "repo", "missing"); !errors.Is(err, ErrBranchNotFound) {
		t.Fatalf("missing branch: %v", err)
	}
	for _, branch := range []string{"", "-upload-pack=x", "a..b", "main*"} {
		if _, err := s.ResolveBranch(context.Background(), "team", "repo", branch); !errors.Is(err, ErrInvalidArgument) {
			t.Errorf("branch %q: %v", branch, err)
		}
	}
	if _, err := s.ResolveBranch(context.Background(), "-team", "repo", "main"); !errors.Is(err, ErrInvalidArgument) {
		t.Errorf("owner option: %v", err)
	}

	denied := errors.New("no connected account")
	s, envs := branchService(t, repo, func(context.Context, string, string) (string, error) { return "", denied })
	if _, err := s.ResolveBranch(context.Background(), "team", "repo", "main"); !errors.Is(err, denied) {
		t.Fatalf("credential failure: %v", err)
	}
	if len(*envs) != 0 {
		t.Fatal("ran git without credentials")
	}
}
