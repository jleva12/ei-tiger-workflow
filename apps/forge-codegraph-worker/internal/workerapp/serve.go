package workerapp

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"sync"
	"time"

	spannerstore "ei-aitiger-codegraph/storage/spanner"
	"ei-aitiger-codegraph/worker/internal/jobqueue"
)

// The health loop probes both databases every heartbeatInterval and, when
// Spanner answers, heartbeats this worker's registration there, which
// records the worker, its queue and configuration while it lives. The probes
// are trivial reads with a short budget; the heartbeat is a write and gets
// its own, longer one, because during a load its commit queues behind the
// loader's and a shared three-second budget timed it out on a saturated
// instance.
const (
	heartbeatInterval = 5 * time.Second
	liveProbeTimeout  = 3 * time.Second
	heartbeatTimeout  = 10 * time.Second
	// idlePoll is how long a task slot waits after finding no job.
	idlePoll = 2 * time.Second
)

// Serve owns the shared database clients, job consumers and health listener.
func Serve(ctx context.Context, c Config, deps Dependencies, logger *slog.Logger) error {
	if logger == nil {
		return errors.New("worker: logger required")
	}
	if _, err := exec.LookPath("git"); err != nil {
		return errors.New("worker: git executable is required")
	}
	startup, cancel := context.WithTimeout(ctx, c.StartupTimeout)
	storage, err := OpenStorage(startup, c)
	cancel()
	if err != nil {
		return err
	}
	defer storage.Close()
	db, err := jobqueue.Open(c.JobsMySQLDSN)
	if err != nil {
		return err
	}
	defer db.Close()
	host, err := os.Hostname()
	if err != nil {
		return err
	}
	owner := fmt.Sprintf("%s:%d", host, os.Getpid())
	runtime, err := NewRuntime(c, deps, storage, owner, logger)
	if err != nil {
		return err
	}
	defer runtime.Close()
	if err = runtime.Source.Prune(ctx); err != nil {
		logger.Warn("worktree prune failed", "error", err)
	}
	digest := ConfigurationDigest(c, deps)
	registration := spannerstore.WorkerRegistration{Queue: c.Queue, OwnerID: owner, ConfigDigest: digest, Languages: deps.Languages, BuildMode: c.BuildMode, MaxAttempts: c.MaxAttempts, Embeddings: c.Embedding.Enabled(), StartedAt: time.Now().UTC()}
	if c.GitHubToken == "" {
		logger.Info("CODEGRAPH_GITHUB_TOKEN is not set: GitHub is read anonymously, so only public repositories can be ingested")
	}
	var api *workerAPI
	if c.AdmissionToken != "" {
		api = newWorkerAPI(c.AdmissionToken, storage.Store, storeGraph{storage.Store}, storeGraph{storage.Store}, newStoreAudit(storage.Store, runtime.Embedder),
			storeSearch{store: storage.Store, embedder: runtime.Embedder, enhance: c.Search.EnhanceQuery}, logger)
	}
	w := &ingester{c: c, logger: logger, queue: jobqueue.New(db, c.Queue), runs: storage.Store, branches: runtime.Source, pipeline: runtime.Pipeline, digest: digest}
	return serveJobs(ctx, w, storage.Store, registration, api)
}

func serveJobs(ctx context.Context, w *ingester, spanner registry, registration spannerstore.WorkerRegistration, api *workerAPI) (retErr error) {
	c, logger := w.c, w.logger
	startup, cancel := context.WithTimeout(ctx, c.StartupTimeout)
	defer cancel()
	status := &healthStatus{}
	health, healthDone, err := startHealth(startup, c.HealthAddr, status, api, logger)
	if err != nil {
		return err
	}
	if health != nil {
		defer health.Close()
	}
	if err = spanner.Ping(startup); err != nil {
		return fmt.Errorf("worker: Spanner startup check: %w", err)
	}
	if err = w.queue.Ping(startup); err != nil {
		return fmt.Errorf("worker: the admin API's MySQL (CODEGRAPH_JOBS_MYSQL_DSN) startup check: %w", err)
	}
	if err = w.queue.Check(startup); err != nil {
		return err
	}
	if err = spanner.RegisterWorker(startup, registration); err != nil {
		return fmt.Errorf("worker: register on queue %s: %w", registration.Queue, err)
	}
	defer func() {
		release, stop := context.WithTimeout(context.Background(), c.CleanupTimeout)
		defer stop()
		if e := spanner.UnregisterWorker(release, registration.Queue, registration.OwnerID); e != nil {
			logger.Warn("could not remove worker registration; it expires with its heartbeat", "error", e)
		}
	}()
	cancel()
	claiming, stopClaiming := context.WithCancel(ctx)
	defer stopClaiming()
	work, cancelWork := context.WithCancel(context.WithoutCancel(ctx))
	defer cancelWork()
	healthCtx, stopHealth := context.WithCancel(work)
	defer stopHealth()
	status.spannerOK.Store(true)
	status.queueOK.Store(true)
	status.ready.Store(true)
	host, err := os.Hostname()
	if err != nil {
		return err
	}
	var active sync.WaitGroup
	for i := 0; i < c.TaskConcurrency; i++ {
		active.Add(1)
		go func(slot int) {
			defer active.Done()
			owner := fmt.Sprintf("%s:%d:%d", host, os.Getpid(), slot)
			for claiming.Err() == nil {
				poll, stop := context.WithTimeout(claiming, 15*time.Second)
				claimed := time.Now()
				job, e := w.queue.Claim(poll, owner, jobLease, uint64(c.MaxAttempts))
				stop()
				if e != nil {
					if !errors.Is(e, jobqueue.ErrNoJob) && claiming.Err() == nil {
						logger.ErrorContext(claiming, "job claim failed", "error", e)
					}
					if !pause(claiming, idlePoll) {
						return
					}
					continue
				}
				if claiming.Err() != nil {
					release, stop := context.WithTimeout(context.Background(), c.CleanupTimeout)
					if e = w.queue.Requeue(release, job, 0, false, "worker_draining", ""); e != nil {
						logger.Warn("could not release claim while draining", "error", e)
					}
					stop()
					return
				}
				logger.Info("ingestion job claimed", "job_id", job.ID, "repository", job.URL, "branch", job.Branch,
					"organization_id", job.OrganizationID, "attempt", job.Attempts, "settle_only", job.SettleOnly)
				w.process(work, job, claimed.Add(jobLease))
			}
		}(i)
	}
	healthStopped := make(chan struct{})
	go func() {
		defer close(healthStopped)
		ticker := time.NewTicker(heartbeatInterval)
		defer ticker.Stop()
		for {
			select {
			case <-healthCtx.Done():
				return
			case <-ticker.C:
				probe, stop := context.WithTimeout(healthCtx, liveProbeTimeout)
				status.queueOK.Store(w.queue.Ping(probe) == nil)
				e := spanner.Live(probe)
				stop()
				status.spannerOK.Store(e == nil)
				if e != nil {
					continue
				}
				beat, stopBeat := context.WithTimeout(healthCtx, heartbeatTimeout)
				if re := spanner.RegisterWorker(beat, registration); re != nil {
					logger.Warn("worker heartbeat failed", "error", re)
				}
				stopBeat()
			}
		}
	}()
	logger.Info("ingestion worker ready", "queue", c.Queue, "task_concurrency", c.TaskConcurrency, "config_digest", w.digest)
	select {
	case <-ctx.Done():
	case e := <-healthDone:
		retErr = fmt.Errorf("worker: health listener stopped: %w", e)
	}
	status.ready.Store(false)
	stopClaiming()
	stopHealth()
	<-healthStopped
	logger.Info("worker draining", "drain_timeout", c.ShutdownTimeout, "cleanup_timeout", c.CleanupTimeout)
	done := make(chan struct{})
	go func() { active.Wait(); close(done) }()
	timer := time.NewTimer(c.ShutdownTimeout)
	defer timer.Stop()
	select {
	case <-done:
	case <-timer.C:
		cancelWork()
		cleanup := time.NewTimer(c.CleanupTimeout)
		defer cleanup.Stop()
		select {
		case <-done:
		case <-cleanup.C:
			return errors.Join(retErr, errors.New("worker: shutdown deadline exceeded"))
		}
	}
	if health != nil {
		shutdown, stop := context.WithTimeout(context.Background(), c.CleanupTimeout)
		defer stop()
		retErr = errors.Join(retErr, health.Shutdown(shutdown))
	}
	logger.Info("worker shutdown complete")
	return retErr
}

func pause(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}
