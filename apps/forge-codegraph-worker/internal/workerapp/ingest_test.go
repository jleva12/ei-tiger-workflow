package workerapp

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/worker/internal/jobqueue"
	"ei-aitiger-codegraph/worker/internal/repository/github"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// fakeQueue records how the worker settles its job.
type fakeQueue struct {
	mu        sync.Mutex
	renewErr  error
	writeErr  error // returned by SetCommit and AttachRun
	failErr   error
	renewals  int
	commits   []string
	attached  []deployment.RunKey
	cleared   int
	succeeded []jobqueue.Outcome
	requeued  []requeued
	failed    []string
	events    *[]string
}

type requeued struct {
	code   string
	charge bool
	delay  time.Duration
}

func (f *fakeQueue) event(e string) {
	if f.events != nil {
		*f.events = append(*f.events, e)
	}
}

func (f *fakeQueue) Ping(context.Context) error  { return nil }
func (f *fakeQueue) Check(context.Context) error { return nil }
func (f *fakeQueue) Claim(context.Context, string, time.Duration, uint64) (jobqueue.Job, error) {
	return jobqueue.Job{}, jobqueue.ErrNoJob
}
func (f *fakeQueue) Renew(context.Context, jobqueue.Job, time.Duration) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.renewals++
	return f.renewErr
}
func (f *fakeQueue) SetCommit(_ context.Context, _ jobqueue.Job, sha string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.event("set commit")
	f.commits = append(f.commits, sha)
	return f.writeErr
}
func (f *fakeQueue) AttachRun(_ context.Context, _ jobqueue.Job, key deployment.RunKey) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.event("attach run")
	f.attached = append(f.attached, key)
	return f.writeErr
}
func (f *fakeQueue) ClearRun(context.Context, jobqueue.Job) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cleared++
	return nil
}
func (f *fakeQueue) Succeed(_ context.Context, _ jobqueue.Job, out jobqueue.Outcome) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.succeeded = append(f.succeeded, out)
	return nil
}
func (f *fakeQueue) Requeue(_ context.Context, _ jobqueue.Job, delay time.Duration, charge bool, code, _ string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requeued = append(f.requeued, requeued{code, charge, delay})
	return nil
}
func (f *fakeQueue) Fail(_ context.Context, _ jobqueue.Job, code, _ string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.event("fail job")
	f.failed = append(f.failed, code)
	return f.failErr
}

// settled is how many times the job was settled, whichever way.
func (f *fakeQueue) settled() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.succeeded) + len(f.requeued) + len(f.failed)
}

type fakeRunner struct {
	err   error
	block bool
	phase deployment.Phase
}

func (r fakeRunner) Run(ctx context.Context, key deployment.RunKey) (deployment.Run, error) {
	if r.block {
		<-ctx.Done()
		return deployment.Run{}, ctx.Err()
	}
	phase := r.phase
	if phase == "" {
		phase = deployment.Succeeded
	}
	return deployment.Run{Key: key, Phase: phase, Generation: 2, Metrics: &deployment.Metrics{Files: 3}, WarningMessage: "a module did not compile"}, r.err
}

func testConfig() Config {
	c := DefaultConfig()
	c.Timeout = 5 * time.Second
	c.CleanupTimeout = time.Second
	c.MaxAttempts = 6
	return c
}

func testJob() jobqueue.Job {
	return jobqueue.Job{ID: "job-1", RepositoryID: "r-1", OrganizationID: "org-1", URL: "https://github.com/acme/widgets",
		Branch: "main", RequestedBy: "user-1", Token: 1, Attempts: 1}
}

type world struct {
	store    *fakeStore
	queue    *fakeQueue
	branches *fakeBranches
	w        *ingester
	events   []string
}

func newWorld(runner runner) *world {
	wd := &world{store: newFakeStore(), queue: &fakeQueue{}, branches: &fakeBranches{}}
	wd.store.events, wd.queue.events = &wd.events, &wd.events
	wd.w = &ingester{c: testConfig(), logger: slog.New(slog.DiscardHandler), queue: wd.queue, runs: wd.store,
		branches: wd.branches, pipeline: runner, digest: testDigest}
	return wd
}

func (wd *world) process(ctx context.Context, job jobqueue.Job) {
	wd.w.process(ctx, job, time.Now().Add(jobLease))
}

func TestAJobIsAdmittedRunAndSucceeds(t *testing.T) {
	wd := newWorld(fakeRunner{})
	wd.process(context.Background(), testJob())
	// The branch head is read and recorded before the run is admitted, so a
	// retry admits the same commit.
	if len(wd.branches.as) != 1 || wd.branches.as[0] != "acme/widgets" || len(wd.queue.commits) != 1 || wd.queue.commits[0] != headSHA {
		t.Fatalf("branch read %v, commits %v", wd.branches.as, wd.queue.commits)
	}
	if strings.Join(wd.events, ",") != "set commit,admit,attach run" {
		t.Fatalf("order %v", wd.events)
	}
	locator, _ := deployment.ParseGitHubURL("https://github.com/acme/widgets")
	repo := wd.store.repos[locator.RepositoryID()]
	if repo.GitHubURL != locator.CanonicalURL() || repo.IntegrationID != forgeIntegration || wd.store.puts != 1 {
		t.Fatalf("registration %+v", repo)
	}
	got := wd.store.admitted[0]
	if got.SubmissionID != "forge-job-job-1" || got.Request.TargetCommitSHA != headSHA || got.Request.RequestedBy != "user-1" ||
		got.Request.AnalysisConfigDigest != testDigest || got.Request.Branch != "main" || got.Request.RepositoryID != repo.RepositoryID {
		t.Fatalf("admitted %+v", got)
	}
	if len(wd.queue.succeeded) != 1 || wd.queue.settled() != 1 {
		t.Fatalf("settlement %+v", wd.queue)
	}
	out := wd.queue.succeeded[0]
	metrics := out.Metrics.(jobMetrics)
	if out.Status != jobqueue.Succeeded || out.Generation != 2 || out.Run != wd.queue.attached[0] || metrics.Files != 3 || metrics.Warning == "" {
		t.Fatalf("outcome %+v", out)
	}
}

func TestAPinnedCommitIsAdmittedAsGiven(t *testing.T) {
	wd := newWorld(fakeRunner{})
	job := testJob()
	job.CommitSHA = pinnedSHA
	wd.process(context.Background(), job)
	if len(wd.branches.as) != 0 || len(wd.queue.commits) != 0 || wd.store.admitted[0].Request.TargetCommitSHA != pinnedSHA {
		t.Fatalf("pinned commit: read %v, commits %v, admitted %+v", wd.branches.as, wd.queue.commits, wd.store.admitted)
	}
}

func TestARunAdmittedBeforeACrashIsAdopted(t *testing.T) {
	wd := newWorld(fakeRunner{})
	key := deployment.RunKey{RepositoryID: "repo:x", RunID: "run-earlier"}
	wd.store.submissions["forge-job-job-1"] = key
	wd.store.runs[key] = deployment.Run{Key: key, Phase: deployment.Accepted}
	job := testJob()
	job.CommitSHA = headSHA
	wd.process(context.Background(), job)
	if len(wd.store.admitted) != 0 || len(wd.queue.attached) != 1 || wd.queue.attached[0] != key || wd.queue.succeeded[0].Run != key {
		t.Fatalf("adoption: admitted %+v, attached %v, outcome %+v", wd.store.admitted, wd.queue.attached, wd.queue.succeeded)
	}
}

func TestAGraphOnAnotherBranchFailsTheJob(t *testing.T) {
	wd := newWorld(fakeRunner{})
	locator, _ := deployment.ParseGitHubURL("https://github.com/acme/widgets")
	wd.store.repos[locator.RepositoryID()] = deployment.Repository{RepositoryID: locator.RepositoryID(), GitHubURL: locator.CanonicalURL(), Branch: "develop"}
	wd.process(context.Background(), testJob())
	if len(wd.store.admitted) != 0 || len(wd.queue.failed) != 1 || wd.queue.failed[0] != "branch_conflict" {
		t.Fatalf("branch conflict: admitted %+v, failed %v", wd.store.admitted, wd.queue.failed)
	}
}

func TestAccessFailuresAreNamed(t *testing.T) {
	exitErr := exec.Command("sh", "-c", "exit 128").Run()
	for want, err := range map[string]error{
		"branch_not_found":      fmt.Errorf("%w: main", github.ErrBranchNotFound),
		"repository_unreadable": fmt.Errorf("github: read acme/widgets: %w", exitErr),
	} {
		wd := newWorld(fakeRunner{})
		wd.branches.err = err
		wd.process(context.Background(), testJob())
		if len(wd.queue.failed) != 1 || wd.queue.failed[0] != want || len(wd.store.admitted) != 0 || len(wd.store.failedRuns) != 0 {
			t.Errorf("%s: failed %v, admitted %d", want, wd.queue.failed, len(wd.store.admitted))
		}
	}
}

func TestAnInvalidURLFailsTheJob(t *testing.T) {
	wd := newWorld(fakeRunner{})
	job := testJob()
	job.URL = "https://gitlab.com/acme/widgets"
	wd.process(context.Background(), job)
	if len(wd.queue.failed) != 1 || wd.queue.failed[0] != "invalid_repository" {
		t.Fatalf("failed %v", wd.queue.failed)
	}
}

func TestALostJobIsNotSettled(t *testing.T) {
	// Removed, or claimed by another worker, while being admitted.
	wd := newWorld(fakeRunner{})
	wd.queue.writeErr = fmt.Errorf("%w: job no longer ours", deployment.ErrFenceLost)
	wd.process(context.Background(), testJob())
	if wd.queue.settled() != 0 || len(wd.store.admitted) != 0 || len(wd.store.failedRuns) != 0 {
		t.Fatalf("lost job settled: %+v", wd.queue)
	}
	// Lost while running: the renewal finds out and stops the run.
	wd = newWorld(fakeRunner{block: true})
	wd.queue.renewErr = deployment.ErrFenceLost
	wd.w.renewEvery = 20 * time.Millisecond
	job := testJob()
	done := make(chan struct{})
	go func() {
		defer close(done)
		wd.w.process(context.Background(), job, time.Now().Add(jobLease))
	}()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("a lost run kept running")
	}
	if wd.queue.settled() != 0 || len(wd.store.failedRuns) != 0 {
		t.Fatalf("lost run settled: %+v", wd.queue)
	}
}

func TestASpentJobIsSettledFromItsRun(t *testing.T) {
	key := deployment.RunKey{RepositoryID: "repo:x", RunID: "run-1"}
	// Its run ended before its worker died: it succeeded.
	wd := newWorld(fakeRunner{err: errors.New("must not run")})
	wd.store.runs[key] = deployment.Run{Key: key, Phase: deployment.Succeeded, Generation: 4}
	job := testJob()
	job.Run, job.SettleOnly = key, true
	wd.process(context.Background(), job)
	if len(wd.queue.succeeded) != 1 || wd.queue.succeeded[0].Generation != 4 {
		t.Fatalf("ended run: %+v", wd.queue)
	}
	// Its run never ended: the job and the run fail.
	wd = newWorld(fakeRunner{err: errors.New("must not run")})
	wd.store.runs[key] = deployment.Run{Key: key, Phase: deployment.Running}
	wd.process(context.Background(), job)
	if len(wd.queue.failed) != 1 || wd.queue.failed[0] != "retry_exhausted" || len(wd.store.failedRuns) != 1 {
		t.Fatalf("unended run: %+v %v", wd.queue, wd.store.failedRuns)
	}
}

func TestShutdownAndABusyRepositoryRequeueUncharged(t *testing.T) {
	wd := newWorld(fakeRunner{err: deployment.ErrRepositoryBusy})
	wd.process(context.Background(), testJob())
	if len(wd.queue.requeued) != 1 || wd.queue.requeued[0] != (requeued{"repository_busy", false, busyDelay}) {
		t.Fatalf("busy: %+v", wd.queue.requeued)
	}
	wd = newWorld(fakeRunner{block: true})
	parent, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		wd.process(parent, testJob())
	}()
	time.Sleep(50 * time.Millisecond)
	cancel()
	<-done
	if len(wd.queue.requeued) != 1 || wd.queue.requeued[0] != (requeued{"worker_stopped", false, 0}) || len(wd.queue.failed) != 0 {
		t.Fatalf("stopping: %+v", wd.queue)
	}
}

func TestTransientFailuresRetryUntilTheAttemptsAreSpent(t *testing.T) {
	unavailable := status.Error(codes.Unavailable, "temporarily unavailable")
	wd := newWorld(fakeRunner{err: unavailable})
	job := testJob()
	job.Attempts = 2
	wd.process(context.Background(), job)
	if len(wd.queue.requeued) != 1 || wd.queue.requeued[0] != (requeued{"transient_failure", true, time.Minute}) {
		t.Fatalf("retry: %+v", wd.queue.requeued)
	}
	wd = newWorld(fakeRunner{err: unavailable})
	job.Attempts = 6
	wd.process(context.Background(), job)
	if len(wd.queue.failed) != 1 || wd.queue.failed[0] != "retry_exhausted" || len(wd.store.failedRuns) != 1 {
		t.Fatalf("last attempt: %+v %v", wd.queue, wd.store.failedRuns)
	}
}

func TestPermanentFailuresFailTheJobThenTheRun(t *testing.T) {
	for _, err := range []error{errors.New("unknown failure"), deployment.ErrIntegrity, status.Error(codes.PermissionDenied, "denied")} {
		wd := newWorld(fakeRunner{err: err})
		wd.process(context.Background(), testJob())
		if len(wd.queue.failed) != 1 || wd.queue.failed[0] != "permanent_failure" || len(wd.queue.requeued) != 0 || len(wd.queue.succeeded) != 0 {
			t.Fatalf("permanent %v: %+v", err, wd.queue)
		}
		if n := len(wd.events); n < 2 || wd.events[n-2] != "fail job" || wd.events[n-1] != "fail run" {
			t.Fatalf("the run must fail after the job: %v", wd.events)
		}
	}
	// The run's own code names the failure.
	wd := newWorld(fakeRunnerWithCode{code: "build_failed"})
	wd.w.pipeline = fakeRunnerWithCode{code: "build_failed", store: wd.store}
	wd.process(context.Background(), testJob())
	if len(wd.queue.failed) != 1 || wd.queue.failed[0] != "build_failed" {
		t.Fatalf("run's code: %v", wd.queue.failed)
	}
	// A shutdown doesn't turn a permanent failure into a requeue.
	parent, cancel := context.WithCancel(context.Background())
	cancel()
	wd = newWorld(fakeRunner{err: deployment.ErrIntegrity})
	wd.process(parent, testJob())
	if len(wd.queue.failed) != 1 || len(wd.queue.requeued) != 0 {
		t.Fatalf("permanent failure during shutdown: %+v", wd.queue)
	}
	// The run is left alone when the job's failure could not be recorded.
	wd = newWorld(fakeRunner{err: deployment.ErrIntegrity})
	wd.queue.failErr = errors.New("mysql down")
	wd.process(context.Background(), testJob())
	if len(wd.store.failedRuns) != 0 {
		t.Fatalf("run failed without the job: %v", wd.store.failedRuns)
	}
}

// fakeRunnerWithCode fails the run with code, as the pipeline records it.
type fakeRunnerWithCode struct {
	code  string
	store *fakeStore
}

func (r fakeRunnerWithCode) Run(_ context.Context, key deployment.RunKey) (deployment.Run, error) {
	if r.store != nil {
		r.store.mu.Lock()
		run := r.store.runs[key]
		run.Phase, run.ErrorCode = deployment.Failed, r.code
		r.store.runs[key] = run
		r.store.mu.Unlock()
	}
	return deployment.Run{Key: key, Phase: deployment.Failed}, deployment.ErrIntegrity
}

func TestAWholeJobTimeoutFails(t *testing.T) {
	wd := newWorld(fakeRunner{block: true})
	wd.w.c.Timeout = 10 * time.Millisecond
	wd.process(context.Background(), testJob())
	if len(wd.queue.failed) != 1 || wd.queue.failed[0] != "job_timeout" || len(wd.queue.requeued) != 0 {
		t.Fatalf("timeout: %+v", wd.queue)
	}
}

func TestASupersededRunWhoseCommitIsLiveSucceeded(t *testing.T) {
	wd := newWorld(fakeRunner{phase: deployment.Superseded})
	job := testJob()
	job.CommitSHA = headSHA
	locator, _ := deployment.ParseGitHubURL(job.URL)
	live := deployment.RunKey{RepositoryID: locator.RepositoryID(), RunID: "run-other-org"}
	wd.store.runs[live] = deployment.Run{Key: live, Phase: deployment.Succeeded, Generation: 7, Metrics: &deployment.Metrics{Files: 9}}
	wd.store.state = graph.RepositoryState{RepositoryID: live.RepositoryID, LiveRunID: live.RunID, LiveCommit: headSHA, LiveGeneration: 7}
	wd.process(context.Background(), job)
	out := wd.queue.succeeded[0]
	if out.Status != jobqueue.Succeeded || out.Run != live || out.Generation != 7 || out.Metrics.(jobMetrics).Files != 9 {
		t.Fatalf("outcome %+v", out)
	}
	// Superseded by another commit, it stays superseded.
	wd = newWorld(fakeRunner{phase: deployment.Superseded})
	wd.store.state = graph.RepositoryState{LiveRunID: "run-newer", LiveCommit: pinnedSHA}
	wd.process(context.Background(), job)
	if wd.queue.succeeded[0].Status != jobqueue.Superseded {
		t.Fatalf("outcome %+v", wd.queue.succeeded[0])
	}
}

func TestARunTheGraphLostIsAdmittedAgain(t *testing.T) {
	wd := newWorld(fakeRunner{})
	job := testJob()
	job.CommitSHA = headSHA
	job.Run = deployment.RunKey{RepositoryID: "repo:gone", RunID: "run-gone"}
	wd.process(context.Background(), job)
	if wd.queue.cleared != 1 || len(wd.store.admitted) != 1 || len(wd.queue.succeeded) != 1 {
		t.Fatalf("readmission: cleared %d, admitted %d, %+v", wd.queue.cleared, len(wd.store.admitted), wd.queue)
	}
}

func TestTwoJobsOfOneRunRunOneAtATime(t *testing.T) {
	var r runningRuns
	key := deployment.RunKey{RepositoryID: "repo:x", RunID: "run-1"}
	release, ok := r.hold(key)
	if !ok {
		t.Fatal("first hold")
	}
	if _, ok = r.hold(key); ok {
		t.Fatal("a run held twice")
	}
	release()
	if _, ok = r.hold(key); !ok {
		t.Fatal("a released run not held again")
	}
}

func TestRenewUntilExpiryToleratesTransientErrors(t *testing.T) {
	q := &fakeQueue{renewErr: status.Error(codes.Unavailable, "unavailable")}
	expires := time.Now().Add(3 * time.Second)
	go func() {
		time.Sleep(1200 * time.Millisecond)
		q.mu.Lock()
		q.renewErr = nil
		q.mu.Unlock()
	}()
	next, err := renewUntilExpiry(context.Background(), q, testJob(), expires)
	if err != nil || !next.After(expires) {
		t.Fatalf("renewal should recover: %v %v", next, err)
	}
	q = &fakeQueue{renewErr: deployment.ErrFenceLost}
	if _, err = renewUntilExpiry(context.Background(), q, testJob(), expires); !errors.Is(err, deployment.ErrFenceLost) {
		t.Fatalf("fence loss must surface: %v", err)
	}
	q = &fakeQueue{renewErr: status.Error(codes.PermissionDenied, "denied")}
	if _, err = renewUntilExpiry(context.Background(), q, testJob(), expires); status.Code(err) != codes.PermissionDenied || q.renewals != 1 {
		t.Fatalf("permanent renewal error: %v, %d renewals", err, q.renewals)
	}
}
