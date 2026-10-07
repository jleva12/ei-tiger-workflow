// Package ingestion runs one admitted deployment end to end: fetch the commit,
// evaluate the build, find what changed, parse and attribute only the affected
// compilation contexts, assign persistent identities, project the graph,
// diff it against the live generation, load the new generation, and flip the
// repository's live pointer. Everything before the flip is invisible to
// readers and everything before Spanner is disposable.
package ingestion

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
	"ei-aitiger-codegraph/worker/internal/discovery"
	"ei-aitiger-codegraph/worker/internal/graphanalysis"
	"ei-aitiger-codegraph/worker/internal/localindex"
	"ei-aitiger-codegraph/worker/internal/repository/github"
	"ei-aitiger-codegraph/worker/internal/retry"
	"ei-aitiger-codegraph/worker/internal/scratch"
)

const Version = "ingestion-v5-members"

type Config struct {
	Dispatcher *parser.Dispatcher
	Provider   bc.Provider
	// Resolvers is the binding authority per language. Every compilation
	// context is attributed by the resolver of its source set's language.
	Resolvers        map[string]semantic.Resolver
	Matcher          semantic.Matcher
	Projector        semantic.Projector
	Store            *spannerstore.Store
	Source           *github.Service
	SyntaxCache      *localindex.SyntaxCache
	Embedder         codesearch.Provider // optional
	WorkDir          string              // run-local indexes live under <WorkDir>/runs
	OwnerID          string
	ParserLimits     parser.Limits
	BuildLimits      bc.Limits
	DiscoveryLimits  discovery.Limits
	Workers          int
	Loader           spannerstore.LoaderOptions
	EmbedConcurrency int
	LeaseTTL         time.Duration
	RenewInterval    time.Duration
	Logger           *slog.Logger
}

type Pipeline struct {
	config  Config
	digest  string
	workers int
}

func New(c Config) (*Pipeline, error) {
	switch {
	case c.Dispatcher == nil || c.Dispatcher.Digest() == "":
		return nil, errors.New("ingestion: parser dispatcher required")
	case c.Provider == nil || len(c.Resolvers) == 0 || c.Matcher == nil || c.Projector == nil:
		return nil, errors.New("ingestion: build provider, resolvers, matcher and projector required")
	case c.Store == nil || c.Source == nil || c.SyntaxCache == nil:
		return nil, errors.New("ingestion: store, repository source and syntax cache required")
	case c.WorkDir == "" || c.OwnerID == "":
		return nil, errors.New("ingestion: work directory and owner id required")
	}
	for language, resolver := range c.Resolvers {
		if language == "" || resolver == nil {
			return nil, errors.New("ingestion: every resolver needs a language and an implementation")
		}
	}
	if err := c.ParserLimits.Validate(); err != nil {
		return nil, err
	}
	if err := c.BuildLimits.Validate(); err != nil {
		return nil, err
	}
	if err := c.DiscoveryLimits.Validate(); err != nil {
		return nil, err
	}
	if c.Workers < 1 || c.Workers > 64 {
		return nil, errors.New("ingestion: workers must be between 1 and 64")
	}
	if c.LeaseTTL <= 0 {
		c.LeaseTTL = 2 * time.Minute
	}
	if c.RenewInterval <= 0 || c.RenewInterval >= c.LeaseTTL/2 {
		c.RenewInterval = c.LeaseTTL / 4
	}
	if c.EmbedConcurrency < 1 {
		c.EmbedConcurrency = codesearch.DefaultIndexConcurrency
	}
	if c.Logger == nil {
		c.Logger = slog.Default()
	}
	scratch.Sweep(filepath.Join(c.WorkDir, "runs"), "", scratch.Stale) // run indexes a killed process left
	if err := os.MkdirAll(filepath.Join(c.WorkDir, "runs"), 0o700); err != nil {
		return nil, err
	}
	return &Pipeline{config: c, digest: Digest(c.Dispatcher.Digest(), ResolverVersions(c.Resolvers), c.BuildLimits, c.DiscoveryLimits, c.ParserLimits), workers: c.Workers}, nil
}

// Digest is the analysis configuration fingerprint a request is bound to:
// the pipeline and IR versions, the parser registry, discovery, every
// language's resolver identity, and the limits.
func Digest(dispatcher string, resolvers []ResolverVersion, build bc.Limits, scan discovery.Limits, parse parser.Limits) string {
	return "sha256:" + digestOf(struct {
		Version, IR, Registry, Discovery string
		Resolvers                        []ResolverVersion
		Build                            bc.Limits
		Scan                             discovery.Limits
		Parse                            parser.Limits
	}{Version, ir.SchemaVersion, dispatcher, discovery.Version, resolvers, build, scan, parse})
}

// ConfigurationDigest identifies what this pipeline computes. Admission binds
// each request to it so only compatible workers execute the run.
func (p *Pipeline) ConfigurationDigest() string { return p.digest }

type stageClock struct {
	start     time.Time
	last      time.Time
	durations map[string]int64
	current   string // the phase under way, for the run's last line
}

func newClock() *stageClock {
	now := time.Now()
	return &stageClock{start: now, last: now, durations: map[string]int64{}}
}
func (c *stageClock) mark(stage string) time.Duration {
	now := time.Now()
	d := now.Sub(c.last)
	c.durations[stage] += d.Milliseconds()
	c.last = now
	return d
}

// previousReader adapts the store to the workspace's baseline contract.
type previousReader struct {
	store      *spannerstore.Store
	repo       string
	generation uint64
}

func (r previousReader) PreviousIdentities(ctx context.Context, lineage string) (semantic.FileIdentities, error) {
	if r.generation == 0 {
		return semantic.FileIdentities{}, semantic.ErrNotFound
	}
	m, err := r.store.GetFileIdentities(ctx, r.repo, lineage, r.generation)
	if errors.Is(err, deployment.ErrNotFound) || errors.Is(err, graph.ErrNotFound) {
		return semantic.FileIdentities{}, semantic.ErrNotFound
	}
	return m, err
}

// Run executes one admitted run to completion. It returns the final run
// record; on failure the run is marked FAILED with a bounded error code.
func (p *Pipeline) Run(ctx context.Context, key deployment.RunKey) (out deployment.Run, err error) {
	if err = key.Validate(); err != nil {
		return out, err
	}
	store := p.config.Store
	log := p.config.Logger.With("run_id", key.RunID, "repository_id", key.RepositoryID)
	run, err := store.GetRun(ctx, key)
	if err != nil {
		return out, err
	}
	if run.Terminal() {
		return run, nil
	}
	if run.Request.AnalysisConfigDigest != p.digest {
		// Claims match the job's digest, so this run's job was adopted from
		// a configuration no live worker serves: it runs under this one,
		// which the generation's inputs record.
		log.Info("running a run admitted under another analysis configuration", "admitted", run.Request.AnalysisConfigDigest, "worker", p.digest)
	}
	repo, err := store.GetRepository(ctx, key.RepositoryID)
	if err != nil {
		return run, err
	}
	lease, err := store.AcquireLease(ctx, key, p.config.OwnerID, p.config.LeaseTTL)
	if err != nil {
		return run, err
	}
	guard := startLease(ctx, store, lease, p.config.LeaseTTL, p.config.RenewInterval)
	defer func() {
		guard.Stop()
		release, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if e := store.ReleaseLease(release, guard.Lease()); e != nil && !errors.Is(e, deployment.ErrFenceLost) {
			err = errors.Join(err, e)
		}
	}()
	work := guard.Context()
	clock := newClock()
	metrics := deployment.Metrics{Durations: clock.durations}
	finish := func(cause error) {
		if cause == nil || errors.Is(cause, deployment.ErrFenceLost) || guard.Err() != nil {
			return
		}
		mark, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		latest, e := store.GetRun(mark, key)
		if e != nil {
			return
		}
		if latest.Terminal() {
			return
		}
		latest.Phase = deployment.Failed
		if errors.Is(cause, deployment.ErrStaleDeployment) {
			// A newer deployment is live; this run must not be retried.
			latest.Phase = deployment.Superseded
		}
		latest.ErrorCode = errorCode(cause)
		latest.ErrorMessage = deployment.FailureMessage(cause.Error())
		retryable := retry.Transient(cause) && ctx.Err() != context.DeadlineExceeded
		latest.FailureRetryable = &retryable
		now := time.Now().UTC()
		latest.FinishedAt = &now
		if latest.Index != nil && latest.Index.Status == deployment.IndexRunning {
			index := *latest.Index
			index.Status, index.FinishedAt, index.Error = deployment.IndexIncomplete, &now, latest.ErrorCode
			latest.Index = &index
		}
		latest.Metrics = &metrics
		if _, e = store.UpdateRun(mark, latest); e != nil {
			log.Warn("could not record run failure", "error", e)
		}
	}
	defer func() {
		if err == nil {
			log.Info("run finished", "outcome", out.Phase, "generation", out.Generation, "elapsed", roundDuration(time.Since(clock.start)), "with_warnings", out.WarningMessage != "")
			return
		}
		log.Warn("run stopped", "phase", clock.current, "elapsed", roundDuration(time.Since(clock.start)), "error", err)
	}()
	defer func() {
		if err != nil {
			if lost := guard.Err(); lost != nil {
				// The lease was lost and the work stopped wherever it was:
				// the loss is the failure, not the step it interrupted.
				err = errors.Join(lost, fmt.Errorf("the run stopped: %w", err))
			} else {
				err = errors.Join(err, context.Cause(work))
			}
			finish(err)
		}
	}()

	// Generation and baseline come from the live state under the lease.
	state, err := store.State(work, key.RepositoryID)
	if err != nil {
		return run, err
	}
	if !deployment.ValidBranch(run.Request.Branch) || run.Request.Branch != repo.Branch {
		return run, fmt.Errorf("%w: run must use the repository's tracked branch", deployment.ErrInvalidRequest)
	}
	if state.LiveRunID != "" && state.LiveRunID != key.RunID {
		live, e := store.GetRun(work, deployment.RunKey{RepositoryID: key.RepositoryID, RunID: state.LiveRunID})
		if e != nil {
			return run, e
		}
		if run.Request.SupersededBy(live.Request) {
			// Publish would refuse this run; end it now without work or retries.
			now := time.Now().UTC()
			run.Phase, run.FinishedAt = deployment.Superseded, &now
			if run, err = store.UpdateRun(work, run); err != nil {
				return run, err
			}
			log.Info("run superseded by the live deployment", "sequence", run.Request.DeploymentSequence, "live_sequence", live.Request.DeploymentSequence, "live_run_id", live.Key.RunID)
			return run, nil
		}
	}
	// Publication may have committed before an embedding failure, process
	// crash, or uncertain transaction result. Resume the live index directly;
	// rebuilding would discard durable progress and allocate a new generation.
	if state.LiveRunID == key.RunID && state.LiveGeneration == run.Generation && run.Index != nil {
		if run.Metrics != nil {
			metrics = *run.Metrics
			if metrics.Durations == nil {
				metrics.Durations = map[string]int64{}
			}
			clock.durations = metrics.Durations
		}
		run.Attempts++
		log.Info("resuming required embeddings", "generation", run.Generation)
		return p.index(work, guard, run, &metrics, clock, log)
	}
	generation := state.LiveGeneration + 1
	run.Phase = deployment.Running
	run.FinishedAt, run.ErrorCode, run.ErrorMessage, run.WarningMessage, run.FailureRetryable = nil, "", "", "", nil
	run.Generation = generation
	run.BaselineGeneration = state.LiveGeneration
	run.BaselineCommit = state.LiveCommit
	now := time.Now().UTC()
	run.StartedAt = &now
	run.Attempts++
	if run, err = store.UpdateRun(work, run); err != nil {
		return run, err
	}
	log.Info("run started", "commit", run.Request.TargetCommitSHA, "generation", generation, "baseline_generation", state.LiveGeneration, "baseline_commit", state.LiveCommit)
	clock.current = "prepare"

	// Any rows above live belong to an abandoned attempt.
	if deleted, reopened, e := store.SweepAboveLive(work, guard.Lease()); e != nil {
		return run, fmt.Errorf("ingestion: sweep: %w", e)
	} else if deleted+reopened > 0 {
		log.Info("swept abandoned generation", "deleted", deleted, "reopened", reopened)
	}
	clock.finish(log, "prepare")

	clock.begin(log, "fetch")
	locator, err := github.ParseURL(repo.GitHubURL, run.Request.TargetCommitSHA)
	if err != nil {
		return run, err
	}
	// baselineCommit is false when the published commit cannot be diffed
	// against: every file is recomputed instead.
	baselineCommit := run.BaselineCommit != ""
	if err := p.config.Source.ValidateRevision(work, github.Request{Owner: locator.Owner, Name: locator.Name, CommitSHA: run.Request.TargetCommitSHA}, run.Request.Branch, state.LiveCommit); err != nil {
		switch {
		case errors.Is(err, deployment.ErrStaleDeployment):
			// This job may have been queued before a descendant published.
			// Complete it as superseded without touching the visible graph.
			now := time.Now().UTC()
			run.Phase, run.FinishedAt, run.ErrorCode = deployment.Superseded, &now, "commit_not_forward"
			return store.UpdateRun(work, run)
		case errors.Is(err, github.ErrHistoryRewritten):
			// A force-push: the files still diff against the published
			// commit, whatever its ancestry.
			log.Warn("the branch history was rewritten; the graph goes on from the published commit", "error", err)
		case errors.Is(err, github.ErrBaselineGone):
			log.Warn("the published commit is gone from the repository; every file is recomputed", "error", err)
			baselineCommit = false
		default:
			return run, fmt.Errorf("ingestion: validate branch revision: %w", err)
		}
	}
	checkout, err := p.config.Source.Checkout(work, github.Request{Owner: locator.Owner, Name: locator.Name, CommitSHA: run.Request.TargetCommitSHA})
	if err != nil {
		return run, fmt.Errorf("ingestion: checkout: %w", err)
	}
	defer func() { err = errors.Join(err, checkout.Close()) }()
	var changes []github.FileChange
	if baselineCommit && run.BaselineCommit != run.Request.TargetCommitSHA {
		changes, err = p.config.Source.Diff(work, github.Request{Owner: locator.Owner, Name: locator.Name, CommitSHA: run.Request.TargetCommitSHA}, run.BaselineCommit, run.Request.TargetCommitSHA)
		if errors.Is(err, github.ErrCommitNotFound) && work.Err() == nil {
			log.Warn("the published commit cannot be diffed against; every file is recomputed", "error", err)
			changes, err, baselineCommit = nil, nil, false
		}
		if err != nil {
			return run, fmt.Errorf("ingestion: diff: %w", err)
		}
	}
	clock.finish(log, "fetch", "changed_files", len(changes), "diffed", baselineCommit && run.BaselineCommit != run.Request.TargetCommitSHA)

	clock.begin(log, "build")
	local := bc.Checkout{Path: checkout.Path, RepositoryID: key.RepositoryID, SnapshotID: checkout.CommitSHA}
	build, err := p.config.Provider.Build(work, bc.Request{Checkout: local, Limits: p.config.BuildLimits})
	if err != nil {
		return run, fmt.Errorf("ingestion: build context: %w", err)
	}
	if err = build.Validate(); err != nil {
		return run, err
	}
	if build.RepositoryID != local.RepositoryID || build.SnapshotID != local.SnapshotID {
		return run, bc.ErrIdentityMismatch
	}
	profiles := make(map[string]parser.Profile, len(build.Inventory.SourceSets))
	for _, set := range build.Inventory.SourceSets {
		language, version := set.SyntaxLanguage()
		profile := parser.Profile{Language: language, Version: version, Options: parser.Options{EnablePreview: set.EnablePreview, Settings: set.LanguageOptions}}
		if e := p.config.Dispatcher.ValidateProfile(profile, p.config.ParserLimits); e != nil {
			return run, fmt.Errorf("ingestion: source set %s: %w", set.ID, e)
		}
		profiles[string(set.ID)] = profile
	}
	gaps := buildWarnings(build, p.config.DiscoveryLimits.IncludeTests)
	for _, gap := range gaps {
		log.Warn("build left compiled output out", "reason", gap.text)
	}
	run.WarningMessage = warningMessage(gapTexts(gaps, nil))
	clock.finish(log, "build", "modules", len(build.Inventory.Modules), "source_sets", len(build.Inventory.SourceSets), "gaps", len(gaps))

	// Discovery: the full inventory is cheap and needed for context membership.
	clock.begin(log, "discover")
	var inventory []semantic.SourceInput
	report, err := discovery.WalkWithClassifier(work, local, build, p.config.DiscoveryLimits, p.config.Dispatcher, func(src ir.Source) error {
		inventory = append(inventory, semantic.SourceInput{Source: src, Lineage: graph.Lineage(key.RepositoryID, src.ModuleID, src.SourceSetID, src.Path)})
		return nil
	})
	if err != nil {
		return run, fmt.Errorf("ingestion: discover: %w", err)
	}
	metrics.Files = report.Files
	omitted := omissionWarning(report)
	if omitted != "" {
		log.Warn("discovery left files out", "omissions", report.Omissions, "issues", report.Issues)
	}
	run.WarningMessage = warningMessage(append(gapTexts(gaps, nil), nonEmpty(omitted)...))
	digests := contextDigests(build)
	inputs := deployment.GenerationInputs{SchemaVersion: deployment.SchemaVersion, ContextID: string(build.ID), AnalysisConfigDigest: p.digest, SourceSets: make(map[string]string, len(digests))}
	for id, digest := range digests {
		inputs.SourceSets[string(id)] = digest
	}
	scope, reason, err := p.changeScope(work, run, digests)
	if err != nil {
		return run, fmt.Errorf("ingestion: change scope: %w", err)
	}
	if !baselineCommit && run.BaselineGeneration != 0 {
		scope, reason = changeScope{full: true}, "baseline_commit_unavailable"
	}
	cs, err := computeChangeSet(work, key.RepositoryID, run.BaselineGeneration, inventory, build, changes, store, scope)
	if err != nil {
		return run, fmt.Errorf("ingestion: change set: %w", err)
	}
	if cs.full && run.BaselineGeneration != 0 {
		// A full recomputation retires every file the baseline had and
		// this commit does not, whatever git's diff said or could say.
		if err = retireMissingFiles(work, store, key.RepositoryID, run.BaselineGeneration, inventory, &cs); err != nil {
			return run, fmt.Errorf("ingestion: change set: %w", err)
		}
	}
	var affected []semantic.SourceInput
	for i := range inventory {
		if cs.files[inventory[i].Lineage] {
			inventory[i].Affected = true
			affected = append(affected, inventory[i])
		}
	}
	metrics.AffectedFiles = uint64(len(affected))
	metrics.Contexts = uint64(len(cs.contexts))
	metrics.InvalidatedContexts = uint64(len(scope.invalidated))
	clock.finish(log, "discover", "files", len(inventory), "affected", len(affected), "contexts", len(cs.contexts), "invalidated_contexts", len(scope.invalidated), "deleted", len(cs.deleted), "renamed", len(cs.renamed), "full", cs.full, "scope", reason)

	if !cs.full && len(affected) == 0 && len(cs.deleted) == 0 {
		// Nothing to recompute: publish an empty generation so the commit is live.
		return p.publish(work, guard, run, &metrics, inputs, clock, log)
	}

	// Run-local index.
	runDir := filepath.Join(p.config.WorkDir, "runs", sanitize(key.RunID))
	previous := previousReader{store: store, repo: key.RepositoryID, generation: run.BaselineGeneration}
	descriptorKey := descriptorDigest(p.config.Dispatcher)
	index, err := localindex.Open(work, runDir, build, checkout.Path, p.config.SyntaxCache, descriptorKey, previous, localindex.Options{})
	if err != nil {
		return run, fmt.Errorf("ingestion: open index: %w", err)
	}
	defer func() { err = errors.Join(err, index.Close(), os.RemoveAll(runDir)) }()
	for newLineage, oldLineage := range cs.renamed {
		index.AliasPrevious(newLineage, oldLineage)
	}

	// Parse affected files into the syntax cache and retain their bytes.
	clock.begin(log, "parse", "files", len(affected))
	pstats := parseStats{progress: newProgress(log, "parse", "files", uint64(len(affected)))}
	sets := make(map[string]bc.SourceSet, len(build.Inventory.SourceSets))
	for _, set := range build.Inventory.SourceSets {
		sets[string(set.ID)] = set
	}
	affected, err = p.parseAffected(work, checkout.Path, key.RepositoryID, affected, profiles, sets, descriptorKey, p.config.SyntaxCache, store, &pstats)
	if err != nil {
		return run, err
	}
	if err = index.PutFiles(work, inventory); err != nil {
		return run, err
	}
	ids := make([]ir.FileID, 0, len(affected))
	for _, in := range affected {
		ids = append(ids, in.Source.FileID)
	}
	if err = index.SetAffected(work, ids); err != nil {
		return run, err
	}
	for _, skipped := range pstats.skippedFiles {
		// A skipped file is discovered and retained but has no syntax: it
		// contributes no new declarations or bindings to this generation,
		// and whatever an earlier generation knew of it stays, so the edges
		// other files have into it stay valid.
		log.Warn("source skipped by the parser", "path", skipped.Path, "reason", skipped.Reason)
	}
	notParsed := pstats.warning()
	run.WarningMessage = warningMessage(append(gapTexts(gaps, nil), nonEmpty(omitted, notParsed)...))
	clock.finish(log, "parse", "parsed", pstats.parsed.Load(), "cached", pstats.cached.Load(), "skipped", pstats.skipped.Load(), "sources_retained", pstats.uploaded.Load())

	loader := store.NewLoader(guard.Lease(), generation, checkout.CommitSHA, p.config.Loader)
	differ := &graphanalysis.Differ{Repo: key.RepositoryID, Baseline: run.BaselineGeneration, Reader: store, Apply: loader.Apply}
	var projected semantic.ProjectionResult
	if len(affected) > 0 {
		// Resolve bindings with each language's own toolchain.
		clock.begin(log, "resolve", "files", len(affected))
		resolving := &lookupProgress{Workspace: index, progress: newProgress(log, "resolve", "files", uint64(len(affected))), seen: map[ir.FileID]bool{}}
		resolved, err := p.resolve(work, log, key, checkout.CommitSHA, checkout.Path, cs.contextList(), sets, resolving)
		if err != nil {
			return run, fmt.Errorf("ingestion: resolve: %w", err)
		}
		metrics.Symbols, metrics.Resolved, metrics.Unresolved, metrics.Ambiguous, metrics.Unsupported = resolved.Symbols, resolved.Resolved, resolved.Unresolved, resolved.Ambiguous, resolved.Unsupported
		metrics.Lookups = resolved.Resolved + resolved.Unresolved + resolved.Ambiguous + resolved.Unsupported
		// A set the resolver compiled itself is no longer a gap.
		for _, set := range resolved.CompiledOutputs {
			log.Info("resolution compiled the output the build left out", "source_set", set)
		}
		run.WarningMessage = warningMessage(append(append(gapTexts(gaps, resolved.CompiledOutputs), nonEmpty(omitted, notParsed)...), resolved.Warnings...))
		// A context resolved short is recorded with an input digest no build
		// produces, so the next run invalidates it and resolves every file
		// in it again, not only the ones that changed.
		for _, id := range resolved.Degraded {
			if digest, ok := inputs.SourceSets[id]; ok {
				inputs.SourceSets[id] = degradedDigest(digest)
			}
		}
		if len(resolved.Degraded) > 0 {
			log.Warn("resolution stopped short for some contexts; the next run resolves them again", "contexts", resolved.Degraded)
		}
		clock.finish(log, "resolve", "files_resolved", resolving.progress.done.Load(), "symbols", resolved.Symbols, "resolved", resolved.Resolved, "unresolved", resolved.Unresolved, "ambiguous", resolved.Ambiguous, "unsupported", resolved.Unsupported)

		// Persistent identities.
		clock.begin(log, "match", "files", len(affected))
		matched, err := p.config.Matcher.Match(work, semantic.MatchRequest{Run: key, Files: affected}, index)
		if err != nil {
			return run, fmt.Errorf("ingestion: match: %w", err)
		}
		batch := make([]semantic.FileIdentities, 0, 256)
		for _, in := range affected {
			m, e := index.Identities(work, in.Source.FileID)
			if e != nil {
				return run, fmt.Errorf("ingestion: identities %s: %w", in.Source.Path, e)
			}
			batch = append(batch, m)
			if len(batch) == 256 {
				if e = store.PutFileIdentities(work, guard.Lease(), generation, batch); e != nil {
					return run, e
				}
				batch = batch[:0]
			}
		}
		if len(batch) > 0 {
			if err = store.PutFileIdentities(work, guard.Lease(), generation, batch); err != nil {
				return run, err
			}
		}
		clock.finish(log, "match", "allocated", matched.Allocated, "continued", matched.Continued, "ambiguous", matched.Ambiguous)
	}

	// Project, diff and load generation G.
	clock.begin(log, "load", "files", len(affected), "deleted", len(cs.deleted))
	if len(affected) > 0 {
		loading := newProgress(log, "load", "files", uint64(len(affected)))
		projected, err = p.config.Projector.Project(work, semantic.ProjectRequest{Run: key, Files: affected, SyntaxLimits: p.config.ParserLimits, Progress: func() { loading.add(1) }}, index, differ.Emit)
		if err != nil {
			return run, fmt.Errorf("ingestion: project: %w", err)
		}
	}
	for _, lineage := range cs.deleted {
		if e := differ.RetireLineage(work, lineage); e != nil {
			return run, fmt.Errorf("ingestion: retire %s: %w", lineage, e)
		}
	}
	if err = differ.Finish(work); err != nil {
		return run, fmt.Errorf("ingestion: diff: %w", err)
	}
	if err = loader.Flush(work); err != nil {
		return run, fmt.Errorf("ingestion: load: %w", err)
	}
	var closed uint64
	if retired := differ.RetiredNodeIDs(); len(retired) > 0 {
		if closed, err = store.CloseEdgesTouching(work, guard.Lease(), generation, checkout.CommitSHA, retired); err != nil {
			return run, fmt.Errorf("ingestion: retire cascade: %w", err)
		}
	}
	stats := loader.Stats()
	// Facts are counted as the Differ accepted them: a fact projected twice
	// under one ID is loaded once.
	acceptedNodes, acceptedEdges := differ.Accepted()
	if dropped, conflicts := differ.Duplicates(); dropped > 0 {
		ids := make([]string, 0, len(conflicts))
		for _, k := range conflicts {
			ids = append(ids, string(k.Kind)+" "+k.ID)
		}
		log.Warn("projection repeated graph IDs; the first fact of each was kept", "dropped", dropped, "projected_nodes", projected.Nodes, "projected_edges", projected.Edges, "conflicting", ids)
	}
	metrics.Nodes, metrics.Edges = acceptedNodes, acceptedEdges
	metrics.Added, metrics.Updated, metrics.Retired, metrics.Reopened = stats.Added, stats.Updated, stats.Retired, stats.Reopened
	clock.finish(log, "load", "generation", generation, "nodes", acceptedNodes, "edges", acceptedEdges, "added", stats.Added, "updated", stats.Updated, "retired", stats.Retired, "reopened", stats.Reopened, "dangling_edges_closed", closed, "batches", stats.Batches)

	nodes, edges, err := store.CountOpen(work, key.RepositoryID, generation)
	if err != nil {
		return run, err
	}
	// An initial generation contains exactly what was accepted. A later
	// full recomputation also keeps lineage-less records the projection no
	// longer mentions, so the equality check applies to the initial load only.
	if run.BaselineGeneration == 0 && (nodes != acceptedNodes || edges != acceptedEdges) {
		return run, fmt.Errorf("%w: loaded %d nodes/%d edges, projected %d/%d", deployment.ErrIntegrity, nodes, edges, acceptedNodes, acceptedEdges)
	}
	clock.finish(log, "verify", "open_nodes", nodes, "open_edges", edges)
	return p.publish(work, guard, run, &metrics, inputs, clock, log)
}

// changeScope decides how much of the repository the run recomputes before
// git is consulted. Everything is recomputed for an initial generation, an
// analysis refresh, a baseline analysed under another configuration, or a
// baseline whose inputs were never recorded. Otherwise only the compilation
// contexts whose build inputs differ from the live generation's are
// invalidated; the change set expands them downstream like changed files.
func (p *Pipeline) changeScope(ctx context.Context, run deployment.Run, digests map[bc.SourceSetID]string) (changeScope, string, error) {
	if run.BaselineGeneration == 0 {
		return changeScope{full: true}, "initial", nil
	}
	if run.Request.TriggerKind == deployment.TriggerAnalysisRefresh {
		return changeScope{full: true}, "analysis_refresh", nil
	}
	previous, err := p.config.Store.GetGenerationInputs(ctx, run.Key.RepositoryID, run.BaselineGeneration)
	if errors.Is(err, deployment.ErrNotFound) {
		return changeScope{full: true}, "baseline_inputs_unknown", nil
	}
	if err != nil {
		return changeScope{}, "", err
	}
	if previous.AnalysisConfigDigest != p.digest {
		return changeScope{full: true}, "configuration_changed", nil
	}
	// A source set the baseline had and this build does not (the build's
	// layout changed): its files' lineages are gone although git deleted
	// nothing, and only a full recomputation retires them.
	for id := range previous.SourceSets {
		if _, ok := digests[bc.SourceSetID(id)]; !ok {
			return changeScope{full: true}, "source_sets_changed", nil
		}
	}
	invalidated := map[bc.SourceSetID]bool{}
	for id, digest := range digests {
		if previous.SourceSets[string(id)] != digest {
			invalidated[id] = true
		}
	}
	reason := "build_inputs_unchanged"
	if len(invalidated) > 0 {
		reason = "build_inputs_changed"
	}
	return changeScope{invalidated: invalidated}, reason, nil
}

func (p *Pipeline) publish(ctx context.Context, guard *leaseGuard, run deployment.Run, metrics *deployment.Metrics, inputs deployment.GenerationInputs, clock *stageClock, log *slog.Logger) (deployment.Run, error) {
	store := p.config.Store
	clock.current = "publish"
	// What this generation was computed from, so the next run can tell a
	// dependency or configuration change from an unchanged build.
	if err := store.PutGenerationInputs(ctx, guard.Lease(), run.Generation, inputs); err != nil {
		return run, fmt.Errorf("ingestion: record inputs: %w", err)
	}
	run.Metrics = metrics
	if p.config.Embedder != nil {
		run.Index = &deployment.IndexState{Status: deployment.IndexRunning, StartedAt: time.Now().UTC(), Model: p.config.Embedder.Model(), Dimensions: p.config.Embedder.Dimensions()}
	}
	published, err := store.Publish(ctx, guard.Lease(), run)
	if err != nil {
		return run, fmt.Errorf("ingestion: publish: %w", err)
	}
	clock.finish(log, "publish", "generation", published.Generation, "commit", published.Request.TargetCommitSHA)
	if published.Index != nil {
		return p.index(ctx, guard, published, metrics, clock, log)
	}
	return published, nil
}

// indexProgressInterval bounds how often a running index pass rewrites its
// count on the run record.
const indexProgressInterval = 10 * time.Second

// index completes required embeddings before the run can succeed. Every
// durable progress update is fenced; failure is returned to the job runner.
func (p *Pipeline) index(ctx context.Context, guard *leaseGuard, run deployment.Run, metrics *deployment.Metrics, clock *stageClock, log *slog.Logger) (deployment.Run, error) {
	if p.config.Embedder == nil {
		return run, fmt.Errorf("%w: required embedding provider is missing", deployment.ErrUnavailable)
	}
	previous := *run.Index
	if (previous.Model != "" && previous.Model != p.config.Embedder.Model()) || (previous.Dimensions != 0 && previous.Dimensions != p.config.Embedder.Dimensions()) {
		return run, fmt.Errorf("%w: required embedding configuration changed", deployment.ErrUnavailable)
	}
	run.Phase, run.FinishedAt, run.ErrorCode, run.ErrorMessage, run.FailureRetryable = deployment.Running, nil, "", "", nil
	run.Index = &deployment.IndexState{Status: deployment.IndexRunning, StartedAt: previous.StartedAt, Embedded: previous.Embedded, Requests: previous.Requests, Model: p.config.Embedder.Model(), Dimensions: p.config.Embedder.Dimensions()}
	updated, err := p.config.Store.UpdateIndex(ctx, guard.Lease(), run)
	if err != nil {
		return run, fmt.Errorf("ingestion: start index: %w", err)
	}
	run = updated
	// The total sizes the progress lines only; without it they count up.
	pending, err := p.config.Store.PendingDocuments(ctx, run.Key.RepositoryID, p.config.Embedder.Model(), p.config.Embedder.Dimensions())
	if err != nil {
		log.Debug("could not count the documents to embed", "error", err)
	}
	clock.begin(log, "embed", "documents", pending, "model", p.config.Embedder.Model())
	embedding := newProgress(log, "embed", "documents", pending)
	last := time.Now()
	progress := func(embedded uint64) {
		embedding.set(embedded)
		if time.Since(last) < indexProgressInterval {
			return
		}
		last = time.Now()
		state := *run.Index
		state.Embedded = previous.Embedded + embedded
		run.Index = &state
		updated, err := p.config.Store.UpdateIndex(ctx, guard.Lease(), run)
		if err != nil {
			log.Warn("index progress not recorded", "error", err)
			return
		}
		run = updated
	}
	report, indexErr := codesearch.IndexConcurrent(ctx, p.config.Store, p.config.Embedder, run.Key.RepositoryID, codesearch.IndexOptions{Concurrency: p.config.EmbedConcurrency, Logger: log, Progress: progress})
	indexErr = errors.Join(indexErr, context.Cause(ctx))
	if indexErr == nil {
		clock.finish(log, "embed", "embedded", report.Embedded, "skipped", report.Skipped, "requests", report.Requests, "pages", report.Pages)
	} else {
		clock.mark("embed")
	}
	finished := time.Now().UTC()
	state := *run.Index
	state.Embedded, state.Requests, state.FinishedAt = previous.Embedded+report.Embedded, previous.Requests+report.Requests, &finished
	state.Status, run.Phase = deployment.IndexComplete, deployment.Succeeded
	if indexErr != nil {
		state.Status, run.Phase, run.ErrorCode = deployment.IndexIncomplete, deployment.Failed, "embedding_failed"
		run.ErrorMessage = deployment.FailureMessage(indexErr.Error())
		retryable := retry.Transient(indexErr) && ctx.Err() != context.DeadlineExceeded
		run.FailureRetryable = &retryable
		state.Error = indexErr.Error()
		if len(state.Error) > 512 {
			state.Error = state.Error[:512]
		}
		log.Warn("required embedding pass failed", "embedded", state.Embedded, "error", indexErr)
	}
	metrics.Embedded = state.Embedded
	run.WarningMessage = embeddingWarning(run.WarningMessage, report.Skipped)
	if report.Skipped > 0 {
		log.Warn("documents left without an embedding", "skipped", report.Skipped, "nodes", report.SkippedNodes)
	}
	run.Index, run.Metrics = &state, metrics
	// Failure can be recorded after cancellation, but never after ownership
	// has expired or a successor has acquired the repository lease.
	final, cancel := context.WithTimeout(context.WithoutCancel(ctx), 15*time.Second)
	defer cancel()
	// A timed-out progress update may actually have committed. Refresh its
	// revision before settlement; UpdateIndex still checks the lease and live
	// generation in its write transaction. An uncertain progress response must
	// not turn a completed pass into a permanent revision-conflict failure.
	latest, readErr := p.config.Store.GetRun(final, run.Key)
	if readErr != nil {
		return run, errors.Join(indexErr, fmt.Errorf("ingestion: read index outcome: %w", readErr))
	}
	run.Revision = latest.Revision
	updated, persistErr := p.config.Store.UpdateIndex(final, guard.Lease(), run)
	if persistErr != nil {
		return run, errors.Join(indexErr, fmt.Errorf("ingestion: record index outcome: %w", persistErr))
	}
	return updated, indexErr
}

// buildWarning is what a run says about a source set the build left without
// compiled output.
type buildWarning struct {
	set  bc.SourceSetID
	text string
}

// buildWarnings names every source set the build left without compiled
// output, with the build's reason, e.g. "app main has no compiled output, …
// Maven could not compile it: …". The run still analyses those sets from
// source. Test source sets are named only when tests are analysed.
func buildWarnings(build bc.BuildContext, includeTests bool) []buildWarning {
	modules := make(map[bc.ModuleID]string, len(build.Inventory.Modules))
	for _, m := range build.Inventory.Modules {
		modules[m.ID] = m.Name
	}
	sets := make(map[bc.SourceSetID]bc.SourceSet, len(build.Inventory.SourceSets))
	for _, set := range build.Inventory.SourceSets {
		sets[set.ID] = set
	}
	var warnings []buildWarning
	var noClasspath []string
	classpathReason := ""
	for _, gap := range build.Inventory.MissingInputs {
		if gap.Requested == bc.GapBuildFailed {
			warnings = append(warnings, buildWarning{text: gap.Reason + " Its sources were analysed without the build, so references into its dependencies are unresolved."})
			continue
		}
		if gap.Requested == bc.GapConfiguration {
			warnings = append(warnings, buildWarning{text: gap.Reason})
			continue
		}
		where := modules[gap.ModuleID]
		if set, ok := sets[gap.SourceSetID]; ok {
			if set.Kind == bc.SourceSetTest && !includeTests {
				continue
			}
			where = strings.TrimSpace(modules[set.ModuleID] + " " + set.Name)
		}
		if strings.HasPrefix(gap.Requested, "classpath:") {
			if where != "" && !slices.Contains(noClasspath, where) {
				noClasspath = append(noClasspath, where)
			}
			if classpathReason == "" {
				classpathReason = gap.Reason
			}
			continue
		}
		if gap.Requested != bc.GapCompiledOutput {
			continue
		}
		if where == "" {
			where = "A source set"
		}
		warnings = append(warnings, buildWarning{set: gap.SourceSetID, text: where + " has no compiled output, so other source sets' references into it are unresolved. " + gap.Reason})
	}
	if len(noClasspath) > 0 {
		// One line for all of them: a repository the build cannot reach
		// leaves many modules without one, for the same reason.
		names := noClasspath[:min(4, len(noClasspath))]
		list := strings.Join(names, ", ")
		if more := len(noClasspath) - len(names); more > 0 {
			list += fmt.Sprintf(" and %d more", more)
		}
		verb := "has"
		if len(noClasspath) > 1 {
			verb = "have"
		}
		warnings = append(warnings, buildWarning{text: fmt.Sprintf("%s %s no dependency classpath, so references into libraries are unresolved there. %s", list, verb, classpathReason)})
	}
	return warnings
}

// gapTexts are the warnings of the sets the resolver did not compile itself.
func gapTexts(warnings []buildWarning, compiled []string) []string {
	var texts []string
	for _, w := range warnings {
		if !slices.Contains(compiled, string(w.set)) {
			texts = append(texts, w.text)
		}
	}
	return texts
}

// warningMessage is what a run says about what it left out, one warning per
// line; empty without warnings.
func warningMessage(warnings []string) string {
	return deployment.FailureMessage(strings.Join(warnings, "\n"))
}

// degradedDigest marks a context's recorded input digest as resolved short:
// a digest of the real one, which no build produces.
func degradedDigest(digest string) string {
	return "sha256:" + digestOf(struct{ Degraded string }{digest})
}

func nonEmpty(texts ...string) []string {
	var out []string
	for _, t := range texts {
		if t != "" {
			out = append(out, t)
		}
	}
	return out
}

// omissionWarning names what discovery left out that a reader would expect
// in the graph: directories nested beyond the depth limit, names the graph
// cannot store and source roots that are not directories. Missing, external
// or symlinked roots and special files are ordinary and not mentioned.
func omissionWarning(report discovery.Report) string {
	labels := map[string]string{
		"too_deep":                "nested deeper than the depth limit",
		"unrepresentable_path":    "with names the graph cannot store",
		"unsupported_source_root": "source roots that are not directories",
	}
	counts := map[string]int{}
	examples := map[string]string{}
	total := 0
	for _, issue := range report.Issues {
		if _, ok := labels[issue.Code]; !ok {
			continue
		}
		total++
		counts[issue.Code]++
		if examples[issue.Code] == "" {
			examples[issue.Code] = issue.Path
		}
	}
	if total == 0 {
		return ""
	}
	var parts []string
	for _, code := range []string{"too_deep", "unrepresentable_path", "unsupported_source_root"} {
		if counts[code] > 0 {
			parts = append(parts, fmt.Sprintf("%d %s (e.g. %s)", counts[code], labels[code], shorten(examples[code], 160)))
		}
	}
	return fmt.Sprintf("%d paths were left out of the graph: %s.", total, strings.Join(parts, "; "))
}

// shorten cuts text to max bytes on a character boundary.
func shorten(text string, max int) string {
	if len(text) <= max {
		return text
	}
	cut := max - len("…")
	for cut > 0 && !utf8.RuneStart(text[cut]) {
		cut--
	}
	return text[:cut] + "…"
}

const embeddingWarningText = " code search documents were left without an embedding: the embedding provider refused their text. They are still found by name and keyword search, and the next ingestion tries them again."

// embeddingWarning replaces the run warning's line about documents the
// embedding pass skipped, which a resumed pass recounts, and keeps the rest.
func embeddingWarning(warning string, skipped uint64) string {
	var lines []string
	for _, line := range strings.Split(warning, "\n") {
		if line != "" && !strings.HasSuffix(line, embeddingWarningText) {
			lines = append(lines, line)
		}
	}
	if skipped > 0 {
		lines = append(lines, strconv.FormatUint(skipped, 10)+embeddingWarningText)
	}
	return warningMessage(lines)
}

func errorCode(err error) string {
	switch {
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		return "interrupted"
	case errors.Is(err, deployment.ErrIntegrity), errors.Is(err, graph.ErrIntegrity), errors.Is(err, semantic.ErrIntegrity):
		return "integrity"
	case errors.Is(err, deployment.ErrUncertainCommit):
		return "uncertain_commit"
	case errors.Is(err, deployment.ErrStaleDeployment):
		return "stale_deployment"
	case errors.Is(err, deployment.ErrFenceLost):
		return "fence_lost"
	}
	msg := err.Error()
	if i := strings.IndexByte(msg, ':'); i > 0 && i < 64 {
		return strings.ReplaceAll(strings.TrimSpace(msg[:i]), " ", "_")
	}
	return "failed"
}

func sanitize(s string) string {
	var b strings.Builder
	for _, c := range s {
		if c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '_' {
			b.WriteRune(c)
		} else {
			b.WriteByte('_')
		}
	}
	return b.String()
}

func digestOf(v any) string {
	data, _ := json.Marshal(v)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
