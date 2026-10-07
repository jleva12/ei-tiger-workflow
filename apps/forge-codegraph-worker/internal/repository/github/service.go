// Package github prepares isolated GitHub checkouts from persistent bare
// mirrors kept on a mounted filesystem.
package github

import (
	"ei-aitiger-codegraph/worker/internal/scratch"

	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

var (
	ErrInvalidArgument = errors.New("github: invalid argument")
	ErrCommitNotFound  = errors.New("github: commit not found")
	// ErrHistoryRewritten says the requested commit neither descends from
	// nor precedes the published one: the branch was force-pushed.
	ErrHistoryRewritten = errors.New("github: the branch history was rewritten")
	// ErrBaselineGone says the published commit no longer exists in the
	// repository, so nothing can be diffed against it.
	ErrBaselineGone = errors.New("github: the published commit is gone")
)

// Config is service-owned configuration, never repository-supplied input.
type Config struct {
	// MirrorDir must be an existing, writable directory. Mirrors live at
	// <MirrorDir>/<owner>/<name>.git (owner and name lowercased) and
	// worktrees at <MirrorDir>/worktrees/<random>.
	MirrorDir string
	// TokenSource returns the token that clones and fetches of a repository
	// (owner, name) authenticate with, looked up again for each command, sent
	// as x-access-token Basic auth. The worker supplies CODEGRAPH_GITHUB_TOKEN
	// for every repository. An empty token with no error selects anonymous
	// access; nil makes every fetch anonymous.
	TokenSource func(context.Context, string, string) (string, error)
	// Timeout bounds every Git invocation and every wait for a mirror lock.
	// It defaults to ten minutes.
	Timeout time.Duration
}

// Request selects a GitHub repository and the exact commit to check out.
// CommitSHA must be the full, lowercase 40-character SHA.
type Request struct {
	Owner     string
	Name      string
	CommitSHA string
}

// FileStatus classifies one entry of a Diff.
type FileStatus string

const (
	StatusAdded    FileStatus = "added"
	StatusModified FileStatus = "modified"
	StatusDeleted  FileStatus = "deleted"
	StatusRenamed  FileStatus = "renamed"
)

// FileChange is one path that differs between two commits. Paths are
// slash-separated and relative to the repository root. OldPath is set only
// for renames.
type FileChange struct {
	Path    string
	OldPath string
	Status  FileStatus
}

// Checkout is a detached worktree at exactly CommitSHA. Treat its files as
// immutable. RepositoryID is "github.com/<owner>/<name>" in lowercase. The
// worktree contains a .git link file; skip it and do not follow repository
// symlinks when discovering source files.
type Checkout struct {
	Path         string
	RepositoryID string
	CommitSHA    string

	service *Service
	mirror  string
	path    string
	mu      sync.Mutex
}

// Close removes this worktree and its registration in the mirror. It is safe
// to call repeatedly, but callers must finish reading the worktree first.
func (c *Checkout) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.path == "" {
		return nil
	}
	if err := c.service.removeWorktree(c.mirror, c.path); err != nil {
		return fmt.Errorf("github: release checkout: %w", err)
	}
	c.path = ""
	return nil
}

type repositoryKey struct{}

type gitRunner func(ctx context.Context, dir string, env []string, args ...string) (string, error)

// remoteURL builds the clone URL for a repository. Tests substitute a local
// transport; production only ever talks HTTPS to github.com.
var remoteURL = func(r Request) string {
	if localRemote != nil {
		return localRemote(r)
	}
	return "https://github.com/" + r.Owner + "/" + r.Name + ".git"
}

// localRemote, when set, replaces GitHub with a local repository and allows
// git's file transport. It exists for integration tests of other packages.
var localRemote func(Request) string

// SetRemoteURL points every repository at the location fn returns (a local
// bare repository path or file:// URL) and permits git's file transport.
// Passing nil restores GitHub over HTTPS. Test use only.
func SetRemoteURL(fn func(Request) string) { localRemote = fn }

// Service keeps one bare mirror per repository and hands out a fresh detached
// worktree per Checkout. It is safe for concurrent use; calls for the same
// repository serialize their mirror updates. Construct with New; the zero
// value is not usable.
type Service struct {
	mirrorDir   string
	env         []string
	tokenSource func(context.Context, string, string) (string, error)
	timeout     time.Duration
	run         gitRunner

	mu    sync.Mutex
	locks map[string]chan struct{}
}

func New(config Config) (*Service, error) {
	if strings.TrimSpace(config.MirrorDir) == "" || config.Timeout < 0 {
		return nil, fmt.Errorf("%w: mirror directory or timeout", ErrInvalidArgument)
	}
	root, err := filepath.Abs(config.MirrorDir)
	if err != nil {
		return nil, fmt.Errorf("github: resolve mirror directory: %w", err)
	}
	root, err = filepath.EvalSymlinks(root)
	if err != nil {
		return nil, fmt.Errorf("github: resolve mirror directory: %w", err)
	}
	info, err := os.Stat(root)
	if err != nil {
		return nil, fmt.Errorf("github: inspect mirror directory: %w", err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("%w: mirror directory must be a directory", ErrInvalidArgument)
	}
	// Worktrees a killed process never removed; Prune drops their
	// registrations.
	scratch.Sweep(filepath.Join(root, "worktrees"), "wt-", scratch.Stale)
	// Creating the worktree parent up front proves the directory is writable.
	if err := os.MkdirAll(filepath.Join(root, "worktrees"), 0o755); err != nil {
		return nil, fmt.Errorf("github: prepare worktree directory: %w", err)
	}
	git, err := exec.LookPath("git")
	if err != nil {
		return nil, fmt.Errorf("github: git executable required: %w", err)
	}
	if config.Timeout == 0 {
		config.Timeout = 10 * time.Minute
	}
	return &Service{
		mirrorDir:   root,
		tokenSource: config.TokenSource,
		env:         gitEnvironment(""),
		timeout:     config.Timeout,
		run:         commandRunner(git),
		locks:       make(map[string]chan struct{}),
	}, nil
}

var ownerPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`)
var namePattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}$`)
var shaPattern = regexp.MustCompile(`^[0-9a-f]{40}$`)

// validate rejects anything that is not a plain GitHub owner, repository
// name, and full lowercase commit SHA. The SHA pattern also excludes options,
// refspecs, and revision expressions.
func (r Request) validate() error {
	if !ownerPattern.MatchString(r.Owner) || !namePattern.MatchString(r.Name) || r.Name == "." || r.Name == ".." {
		return fmt.Errorf("%w: expected GitHub owner and repository name", ErrInvalidArgument)
	}
	if !shaPattern.MatchString(r.CommitSHA) {
		return fmt.Errorf("%w: expected a full lowercase commit SHA", ErrInvalidArgument)
	}
	return nil
}

// mirror locates one repository's persistent state on disk.
type mirror struct {
	id   string // github.com/<owner>/<name>
	key  string // <owner>/<name>, keys the in-process semaphore
	path string // <MirrorDir>/<owner>/<name>.git
	lock string // <MirrorDir>/<owner>/<name>.git.lock
}

func (s *Service) mirrorFor(r Request) mirror {
	owner, name := strings.ToLower(r.Owner), strings.ToLower(r.Name)
	dir := filepath.Join(s.mirrorDir, owner)
	return mirror{
		id:   "github.com/" + owner + "/" + name,
		key:  owner + "/" + name,
		path: filepath.Join(dir, name+".git"),
		lock: filepath.Join(dir, name+".git.lock"),
	}
}

// Checkout brings the repository mirror up to date, makes sure the requested
// commit is present, and creates a detached worktree at that commit. Failed
// or canceled calls remove any partially created worktree. Successful
// callers own Close. Submodules and Git LFS content are never initialized.
func (s *Service) Checkout(ctx context.Context, request Request) (_ *Checkout, err error) {
	if err := request.validate(); err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	ctx = context.WithValue(ctx, repositoryKey{}, request)
	m := s.mirrorFor(request)
	release, err := s.lock(ctx, m)
	if err != nil {
		return nil, err
	}
	defer release()
	if err := s.syncMirror(ctx, m, request, true); err != nil {
		return nil, err
	}
	if err := s.ensureCommit(ctx, m, request.CommitSHA); err != nil {
		return nil, err
	}
	path, err := os.MkdirTemp(filepath.Join(s.mirrorDir, "worktrees"), "wt-")
	if err != nil {
		return nil, fmt.Errorf("github: allocate worktree: %w", err)
	}
	defer func() {
		if err != nil {
			err = errors.Join(err, s.removeWorktree(m.path, path))
		}
	}()
	if _, err = s.git(ctx, m.path, "worktree", "add", "--detach", "--quiet", path, request.CommitSHA); err != nil {
		return nil, fmt.Errorf("github: add worktree: %w", err)
	}
	head, err := s.git(ctx, path, "rev-parse", "--verify", "HEAD^{commit}")
	if err != nil {
		return nil, fmt.Errorf("github: resolve worktree HEAD: %w", err)
	}
	if strings.TrimSpace(head) != request.CommitSHA {
		return nil, errors.New("github: worktree HEAD does not match requested commit")
	}
	return &Checkout{Path: path, RepositoryID: m.id, CommitSHA: request.CommitSHA, service: s, mirror: m.path, path: path}, nil
}

// Diff lists the files that differ between fromSHA and toSHA using the
// repository mirror. Renames are detected at 90% similarity and reported with
// OldPath; copies count as additions and type changes as modifications.
// Callers handle the initial run themselves, so an empty fromSHA is invalid.
func (s *Service) Diff(ctx context.Context, request Request, fromSHA, toSHA string) ([]FileChange, error) {
	if err := request.validate(); err != nil {
		return nil, err
	}
	if !shaPattern.MatchString(fromSHA) || !shaPattern.MatchString(toSHA) {
		return nil, fmt.Errorf("%w: expected full lowercase commit SHAs to diff", ErrInvalidArgument)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	ctx = context.WithValue(ctx, repositoryKey{}, request)
	m := s.mirrorFor(request)
	release, err := s.lock(ctx, m)
	if err != nil {
		return nil, err
	}
	defer release()
	if err := s.syncMirror(ctx, m, request, false); err != nil {
		return nil, err
	}
	for _, sha := range []string{fromSHA, toSHA} {
		if err := s.ensureCommit(ctx, m, sha); err != nil {
			return nil, err
		}
	}
	output, err := s.git(ctx, m.path, "diff", "--name-status", "--find-renames=90%", "-z", fromSHA, toSHA, "--")
	if err != nil {
		return nil, fmt.Errorf("github: diff commits: %w", err)
	}
	changes, err := parseNameStatus(output)
	if err != nil {
		return nil, fmt.Errorf("github: diff commits: %w", err)
	}
	return changes, nil
}

// Prune runs `git worktree prune` on every mirror so registrations left by
// crashed processes do not accumulate. Worktree metadata is protected by
// Git's own locking, so Prune does not take mirror locks and never waits
// behind another worker's clone. Leftover worktree directories are not
// deleted here because another process sharing the volume may own them.
func (s *Service) Prune(ctx context.Context) error {
	owners, err := os.ReadDir(s.mirrorDir)
	if err != nil {
		return fmt.Errorf("github: list mirrors: %w", err)
	}
	var errs []error
	for _, owner := range owners {
		if !owner.IsDir() {
			continue
		}
		repos, err := os.ReadDir(filepath.Join(s.mirrorDir, owner.Name()))
		if err != nil {
			errs = append(errs, err)
			continue
		}
		for _, repo := range repos {
			path := filepath.Join(s.mirrorDir, owner.Name(), repo.Name())
			if !repo.IsDir() || !strings.HasSuffix(repo.Name(), ".git") || !mirrorExists(path) {
				continue
			}
			if err := ctx.Err(); err != nil {
				return err
			}
			if _, err := s.git(ctx, path, "worktree", "prune"); err != nil {
				errs = append(errs, fmt.Errorf("%s: %w", path, err))
			}
		}
	}
	if len(errs) > 0 {
		return fmt.Errorf("github: prune worktrees: %w", errors.Join(errs...))
	}
	return nil
}

// lock serializes mirror mutations. An in-process semaphore keyed by
// repository orders goroutines and keeps the wait cancellable; an advisory
// lock file beside the mirror excludes other worker processes sharing the
// volume. Both waits are bounded by ctx and the configured timeout.
func (s *Service) lock(ctx context.Context, m mirror) (release func(), err error) {
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	sem := s.semaphore(m.key)
	select {
	case sem <- struct{}{}:
	case <-ctx.Done():
		return nil, fmt.Errorf("github: wait for mirror lock: %w", ctx.Err())
	}
	if err := os.MkdirAll(filepath.Dir(m.path), 0o755); err != nil {
		<-sem
		return nil, fmt.Errorf("github: create mirror directory: %w", err)
	}
	unlock, err := lockFile(ctx, m.lock)
	if err != nil {
		<-sem
		return nil, fmt.Errorf("github: lock mirror: %w", err)
	}
	return func() {
		unlock()
		<-sem
	}, nil
}

func (s *Service) semaphore(key string) chan struct{} {
	s.mu.Lock()
	defer s.mu.Unlock()
	sem, ok := s.locks[key]
	if !ok {
		sem = make(chan struct{}, 1)
		s.locks[key] = sem
	}
	return sem
}

// mirrorExists reports whether path holds a bare repository. It never
// follows symlinks.
func mirrorExists(path string) bool {
	info, err := os.Lstat(path)
	if err != nil || !info.IsDir() {
		return false
	}
	head, err := os.Lstat(filepath.Join(path, "HEAD"))
	return err == nil && head.Mode().IsRegular()
}

// syncMirror clones the mirror on first use and otherwise fetches when asked.
// Callers hold the mirror lock.
func (s *Service) syncMirror(ctx context.Context, m mirror, request Request, fetch bool) error {
	if !mirrorExists(m.path) {
		return s.cloneMirror(ctx, m, request)
	}
	if !fetch {
		return nil
	}
	removeStaleLocks(m.path)
	if _, err := s.git(ctx, m.path, "fetch", "--quiet", "--no-tags", "--prune", "origin"); err != nil {
		return fmt.Errorf("github: fetch mirror: %w", err)
	}
	return nil
}

// removeStaleLocks deletes the lock files a Git process killed on timeout
// leaves in a mirror (packed-refs.lock, refs/.../*.lock), which would fail
// every later fetch. The caller holds the mirror lock, so no Git process of
// any worker works in the mirror; a lock a minute old is not being written.
func removeStaleLocks(mirror string) {
	cutoff := time.Now().Add(-time.Minute)
	_ = filepath.WalkDir(mirror, func(name string, entry os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if entry.IsDir() {
			// Object and pack directories hold no ref locks.
			if entry.Name() == "objects" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(entry.Name(), ".lock") || entry.Type()&os.ModeSymlink != 0 {
			return nil
		}
		if info, err := entry.Info(); err == nil && info.ModTime().Before(cutoff) {
			_ = os.Remove(name)
		}
		return nil
	})
}

// cloneMirror clones into a temporary sibling and renames it into place so a
// crash never leaves a half-populated directory at the mirror path.
func (s *Service) cloneMirror(ctx context.Context, m mirror, request Request) (err error) {
	parent := filepath.Dir(m.path)
	tmp, err := os.MkdirTemp(parent, ".clone-")
	if err != nil {
		return fmt.Errorf("github: allocate mirror: %w", err)
	}
	defer func() {
		if err != nil {
			err = errors.Join(err, os.RemoveAll(tmp))
		}
	}()
	if _, err = s.git(ctx, parent, "clone", "--mirror", "--quiet", "--no-tags", "--template=", "--", remoteURL(request), tmp); err != nil {
		return fmt.Errorf("github: clone mirror: %w", err)
	}
	// Anything at the final path is not a usable mirror (mirrorExists was
	// false), so replace it.
	if err = os.RemoveAll(m.path); err != nil {
		return fmt.Errorf("github: replace stale mirror: %w", err)
	}
	if err = os.Rename(tmp, m.path); err != nil {
		return fmt.Errorf("github: publish mirror: %w", err)
	}
	return nil
}

// ensureCommit verifies the commit is in the mirror. A commit that no ref
// reaches (force-pushed away, or a pull request head) is fetched explicitly.
func (s *Service) ensureCommit(ctx context.Context, m mirror, sha string) error {
	if s.hasCommit(ctx, m, sha) {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	_, fetchErr := s.git(ctx, m.path, "fetch", "--quiet", "--no-tags", "origin", sha)
	if s.hasCommit(ctx, m, sha) {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if fetchErr != nil {
		return fmt.Errorf("%w: %s (%w)", ErrCommitNotFound, sha, fetchErr)
	}
	return fmt.Errorf("%w: %s", ErrCommitNotFound, sha)
}

func (s *Service) hasCommit(ctx context.Context, m mirror, sha string) bool {
	_, err := s.git(ctx, m.path, "cat-file", "-e", sha+"^{commit}")
	return err == nil
}

// removeWorktree unregisters a worktree from its mirror and deletes it. The
// registration is best effort (Prune repairs stale entries); the directory
// must go. RemoveAll does not follow symlinks.
func (s *Service) removeWorktree(mirrorPath, path string) error {
	_, _ = s.git(context.Background(), mirrorPath, "worktree", "remove", "--force", path)
	if err := os.RemoveAll(path); err != nil {
		return fmt.Errorf("github: remove worktree: %w", err)
	}
	return nil
}

// git runs one Git command with the hardened environment and the configured
// per-invocation timeout.
func (s *Service) git(ctx context.Context, dir string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	env := s.env
	if s.tokenSource != nil && (subcommand(args) == "clone" || subcommand(args) == "fetch" || subcommand(args) == "ls-remote") {
		repo, ok := ctx.Value(repositoryKey{}).(Request)
		if !ok {
			return "", errors.New("github: missing repository for authentication")
		}
		token, err := s.tokenSource(ctx, repo.Owner, repo.Name)
		if err != nil {
			return "", fmt.Errorf("github: repository authentication: %w", err)
		}
		if strings.ContainsAny(token, "\x00\r\n") {
			return "", errors.New("github: invalid repository credential")
		}
		env = gitEnvironment(token)
	}
	return s.run(ctx, dir, env, args...)
}

// parseNameStatus decodes `git diff --name-status -z` output: a status field
// followed by one path, or two paths for renames and copies, each NUL-ended.
func parseNameStatus(output string) ([]FileChange, error) {
	fields := strings.Split(output, "\x00")
	if n := len(fields); n > 0 && fields[n-1] == "" {
		fields = fields[:n-1]
	}
	changes := make([]FileChange, 0, len(fields)/2)
	for i := 0; i < len(fields); {
		status := fields[i]
		if status == "" {
			return nil, errors.New("empty status field")
		}
		paths := 1
		var change FileChange
		switch status[0] {
		case 'A':
			change.Status = StatusAdded
		case 'M', 'T':
			change.Status = StatusModified
		case 'D':
			change.Status = StatusDeleted
		case 'R':
			change.Status, paths = StatusRenamed, 2
		case 'C':
			change.Status, paths = StatusAdded, 2
		default:
			return nil, fmt.Errorf("unsupported diff status %q", status)
		}
		if i+paths >= len(fields) {
			return nil, fmt.Errorf("truncated diff entry for status %q", status)
		}
		switch paths {
		case 1:
			change.Path = fields[i+1]
		case 2:
			change.Path = fields[i+2]
			if change.Status == StatusRenamed {
				change.OldPath = fields[i+1]
			}
		}
		if change.Path == "" {
			return nil, fmt.Errorf("empty path for status %q", status)
		}
		changes = append(changes, change)
		i += 1 + paths
	}
	return changes, nil
}

// gitEnvironment prepares a safe Git environment with hardened configuration and optional authentication.
func gitEnvironment(token string) []string {
	var env []string
	for _, entry := range os.Environ() {
		if !strings.HasPrefix(entry, "GIT_") && !strings.HasPrefix(entry, "GITHUB_TOKEN=") && !strings.HasPrefix(entry, "CODEGRAPH_TOKEN=") && !strings.Contains(strings.SplitN(entry, "=", 2)[0], "CLIENT_SECRET") {
			env = append(env, entry)
		}
	}
	env = append(env, "GIT_TERMINAL_PROMPT=0", "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL="+os.DevNull, "GIT_LFS_SKIP_SMUDGE=1")
	config := [][2]string{
		{"credential.helper", ""}, {"credential.interactive", "false"},
		{"core.hooksPath", os.DevNull}, {"core.autocrlf", "false"},
		{"protocol.allow", "never"}, {"protocol.https.allow", "always"},
		// GitHub redirects a renamed or transferred repository; the
		// credential header is scoped to github.com wherever it leads.
		{"http.followRedirects", "initial"},
		{"fetch.recurseSubmodules", "false"}, {"submodule.recurse", "false"},
	}
	if token != "" {
		auth := base64.StdEncoding.EncodeToString([]byte("x-access-token:" + token))
		config = append(config, [2]string{"http.https://github.com/.extraHeader", "Authorization: Basic " + auth})
	}
	env = append(env, fmt.Sprintf("GIT_CONFIG_COUNT=%d", len(config)))
	for i, pair := range config {
		env = append(env, fmt.Sprintf("GIT_CONFIG_KEY_%d=%s", i, pair[0]), fmt.Sprintf("GIT_CONFIG_VALUE_%d=%s", i, pair[1]))
	}
	return env
}

// subcommand returns the Git subcommand in args, skipping leading global
// options such as "-c key=value".
func subcommand(args []string) string {
	for i := 0; i < len(args); i++ {
		switch {
		case args[i] == "-c" || args[i] == "-C":
			i++
		case strings.HasPrefix(args[i], "-"):
		default:
			return args[i]
		}
	}
	return ""
}

// commandRunner prepares a Git command runner with a hardened environment and optional authentication.
func commandRunner(git string) gitRunner {
	return func(ctx context.Context, dir string, env []string, args ...string) (string, error) {
		if localRemote != nil {
			args = append([]string{"-c", "protocol.file.allow=always"}, args...)
		}
		cmd := exec.CommandContext(ctx, git, args...)
		cmd.Dir, cmd.Env = dir, env
		configureCancellation(cmd)
		cmd.WaitDelay = time.Second
		// Only rev-parse, diff and ls-remote produce output that is parsed;
		// ls-remote asks for one exact ref. Discard remote-controlled output
		// otherwise so it cannot grow memory or leak credentials through
		// diagnostics.
		var output strings.Builder
		if name := subcommand(args); name == "rev-parse" || name == "diff" || name == "ls-remote" {
			cmd.Stdout = &output
		}
		// The end of stderr says why a command failed; it is bounded and
		// masked before it reaches an error.
		stderr := &tailBuffer{max: 4 << 10}
		cmd.Stderr = stderr
		if err := cmd.Run(); err != nil {
			if ctx.Err() != nil {
				return "", ctx.Err()
			}
			return "", newGitError(subcommand(args), err, stderr.String())
		}
		return output.String(), nil
	}
}

// tailBuffer keeps the last max bytes written to it.
type tailBuffer struct {
	max int
	buf []byte
}

func (t *tailBuffer) Write(p []byte) (int, error) {
	t.buf = append(t.buf, p...)
	if len(t.buf) > t.max {
		t.buf = append(t.buf[:0], t.buf[len(t.buf)-t.max:]...)
	}
	return len(p), nil
}

func (t *tailBuffer) String() string { return string(t.buf) }

// gitError is a failed Git command with what it said, classified: network,
// server, rate-limit and local-resource failures are worth retrying; a
// refused credential, a missing repository or a missing ref is not.
type gitError struct {
	command   string
	err       error
	detail    string
	retryable bool
}

func (e *gitError) Error() string {
	if e.detail == "" {
		return fmt.Sprintf("git %s: %v", e.command, e.err)
	}
	return fmt.Sprintf("git %s: %v: %s", e.command, e.err, e.detail)
}

func (e *gitError) Unwrap() error   { return e.err }
func (e *gitError) Retryable() bool { return e.retryable }

var (
	// Credentials in URLs and authorization values never reach an error.
	gitURLCredentials = regexp.MustCompile(`([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@`)
	gitAuthorization  = regexp.MustCompile(`(?i)(authorization:\s*\S+\s+)\S+`)
	gitToken          = regexp.MustCompile(`\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})`)
	// What makes a failure worth another attempt later.
	gitTransient = []string{
		"could not resolve host", "connection reset", "connection refused", "connection timed out", "operation timed out",
		"failed to connect", "the remote end hung up unexpectedly", "early eof", "rpc failed", "unexpected disconnect",
		"ssl_error", "gnutls", "tls", "http/2 stream", "returned error: 5", "returned error: 429", "rate limit",
		"internal server error", "bad gateway", "service unavailable", "gateway timeout", "curl 18", "curl 28", "curl 56", "curl 92",
		"index-pack failed", "invalid index-pack output", "no space left on device", "unable to create", ".lock': file exists",
		"cannot lock ref", "temporary failure",
	}
)

func newGitError(command string, err error, stderr string) *gitError {
	detail := gitURLCredentials.ReplaceAllString(stderr, "${1}***@")
	detail = gitAuthorization.ReplaceAllString(detail, "${1}***")
	detail = gitToken.ReplaceAllString(detail, "***")
	var lines []string
	for _, line := range strings.Split(detail, "\n") {
		if line = strings.TrimSpace(line); line != "" {
			lines = append(lines, line)
		}
	}
	if len(lines) > 4 {
		lines = lines[len(lines)-4:]
	}
	detail = strings.Join(lines, "; ")
	lower := strings.ToLower(detail)
	retryable := false
	for _, pattern := range gitTransient {
		retryable = retryable || strings.Contains(lower, pattern)
	}
	return &gitError{command: command, err: err, detail: detail, retryable: retryable}
}
