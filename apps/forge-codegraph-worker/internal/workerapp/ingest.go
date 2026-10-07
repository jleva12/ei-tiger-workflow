package workerapp

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os/exec"
	"sync"
	"time"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
	"ei-aitiger-codegraph/worker/internal/jobqueue"
	"ei-aitiger-codegraph/worker/internal/repository/github"
	"ei-aitiger-codegraph/worker/internal/retry"
)

// How a job is held: claimed for jobLease, renewed every jobRenewInterval.
const (
	jobLease         = 45 * time.Second
	jobRenewInterval = 10 * time.Second
	// busyDelay is how long a job waits when its repository is being
	// ingested by another run.
	busyDelay = 30 * time.Second
)

// forgeIntegration marks the repositories the worker registers for the
// Forge admin API's jobs.
const forgeIntegration = "forge-admin"

// queue is the worker's side of the Forge admin API's queue of ingestion
// jobs (internal/jobqueue).
type queue interface {
	Ping(context.Context) error
	Check(context.Context) error
	Claim(ctx context.Context, owner string, lease time.Duration, maxAttempts uint64) (jobqueue.Job, error)
	Renew(context.Context, jobqueue.Job, time.Duration) error
	SetCommit(context.Context, jobqueue.Job, string) error
	AttachRun(context.Context, jobqueue.Job, deployment.RunKey) error
	ClearRun(context.Context, jobqueue.Job) error
	Succeed(context.Context, jobqueue.Job, jobqueue.Outcome) error
	Requeue(ctx context.Context, job jobqueue.Job, delay time.Duration, charge bool, code, message string) error
	Fail(ctx context.Context, job jobqueue.Job, code, message string) error
}

// runs is what admitting, running and settling a job's run needs of the
// graph's store.
type runs interface {
	GetRepository(context.Context, string) (deployment.Repository, error)
	PutRepository(context.Context, deployment.Repository) (deployment.Repository, error)
	AdmitIngestion(context.Context, deployment.Admission) (deployment.AdmissionResult, error)
	SubmissionRun(context.Context, string) (deployment.RunKey, error)
	GetRun(context.Context, deployment.RunKey) (deployment.Run, error)
	FailRun(ctx context.Context, key deployment.RunKey, code string, retryable bool) error
	State(context.Context, string) (graph.RepositoryState, error)
}

type branchResolver interface {
	ResolveBranch(ctx context.Context, owner, name, branch string) (string, error)
}

type runner interface {
	Run(context.Context, deployment.RunKey) (deployment.Run, error)
}

// ingester runs the jobs the worker claims.
type ingester struct {
	c        Config
	logger   *slog.Logger
	queue    queue
	runs     runs
	branches branchResolver
	pipeline runner
	// digest is the worker's analysis configuration, which every run it
	// admits is bound to.
	digest  string
	running runningRuns
	// renewEvery is how often a claim is renewed; jobRenewInterval when 0.
	renewEvery time.Duration
}

// errExhausted settles a job whose worker died after its last allowed
// attempt, and whose run never ended.
var errExhausted = errors.New("worker: the job's attempts are spent")

// jobError is a failure with the code the job records for it.
type jobError struct {
	code string
	err  error
}

func (e *jobError) Error() string { return e.err.Error() }
func (e *jobError) Unwrap() error { return e.err }

// transient marks a failure that a later attempt can get past.
type transient struct{ error }

func (e transient) Unwrap() error   { return e.error }
func (e transient) Retryable() bool { return true }

// process runs a claimed job, holding its lease, and settles it. expires is
// when the claim lapses unless renewed, on this host's clock.
func (w *ingester) process(parent context.Context, job jobqueue.Job, expires time.Time) {
	timeoutCtx, cancel := context.WithTimeout(parent, w.c.Timeout)
	defer cancel()
	ctx, cancelCause := context.WithCancelCause(timeoutCtx)
	defer cancelCause(nil)
	// The lease covers every step: resolving the branch and admitting the
	// run read GitHub and Spanner, and may take a while.
	renewCtx, stopRenew := context.WithCancel(ctx)
	renewal := make(chan error, 1)
	every := w.renewEvery
	if every == 0 {
		every = jobRenewInterval
	}
	go func(held jobqueue.Job) {
		ticker := time.NewTicker(every)
		defer ticker.Stop()
		for {
			select {
			case <-renewCtx.Done():
				renewal <- nil
				return
			case <-ticker.C:
				next, err := renewUntilExpiry(renewCtx, w.queue, held, expires)
				if err != nil {
					if renewCtx.Err() != nil {
						renewal <- nil
					} else {
						cancelCause(err)
						renewal <- err
					}
					return
				}
				expires = next
			}
		}
	}(job)
	run, err := w.ingest(ctx, &job)
	stopRenew()
	err = errors.Join(err, <-renewal)
	settlement, stop := context.WithTimeout(context.Background(), w.c.CleanupTimeout)
	defer stop()
	w.settle(settlement, parent, timeoutCtx, job, run, err)
}

// ingest admits the job's run unless it was, and runs it unless it ended.
// job takes the commit and run it is given.
func (w *ingester) ingest(ctx context.Context, job *jobqueue.Job) (deployment.Run, error) {
	if job.SettleOnly {
		if job.Run.RunID == "" {
			return deployment.Run{}, errExhausted
		}
		run, err := w.runs.GetRun(ctx, job.Run)
		if err == nil && run.Terminal() {
			return run, nil
		}
		return run, errExhausted
	}
	if job.Run.RunID == "" {
		if err := w.admit(ctx, job); err != nil {
			return deployment.Run{}, err
		}
	}
	run, err := w.runs.GetRun(ctx, job.Run)
	if errors.Is(err, deployment.ErrNotFound) {
		// The graph no longer has the run, as after its database was
		// recreated: admit the job again.
		if err = w.queue.ClearRun(ctx, *job); err != nil {
			return deployment.Run{}, err
		}
		job.Run = deployment.RunKey{}
		if err = w.admit(ctx, job); err != nil {
			return deployment.Run{}, err
		}
		run, err = w.runs.GetRun(ctx, job.Run)
	}
	if err != nil || run.Terminal() {
		return run, err
	}
	// Two jobs can share a run (a job of each organization that has the
	// repository, the same commit already live): one of this worker's task
	// slots runs it, the others wait their turn as for a busy repository.
	release, ok := w.running.hold(job.Run)
	if !ok {
		return run, fmt.Errorf("%w: run %s is running in this worker", deployment.ErrRepositoryBusy, job.Run.RunID)
	}
	defer release()
	return invoke(ctx, w.pipeline, job.Run)
}

// admit records the commit the job ingests and the run it is admitted as.
// The commit is recorded before the run is admitted, so a retry admits the
// same one; a job whose run was admitted but not recorded (the worker died
// in between) finds it by its submission, whatever the worker's
// configuration is now.
func (w *ingester) admit(ctx context.Context, job *jobqueue.Job) error {
	locator, err := deployment.ParseGitHubURL(job.URL)
	if err != nil {
		return &jobError{"invalid_repository", err}
	}
	if job.CommitSHA == "" {
		head, err := w.branches.ResolveBranch(ctx, locator.Owner, locator.Name, job.Branch)
		if err != nil {
			return err
		}
		if err = w.queue.SetCommit(ctx, *job, head); err != nil {
			return err
		}
		job.CommitSHA = head
	}
	submission := "forge-job-" + job.ID
	key, err := w.runs.SubmissionRun(ctx, submission)
	if err == nil {
		job.Run = key
		return w.queue.AttachRun(ctx, *job, key)
	}
	if !errors.Is(err, deployment.ErrNotFound) {
		return err
	}
	repo, err := ensureRepository(ctx, w.runs, locator)
	if err != nil {
		return err
	}
	// A graph follows the branch it was first ingested on.
	if repo.Branch != "" && repo.Branch != job.Branch {
		return &jobError{"branch_conflict", fmt.Errorf("the code graph of %s follows branch %q, not %q", locator.CanonicalURL(), repo.Branch, job.Branch)}
	}
	admission := deployment.Admission{SubmissionID: submission, Request: deployment.Request{
		SchemaVersion: deployment.SchemaVersion, RepositoryID: repo.RepositoryID, Branch: job.Branch,
		TargetCommitSHA: job.CommitSHA, AnalysisConfigDigest: w.digest, TriggerKind: deployment.TriggerDeployment,
		RequestedBy: job.RequestedBy,
	}}
	var result deployment.AdmissionResult
	for range 3 {
		result, err = w.runs.AdmitIngestion(ctx, admission)
		if !errors.Is(err, deployment.ErrUncertainCommit) {
			break
		}
		// The commit may have landed: its submission says.
		if key, e := w.runs.SubmissionRun(ctx, submission); e == nil {
			result, err = deployment.AdmissionResult{Run: deployment.Run{Key: key}}, nil
			break
		}
	}
	if errors.Is(err, deployment.ErrConflictingReplay) {
		return &jobError{"branch_conflict", err}
	}
	if err != nil {
		return err
	}
	job.Run = result.Run.Key
	w.logger.InfoContext(ctx, "ingestion admitted", "job_id", job.ID, "repository", locator.CanonicalURL(),
		"repository_id", job.Run.RepositoryID, "run_id", job.Run.RunID, "commit", job.CommitSHA,
		"requested_by", job.RequestedBy, "reused", result.Reused)
	return w.queue.AttachRun(ctx, *job, job.Run)
}

// ensureRepository registers the repository in the graph on its first
// ingestion. Two first ingestions can race; the loser reads the winner's
// registration, and a race it keeps losing is retried later.
func ensureRepository(ctx context.Context, store runs, locator deployment.GitHubLocator) (deployment.Repository, error) {
	for range 3 {
		repo, err := store.GetRepository(ctx, locator.RepositoryID())
		if err == nil {
			if repo.GitHubURL != locator.CanonicalURL() {
				return repo, fmt.Errorf("%w: repository URL mismatch", deployment.ErrConflictingReplay)
			}
			return repo, nil
		}
		if !errors.Is(err, deployment.ErrNotFound) {
			return repo, err
		}
		repo, err = store.PutRepository(ctx, deployment.Repository{
			SchemaVersion: deployment.SchemaVersion, RepositoryID: locator.RepositoryID(),
			GitHubURL: locator.CanonicalURL(), IntegrationID: forgeIntegration,
		})
		if !errors.Is(err, deployment.ErrConflictingReplay) {
			return repo, err
		}
	}
	return deployment.Repository{}, transient{fmt.Errorf("%w: the repository changed while registering", deployment.ErrConflictingReplay)}
}

// settle records how the job went. Nothing is written for a job this worker
// lost. The graph's run is failed only after the job's failure is recorded,
// so a run another worker now holds is never touched.
func (w *ingester) settle(ctx, parent, timeoutCtx context.Context, job jobqueue.Job, run deployment.Run, err error) {
	logger := w.logger.With("job_id", job.ID, "repository", job.URL, "run_id", job.Run.RunID, "attempt", job.Attempts)
	if errors.Is(err, deployment.ErrFenceLost) {
		logger.WarnContext(ctx, "job lost: its lease lapsed or its repository was removed; nothing settled", "error", err)
		return
	}
	if err == nil {
		out := w.outcome(ctx, job, run)
		if e := w.queue.Succeed(ctx, job, out); e != nil {
			logger.WarnContext(ctx, "job completion unavailable; the claim's expiry lets it be settled again", "error", e)
			return
		}
		logger.InfoContext(ctx, "ingestion job finished", "status", out.Status, "generation", out.Generation)
		return
	}
	timedOut := parent.Err() == nil && errors.Is(timeoutCtx.Err(), context.DeadlineExceeded)
	message := deployment.FailureMessage(err.Error())
	switch {
	case parent.Err() != nil && errors.Is(err, context.Canceled):
		// The worker is stopping: another takes the job over, uncharged.
		w.requeue(ctx, logger, job, 0, false, "worker_stopped", "")
	case errors.Is(err, errExhausted):
		w.fail(ctx, logger, job, "retry_exhausted", "the worker running it stopped after its last allowed attempt", true)
	case errors.Is(err, deployment.ErrRepositoryBusy) && !timedOut:
		w.requeue(ctx, logger, job, busyDelay, false, "repository_busy", message)
	case timedOut || !retry.Transient(err):
		// A whole-job deadline is a workload or configuration limit:
		// replaying the same input with the same limit fails the same way.
		code := "job_timeout"
		if !timedOut {
			code = w.failureCode(ctx, job, err)
		}
		logger.ErrorContext(ctx, "ingestion job failed", "code", code, "error", err)
		w.fail(ctx, logger, job, code, message, false)
	case job.Attempts >= uint64(w.c.MaxAttempts):
		logger.ErrorContext(ctx, "ingestion job failed after its last attempt", "error", err)
		w.fail(ctx, logger, job, "retry_exhausted", message, true)
	default:
		// Long enough to ride out an outage of GitHub, Spanner or the
		// embedding provider: 30 s doubling to 10 min, about a quarter hour
		// in all.
		delay := min(30*time.Second<<min(max(job.Attempts, 1)-1, 8), 10*time.Minute)
		logger.WarnContext(ctx, "ingestion job will be retried", "delay", delay, "error", err)
		w.requeue(ctx, logger, job, delay, true, "transient_failure", message)
	}
}

func (w *ingester) requeue(ctx context.Context, logger *slog.Logger, job jobqueue.Job, delay time.Duration, charge bool, code, message string) {
	if e := w.queue.Requeue(ctx, job, delay, charge, code, message); e != nil {
		logger.WarnContext(ctx, "job requeue unavailable; the claim's expiry lets it be claimed again", "code", code, "error", e)
	}
}

func (w *ingester) fail(ctx context.Context, logger *slog.Logger, job jobqueue.Job, code, message string, retryable bool) {
	if e := w.queue.Fail(ctx, job, code, message); e != nil {
		logger.WarnContext(ctx, "job failure unavailable; the claim's expiry lets it be settled again", "code", code, "error", e)
		return
	}
	if job.Run.RunID == "" {
		return
	}
	if e := w.runs.FailRun(ctx, job.Run, code, retryable); e != nil {
		logger.WarnContext(ctx, "could not record the run as failed; the next ingestion resumes or supersedes it", "error", e)
	}
}

// failureCode names a permanent failure for the job: the step that knows
// why, or the code the run recorded.
func (w *ingester) failureCode(ctx context.Context, job jobqueue.Job, err error) string {
	var coded *jobError
	var exit *exec.ExitError
	switch {
	case errors.As(err, &coded):
		return coded.code
	case errors.Is(err, github.ErrBranchNotFound):
		return "branch_not_found"
	case errors.As(err, &exit):
		// git could not read the repository: missing, renamed away,
		// private without a token, or outside what CODEGRAPH_GITHUB_TOKEN reads.
		return "repository_unreadable"
	}
	if job.Run.RunID != "" {
		if run, e := w.runs.GetRun(ctx, job.Run); e == nil && run.ErrorCode != "" {
			return run.ErrorCode
		}
	}
	return "permanent_failure"
}

// outcome is how a job whose run ended went. A run superseded by a newer
// one that published the same commit, as another organization's job of the
// same repository does, still succeeded.
func (w *ingester) outcome(ctx context.Context, job jobqueue.Job, run deployment.Run) jobqueue.Outcome {
	out := jobqueue.Outcome{Status: jobqueue.Succeeded, Run: run.Key, Generation: run.Generation, Metrics: metricsOf(run)}
	if run.Phase != deployment.Superseded {
		return out
	}
	out.Status = jobqueue.Superseded
	state, err := w.runs.State(ctx, run.Key.RepositoryID)
	if err == nil && state.LiveRunID != "" && job.CommitSHA != "" && state.LiveCommit == job.CommitSHA {
		live := deployment.RunKey{RepositoryID: run.Key.RepositoryID, RunID: state.LiveRunID}
		out.Status, out.Run, out.Generation = jobqueue.Succeeded, live, state.LiveGeneration
		if liveRun, e := w.runs.GetRun(ctx, live); e == nil {
			out.Metrics = metricsOf(liveRun)
		}
	}
	return out
}

// jobMetrics is what a job keeps of its run: its counts, and what it left
// out.
type jobMetrics struct {
	*deployment.Metrics
	Warning string `json:"warning,omitempty"`
}

func metricsOf(run deployment.Run) jobMetrics {
	return jobMetrics{Metrics: run.Metrics, Warning: run.WarningMessage}
}

// renewUntilExpiry retries an uncertain renewal until the claim's last
// confirmed expiry; a lost fence cancels the work. The expiry is kept on
// this host's clock, conservatively from before each renewal was sent.
func renewUntilExpiry(ctx context.Context, q queue, job jobqueue.Job, expires time.Time) (time.Time, error) {
	owned, stopOwned := context.WithDeadline(ctx, expires)
	defer stopOwned()
	for {
		if err := owned.Err(); err != nil {
			return time.Time{}, errors.Join(deployment.ErrFenceLost, err)
		}
		sent := time.Now()
		attempt, stop := context.WithTimeout(owned, jobRenewInterval)
		err := q.Renew(attempt, job, jobLease)
		stop()
		if ctx.Err() != nil {
			return time.Time{}, ctx.Err()
		}
		if err == nil {
			return sent.Add(jobLease), nil
		}
		if errors.Is(err, deployment.ErrFenceLost) || !retry.Transient(err) {
			return time.Time{}, err
		}
		if !pause(owned, time.Second) {
			return time.Time{}, errors.Join(deployment.ErrFenceLost, owned.Err(), err)
		}
	}
}

func invoke(ctx context.Context, pipeline runner, key deployment.RunKey) (run deployment.Run, err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("ingestion handler panicked: %v", r)
		}
	}()
	// Git fetches for the run authenticate with CODEGRAPH_GITHUB_TOKEN, or
	// anonymously without one.
	run, err = pipeline.Run(ctx, key)
	if err == nil && !run.Terminal() {
		return run, fmt.Errorf("%w: pipeline returned incomplete run", deployment.ErrInvalidTransition)
	}
	return run, err
}

// runningRuns are the runs this worker's task slots are running.
type runningRuns struct {
	mu   sync.Mutex
	keys map[deployment.RunKey]bool
}

func (r *runningRuns) hold(key deployment.RunKey) (func(), bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.keys[key] {
		return nil, false
	}
	if r.keys == nil {
		r.keys = map[deployment.RunKey]bool{}
	}
	r.keys[key] = true
	return func() {
		r.mu.Lock()
		defer r.mu.Unlock()
		delete(r.keys, key)
	}, true
}

// Spanner's side of serving: probes and the worker's registration.
type registry interface {
	Ping(context.Context) error
	Live(context.Context) error
	RegisterWorker(context.Context, spannerstore.WorkerRegistration) error
	UnregisterWorker(context.Context, string, string) error
}
