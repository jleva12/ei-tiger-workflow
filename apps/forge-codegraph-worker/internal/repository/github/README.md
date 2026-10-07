# GitHub repository service

`Service` keeps one persistent bare mirror per GitHub repository under an existing filesystem directory and hands out a fresh detached worktree for every run. It uses the installed `git` executable and adds no Go dependencies. Mount the volume before constructing the service; `New` requires an existing directory so a missing mount is not silently created on the container filesystem.

```go
source, err := github.New(github.Config{
    MirrorDir:   "/mnt/codegraph/repositories",
    TokenSource: tokens,          // Per repository and network command; nil or "" is anonymous.
    Timeout:     5 * time.Minute, // Per Git invocation and per lock wait.
})
if err != nil {
    return err
}
if err := source.Prune(ctx); err != nil { // At worker start: drop registrations left by crashed workers.
    return err
}
checkout, err := source.Checkout(ctx, github.Request{
    Owner: "my-org", Name: "my-service", CommitSHA: "<40 lowercase hex>",
})
if err != nil {
    return err
}
defer checkout.Close()
changes, err := source.Diff(ctx, request, previousSHA, checkout.CommitSHA) // added / modified / deleted / renamed
```

## Layout

- `<MirrorDir>/<owner>/<name>.git` — bare mirror (`git clone --mirror`), owner and name lowercased so requests that differ only in case share it.
- `<MirrorDir>/<owner>/<name>.git.lock` — advisory lock file for that mirror.
- `<MirrorDir>/worktrees/wt-<random>` — one detached worktree per `Checkout`; `Close` removes it.

## Behavior

`Checkout` clones the mirror on first use (`git clone --mirror --no-tags`) and otherwise fetches with prune, verifies the commit exists (fetching the SHA explicitly when no ref reaches it, for example a pull request head or a force-pushed commit), creates a detached worktree with `git worktree add --detach`, and confirms the worktree HEAD equals the requested SHA. Any failure removes the partially created worktree; a failed clone never leaves a half-populated mirror because clones land in a temporary sibling that is renamed into place. `ErrCommitNotFound` is returned when the commit cannot be obtained. `ParseURL` converts a `https://github.com/owner/name` URL plus SHA into a `Request`.

`Diff` runs `git diff --name-status --find-renames=90% -z` in the mirror. Renames carry `OldPath`; copies are reported as additions and type changes as modifications; unmerged or unknown statuses are errors. An empty `fromSHA` is invalid: callers handle the initial run themselves.

Mirror updates for one repository are serialized by an in-process semaphore (cancellable through the context) plus a `flock` on the lock file, so two worker processes sharing one volume cannot corrupt a mirror. Different repositories proceed in parallel. `Prune` only touches worktree metadata, which Git locks itself, so it does not wait on mirror locks at worker start. On platforms without `flock`, only the in-process semaphore applies.

`TokenSource` is called with the repository's owner and name before every clone, fetch and `ls-remote`, so each command authenticates with the credential current for that repository; the worker returns `CODEGRAPH_GITHUB_TOKEN` for every repository, and an error fails the command rather than falling back to anonymous access. An empty token selects anonymous access; without `CODEGRAPH_GITHUB_TOKEN` the worker sets no `TokenSource`, so every command is anonymous and only public repositories can be read. Authentication is passed as a host-scoped HTTP header through the child process environment, never in command arguments, URLs, or saved Git config; a `GITHUB_TOKEN` or `GIT_*` variable inherited from the parent environment is dropped. Host Git configuration, credential helpers, hooks, and HTTPS redirects are disabled; only the HTTPS protocol is allowed. Git output is discarded except for `rev-parse` and `diff`, whose output is parsed. Every Git invocation is bounded by the context and the configured timeout, and cancellation kills the whole process group so transport helpers cannot keep writing.

Worktrees contain tracked files and a `.git` link file. Discovery must skip `.git` and must not follow repository symlinks. Submodules are never initialized and Git LFS files remain pointers. Mirrors persist across runs and grow with the repository; apply volume quotas and a retention policy for abandoned worktree directories outside this package (a directory left by a crashed process may belong to another worker sharing the volume, so `Prune` does not delete it).

Tests build local repositories with the real `git` binary and substitute a `file://` remote through the unexported `remoteURL` hook. They do not need GitHub credentials or network access.

`ValidateRevision(ctx, request, branch, currentCommit)` refreshes the mirror
and verifies branch membership and forward ancestry with Git before ingestion.
A branch must be literal, and an older or diverged target returns
`deployment.ErrStaleDeployment`. The ingestion worker calls it while holding
the repository lease; branch names and commit dates cannot bypass ancestry.
