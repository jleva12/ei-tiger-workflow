package github

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

func localGit(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-c", "protocol.file.allow=always"}, args...)...)
	cmd.Dir = dir
	cmd.Env = append(gitEnvironment(""), "GIT_AUTHOR_NAME=Test", "GIT_AUTHOR_EMAIL=test@example.com", "GIT_COMMITTER_NAME=Test", "GIT_COMMITTER_EMAIL=test@example.com")
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v: %s", args, err, output)
	}
	return strings.TrimSpace(string(output))
}

func writeFile(t *testing.T, root, name, content string) {
	t.Helper()
	path := filepath.Join(root, name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

// fixtureRepo is a local repository standing in for github.com. base and
// head are consecutive commits on main; orphan is reachable from no ref, so
// only an explicit SHA fetch can bring it into a mirror.
type fixtureRepo struct {
	path               string
	base, head, orphan string
}

func fixture(t *testing.T) fixtureRepo {
	t.Helper()
	path := t.TempDir()
	localGit(t, path, "init", "--quiet", "--initial-branch=main", "--template=")
	writeFile(t, path, "a.txt", "alpha\nbeta\ngamma\n")
	writeFile(t, path, "b.txt", "bravo\ncharlie\n")
	writeFile(t, path, "dir/c.txt", strings.Repeat("stable line\n", 10))
	localGit(t, path, "add", ".")
	localGit(t, path, "commit", "--quiet", "-m", "base")
	base := localGit(t, path, "rev-parse", "HEAD")
	writeFile(t, path, "a.txt", "alpha\nBETA\ngamma\n")
	localGit(t, path, "rm", "--quiet", "b.txt")
	localGit(t, path, "mv", "dir/c.txt", "dir/d.txt")
	writeFile(t, path, "new.txt", "x1\ny2\nz3\n")
	localGit(t, path, "add", ".")
	localGit(t, path, "commit", "--quiet", "-m", "head")
	head := localGit(t, path, "rev-parse", "HEAD")
	localGit(t, path, "checkout", "--quiet", "-b", "orphan")
	writeFile(t, path, "orphan.txt", "unreachable\n")
	localGit(t, path, "add", ".")
	localGit(t, path, "commit", "--quiet", "-m", "orphan")
	orphan := localGit(t, path, "rev-parse", "HEAD")
	localGit(t, path, "checkout", "--quiet", "main")
	localGit(t, path, "branch", "--quiet", "-D", "orphan")
	return fixtureRepo{path: path, base: base, head: head, orphan: orphan}
}

// recorder counts Git subcommands the service runs.
type recorder struct {
	mu       sync.Mutex
	commands []string
}

func (r *recorder) add(name string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.commands = append(r.commands, name)
}

func (r *recorder) count(name string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := 0
	for _, c := range r.commands {
		if c == name {
			n++
		}
	}
	return n
}

func (r *recorder) reset() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.commands = nil
}

func localService(t *testing.T, repo fixtureRepo) (*Service, *recorder) {
	t.Helper()
	s, err := New(Config{MirrorDir: t.TempDir(), TokenSource: func(context.Context, string, string) (string, error) { return "private-test-token", nil }})
	if err != nil {
		t.Fatal(err)
	}
	previous := remoteURL
	remoteURL = func(Request) string { return "file://" + repo.path }
	t.Cleanup(func() { remoteURL = previous })
	rec := &recorder{}
	run := s.run
	s.run = func(ctx context.Context, dir string, env []string, args ...string) (string, error) {
		name := subcommand(args)
		rec.add(name)
		// Only tests substitute a local transport. Production permits HTTPS only.
		if name == "clone" || name == "fetch" {
			args = append([]string{"-c", "protocol.file.allow=always"}, args...)
		}
		return run(ctx, dir, env, args...)
	}
	return s, rec
}

func mirrorPath(s *Service) string {
	return filepath.Join(s.mirrorDir, "example", "demo.git")
}

func worktreeDir(s *Service) string {
	return filepath.Join(s.mirrorDir, "worktrees")
}

func assertEmpty(t *testing.T, dir string) {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil || len(entries) != 0 {
		t.Fatalf("%s not empty: %v, %v", dir, entries, err)
	}
}

func assertCheckout(t *testing.T, s *Service, c *Checkout, sha string) {
	t.Helper()
	if c.CommitSHA != sha || c.RepositoryID != "github.com/example/demo" {
		t.Fatalf("unexpected checkout metadata: %+v", c)
	}
	rel, err := filepath.Rel(worktreeDir(s), c.Path)
	if err != nil || !filepath.IsLocal(rel) || strings.Contains(rel, string(filepath.Separator)) {
		t.Fatalf("worktree is not a direct child of the worktree directory: %s", c.Path)
	}
	if got := localGit(t, c.Path, "rev-parse", "--verify", "HEAD^{commit}"); got != sha {
		t.Fatalf("worktree HEAD = %s, want %s", got, sha)
	}
	if got := localGit(t, c.Path, "rev-parse", "--abbrev-ref", "HEAD"); got != "HEAD" {
		t.Fatalf("worktree is not detached: %s", got)
	}
	info, err := os.Lstat(filepath.Join(c.Path, ".git"))
	if err != nil || !info.Mode().IsRegular() {
		t.Fatalf("expected a .git link file in the worktree: %v", err)
	}
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(content)
}

func TestCheckoutClonesThenReusesMirror(t *testing.T) {
	repo := fixture(t)
	s, rec := localService(t, repo)
	ctx := context.Background()
	first, err := s.Checkout(ctx, Request{Owner: "Example", Name: "Demo", CommitSHA: repo.base})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = first.Close() })
	if !mirrorExists(mirrorPath(s)) {
		t.Fatalf("mirror missing at %s", mirrorPath(s))
	}
	if rec.count("clone") != 1 || rec.count("fetch") != 0 {
		t.Fatalf("first checkout should clone once without fetching: %v", rec.commands)
	}
	assertCheckout(t, s, first, repo.base)
	if got := readFile(t, filepath.Join(first.Path, "a.txt")); got != "alpha\nbeta\ngamma\n" {
		t.Fatalf("base content = %q", got)
	}
	if _, err := os.Stat(filepath.Join(first.Path, "b.txt")); err != nil {
		t.Fatalf("base checkout lacks b.txt: %v", err)
	}
	config := readFile(t, filepath.Join(mirrorPath(s), "config"))
	if strings.Contains(config, "private-test-token") || strings.Contains(config, "Authorization") {
		t.Fatal("credentials persisted in mirror config")
	}

	// A second checkout reuses the mirror: the directory survives and only a
	// fetch runs.
	marker := filepath.Join(mirrorPath(s), "test-marker")
	if err := os.WriteFile(marker, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	rec.reset()
	second, err := s.Checkout(ctx, Request{Owner: "example", Name: "demo", CommitSHA: repo.head})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = second.Close() })
	if rec.count("clone") != 0 || rec.count("fetch") != 1 {
		t.Fatalf("second checkout should only fetch: %v", rec.commands)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("mirror directory was replaced: %v", err)
	}
	assertCheckout(t, s, second, repo.head)
	if first.Path == second.Path {
		t.Fatal("checkouts share a worktree")
	}
	if got := readFile(t, filepath.Join(second.Path, "a.txt")); got != "alpha\nBETA\ngamma\n" {
		t.Fatalf("head content = %q", got)
	}
	if _, err := os.Stat(filepath.Join(second.Path, "dir", "d.txt")); err != nil {
		t.Fatalf("head checkout lacks renamed file: %v", err)
	}
	if _, err := os.Stat(filepath.Join(second.Path, "b.txt")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("head checkout still has deleted file: %v", err)
	}

	// Close removes the worktree and its registration; it is idempotent and
	// ignores caller edits to the exported metadata.
	path := second.Path
	second.Path = repo.path
	if err := second.Close(); err != nil {
		t.Fatal(err)
	}
	if err := second.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("worktree remains: %v", err)
	}
	if _, err := os.Stat(repo.path); err != nil {
		t.Fatalf("cleanup touched unrelated repository: %v", err)
	}
	if list := localGit(t, mirrorPath(s), "worktree", "list", "--porcelain"); strings.Contains(list, path) {
		t.Fatalf("worktree still registered:\n%s", list)
	}
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	assertEmpty(t, worktreeDir(s))
}

func TestCheckoutFetchesCommitUnreachableFromRefs(t *testing.T) {
	repo := fixture(t)
	s, rec := localService(t, repo)
	c, err := s.Checkout(context.Background(), Request{Owner: "example", Name: "demo", CommitSHA: repo.orphan})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = c.Close() })
	assertCheckout(t, s, c, repo.orphan)
	if got := readFile(t, filepath.Join(c.Path, "orphan.txt")); got != "unreachable\n" {
		t.Fatalf("orphan content = %q", got)
	}
	if rec.count("clone") != 1 || rec.count("fetch") != 1 {
		t.Fatalf("expected one clone and one explicit SHA fetch: %v", rec.commands)
	}
}

func TestCheckoutMissingCommit(t *testing.T) {
	repo := fixture(t)
	s, _ := localService(t, repo)
	missing := strings.Repeat("ab", 20)
	c, err := s.Checkout(context.Background(), Request{Owner: "example", Name: "demo", CommitSHA: missing})
	if !errors.Is(err, ErrCommitNotFound) || c != nil {
		t.Fatalf("expected commit-not-found: %v, %v", c, err)
	}
	assertEmpty(t, worktreeDir(s))
	if !mirrorExists(mirrorPath(s)) {
		t.Fatal("mirror should survive a missing commit")
	}
	if _, err := s.Diff(context.Background(), Request{Owner: "example", Name: "demo", CommitSHA: repo.head}, missing, repo.head); !errors.Is(err, ErrCommitNotFound) {
		t.Fatalf("expected commit-not-found from diff: %v", err)
	}
}

func sortChanges(changes []FileChange) {
	slices.SortFunc(changes, func(a, b FileChange) int { return strings.Compare(a.Path, b.Path) })
}

func TestDiff(t *testing.T) {
	repo := fixture(t)
	s, rec := localService(t, repo)
	ctx := context.Background()
	request := Request{Owner: "example", Name: "demo", CommitSHA: repo.head}
	forward, err := s.Diff(ctx, request, repo.base, repo.head)
	if err != nil {
		t.Fatal(err)
	}
	sortChanges(forward)
	want := []FileChange{
		{Path: "a.txt", Status: StatusModified},
		{Path: "b.txt", Status: StatusDeleted},
		{Path: "dir/d.txt", OldPath: "dir/c.txt", Status: StatusRenamed},
		{Path: "new.txt", Status: StatusAdded},
	}
	if !slices.Equal(forward, want) {
		t.Fatalf("forward diff = %+v, want %+v", forward, want)
	}
	if rec.count("clone") != 1 {
		t.Fatalf("diff without a prior checkout should clone the mirror: %v", rec.commands)
	}
	backward, err := s.Diff(ctx, request, repo.head, repo.base)
	if err != nil {
		t.Fatal(err)
	}
	sortChanges(backward)
	want = []FileChange{
		{Path: "a.txt", Status: StatusModified},
		{Path: "b.txt", Status: StatusAdded},
		{Path: "dir/c.txt", OldPath: "dir/d.txt", Status: StatusRenamed},
		{Path: "new.txt", Status: StatusDeleted},
	}
	if !slices.Equal(backward, want) {
		t.Fatalf("backward diff = %+v, want %+v", backward, want)
	}
	same, err := s.Diff(ctx, request, repo.head, repo.head)
	if err != nil || len(same) != 0 {
		t.Fatalf("identical commits: %+v, %v", same, err)
	}
	if _, err := s.Diff(ctx, request, "", repo.head); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("accepted empty from SHA: %v", err)
	}
	if _, err := s.Diff(ctx, request, "HEAD~1", repo.head); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("accepted revision expression: %v", err)
	}
	assertEmpty(t, worktreeDir(s))
}

func TestParseNameStatus(t *testing.T) {
	output := "A\x00added.txt\x00M\x00mod.txt\x00T\x00type.txt\x00C075\x00src.txt\x00copy.txt\x00R100\x00old.txt\x00new.txt\x00D\x00gone.txt\x00"
	got, err := parseNameStatus(output)
	if err != nil {
		t.Fatal(err)
	}
	want := []FileChange{
		{Path: "added.txt", Status: StatusAdded},
		{Path: "mod.txt", Status: StatusModified},
		{Path: "type.txt", Status: StatusModified},
		{Path: "copy.txt", Status: StatusAdded},
		{Path: "new.txt", OldPath: "old.txt", Status: StatusRenamed},
		{Path: "gone.txt", Status: StatusDeleted},
	}
	if !slices.Equal(got, want) {
		t.Fatalf("parsed %+v, want %+v", got, want)
	}
	if got, err := parseNameStatus(""); err != nil || len(got) != 0 {
		t.Fatalf("empty output: %+v, %v", got, err)
	}
	for _, bad := range []string{"U\x00conflict.txt\x00", "X\x00unknown\x00", "R100\x00old.txt\x00", "A\x00", "\x00path\x00", "M\x00\x00"} {
		if _, err := parseNameStatus(bad); err == nil {
			t.Errorf("accepted %q", bad)
		}
	}
}

func TestConcurrentCheckoutsShareMirror(t *testing.T) {
	repo := fixture(t)
	s, rec := localService(t, repo)
	const workers = 3
	var wg sync.WaitGroup
	results := make(chan *Checkout, workers)
	for range workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c, err := s.Checkout(context.Background(), Request{Owner: "example", Name: "demo", CommitSHA: repo.head})
			if err != nil {
				t.Error(err)
				return
			}
			results <- c
		}()
	}
	wg.Wait()
	close(results)
	paths := make(map[string]bool)
	for c := range results {
		if paths[c.Path] {
			t.Error("shared worktree")
		}
		paths[c.Path] = true
		assertCheckout(t, s, c, repo.head)
		if err := c.Close(); err != nil {
			t.Error(err)
		}
	}
	if len(paths) != workers {
		t.Fatalf("got %d checkouts", len(paths))
	}
	if rec.count("clone") != 1 {
		t.Fatalf("mirror should be cloned exactly once: %v", rec.commands)
	}
	assertEmpty(t, worktreeDir(s))
}

func TestCheckoutFailureAndCancellation(t *testing.T) {
	repo := fixture(t)
	s, _ := localService(t, repo)
	request := Request{Owner: "owner", Name: "repo", CommitSHA: repo.head}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := s.Checkout(ctx, request); !errors.Is(err, context.Canceled) {
		t.Fatalf("expected cancellation: %v", err)
	}
	s.run = func(ctx context.Context, dir string, env []string, args ...string) (string, error) {
		<-ctx.Done()
		return "", ctx.Err()
	}
	s.timeout = 20 * time.Millisecond
	if _, err := s.Checkout(context.Background(), request); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected timeout: %v", err)
	}
	// The aborted clone leaves only the lock file behind.
	entries, err := os.ReadDir(filepath.Join(s.mirrorDir, "owner"))
	if err != nil || len(entries) != 1 || entries[0].Name() != "repo.git.lock" {
		t.Fatalf("partial mirror left behind: %v, %v", entries, err)
	}
	assertEmpty(t, worktreeDir(s))
}

func TestWorktreeAddFailureRemovesDirectory(t *testing.T) {
	repo := fixture(t)
	s, _ := localService(t, repo)
	run := s.run
	s.run = func(ctx context.Context, dir string, env []string, args ...string) (string, error) {
		if subcommand(args) == "worktree" && args[1] == "add" {
			return "", errors.New("simulated worktree failure")
		}
		return run(ctx, dir, env, args...)
	}
	if _, err := s.Checkout(context.Background(), Request{Owner: "example", Name: "demo", CommitSHA: repo.head}); err == nil {
		t.Fatal("expected worktree failure")
	}
	assertEmpty(t, worktreeDir(s))
}

func TestPruneRepairsAbandonedWorktrees(t *testing.T) {
	repo := fixture(t)
	s, _ := localService(t, repo)
	ctx := context.Background()
	if err := s.Prune(ctx); err != nil {
		t.Fatalf("prune without mirrors: %v", err)
	}
	c, err := s.Checkout(ctx, Request{Owner: "example", Name: "demo", CommitSHA: repo.head})
	if err != nil {
		t.Fatal(err)
	}
	// Simulate a crashed worker that never closed its checkout.
	if err := os.RemoveAll(c.Path); err != nil {
		t.Fatal(err)
	}
	if list := localGit(t, mirrorPath(s), "worktree", "list", "--porcelain"); !strings.Contains(list, c.Path) {
		t.Fatalf("expected stale registration before prune:\n%s", list)
	}
	if err := s.Prune(ctx); err != nil {
		t.Fatal(err)
	}
	if list := localGit(t, mirrorPath(s), "worktree", "list", "--porcelain"); strings.Contains(list, c.Path) {
		t.Fatalf("stale registration survived prune:\n%s", list)
	}
	if err := c.Close(); err != nil {
		t.Fatalf("close after external removal: %v", err)
	}
}

func TestValidation(t *testing.T) {
	sha := strings.Repeat("a", 40)
	for _, r := range []Request{
		{Owner: "../owner", Name: "repo", CommitSHA: sha}, {Owner: "owner", Name: "..", CommitSHA: sha},
		{Owner: "owner", Name: "repo.git/else", CommitSHA: sha}, {Owner: "owner", Name: "repo"},
		{Owner: "owner", Name: "repo", CommitSHA: "--upload-pack=bad"}, {Owner: "owner", Name: "repo", CommitSHA: strings.ToUpper(sha)},
		{Owner: "owner", Name: "repo", CommitSHA: "refs/heads/main"}, {Owner: "owner", Name: "repo", CommitSHA: sha[:39]},
		{Owner: "owner", Name: "repo", CommitSHA: sha + "\n"},
	} {
		if err := r.validate(); !errors.Is(err, ErrInvalidArgument) {
			t.Errorf("accepted invalid request: %+v", r)
		}
	}
	s, rec := localService(t, fixtureRepo{path: t.TempDir()})
	if _, err := s.Checkout(context.Background(), Request{Owner: "owner", Name: "repo", CommitSHA: "main"}); !errors.Is(err, ErrInvalidArgument) || len(rec.commands) != 0 {
		t.Fatalf("invalid request reached git: %v, %v", err, rec.commands)
	}
	if _, err := New(Config{}); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("accepted empty mirror directory: %v", err)
	}
	if _, err := New(Config{MirrorDir: filepath.Join(t.TempDir(), "unmounted")}); err == nil {
		t.Fatal("created a missing mirror directory")
	}
	file := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(file, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := New(Config{MirrorDir: file}); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("accepted a file as mirror directory: %v", err)
	}
}

func TestGitEnvironment(t *testing.T) {
	t.Setenv("GIT_CONFIG_COUNT", "99")
	t.Setenv("GIT_TRACE", "1")
	t.Setenv("GIT_DIR", "/unrelated")
	env := strings.Join(gitEnvironment("secret-token"), "\n")
	for _, forbidden := range []string{"GIT_CONFIG_COUNT=99", "GIT_TRACE=", "GIT_DIR=", "secret-token"} {
		if strings.Contains(env, forbidden) {
			t.Errorf("unsafe inherited or plaintext value: %s", forbidden)
		}
	}
	for _, required := range []string{"http.https://github.com/.extraHeader", "Authorization: Basic ", "GIT_LFS_SKIP_SMUDGE=1", "protocol.allow", "credential.helper"} {
		if !strings.Contains(env, required) {
			t.Errorf("missing %s", required)
		}
	}
}

// A failed Git command says why, without credentials, and is retried only
// when the cause is transient.
func TestGitErrorsAreClassifiedAndMasked(t *testing.T) {
	cases := []struct {
		stderr    string
		retryable bool
	}{
		{"fatal: unable to access 'https://github.com/o/r/': Could not resolve host: github.com", true},
		{"error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly\nfatal: early EOF", true},
		{"remote: Internal Server Error\nfatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 500", true},
		{"fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 429", true},
		{"error: cannot lock ref 'refs/heads/main': Unable to create '/m/refs/heads/main.lock': File exists.", true},
		{"fatal: write error: No space left on device", true},
		{"remote: Repository not found.\nfatal: repository 'https://github.com/o/r/' not found", false},
		{"fatal: Authentication failed for 'https://x-access-token:ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/o/r/'", false},
	}
	for _, c := range cases {
		e := newGitError("fetch", errors.New("exit status 128"), c.stderr)
		if e.Retryable() != c.retryable {
			t.Errorf("%q: retryable=%v", c.stderr, e.Retryable())
		}
		if strings.Contains(e.Error(), "ghp_") || strings.Contains(e.Error(), "x-access-token:") {
			t.Errorf("credential leaked: %s", e.Error())
		}
	}
}

// Lock files a killed Git process left behind are removed before a fetch;
// fresh ones are left to the process writing them.
func TestStaleMirrorLocksAreRemoved(t *testing.T) {
	mirror := t.TempDir()
	stale := filepath.Join(mirror, "refs", "heads", "main.lock")
	fresh := filepath.Join(mirror, "packed-refs.lock")
	for _, name := range []string{stale, fresh} {
		if err := os.MkdirAll(filepath.Dir(name), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(name, nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	old := time.Now().Add(-time.Hour)
	if err := os.Chtimes(stale, old, old); err != nil {
		t.Fatal(err)
	}
	removeStaleLocks(mirror)
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatal("stale lock kept")
	}
	if _, err := os.Stat(fresh); err != nil {
		t.Fatal("fresh lock removed")
	}
}
