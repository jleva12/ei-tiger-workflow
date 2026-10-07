package spannerstore

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

// --- change -> mutation planning (pure) ------------------------------------

var recordInsertColumns = []string{"RepositoryID", "RecordKind", "RecordID", "GenFrom", "GenTo", "CommitFrom", "CommitTo", "Retired", "Lineage", "Kind", "Name", "SourceID", "TargetID", "FactDigest", "SearchHash", "Payload"}

// recordRow is a new open version of a record as it will be stored.
type recordRow struct {
	Repository string
	Kind       graph.RecordKind
	ID         string
	GenFrom    uint64
	Commit     string
	Lineage    string
	FactKind   string
	Name       string
	SourceID   string
	TargetID   string
	FactDigest string
	SearchHash string
	Payload    []byte
}

func nullString(s string) spanner.NullString { return spanner.NullString{StringVal: s, Valid: s != ""} }

func (r recordRow) mutation() *spanner.Mutation {
	return spanner.InsertOrUpdate("CGRecords", recordInsertColumns, []any{
		r.Repository, string(r.Kind), r.ID, int64(r.GenFrom), spanner.NullInt64{}, r.Commit, "", false,
		nullString(r.Lineage), r.FactKind, r.Name, nullString(r.SourceID), nullString(r.TargetID), r.FactDigest, nullString(r.SearchHash), r.Payload,
	})
}

// closeOp closes an existing version at a generation.
type closeOp struct {
	Repository string
	Kind       graph.RecordKind
	ID         string
	GenFrom    uint64
	GenTo      uint64
	Commit     string
	Retired    bool
}

func (c closeOp) mutation() *spanner.Mutation {
	return spanner.Update("CGRecords", []string{"RepositoryID", "RecordKind", "RecordID", "GenFrom", "GenTo", "CommitTo", "Retired"},
		[]any{c.Repository, string(c.Kind), c.ID, int64(c.GenFrom), int64(c.GenTo), c.Commit, c.Retired})
}

// changePlan is the row-level effect of one graph.Change. Document is the
// search-document row of a node that carries a retrieval document; it is
// committed after the records, in its own bounded batches.
type changePlan struct {
	Op       graph.Operation
	Insert   *recordRow
	Close    *closeOp
	Document *searchDocumentRow
}

func (p changePlan) mutations() []*spanner.Mutation {
	var ms []*spanner.Mutation
	if p.Close != nil {
		ms = append(ms, p.Close.mutation())
	}
	if p.Insert != nil {
		ms = append(ms, p.Insert.mutation())
	}
	return ms
}

func newRecordRow(repo string, generation uint64, commit, lineage string, f graph.Fact, maxBytes int) (recordRow, error) {
	b := payload(f)
	if len(b) > maxBytes {
		return recordRow{}, fmt.Errorf("%w: record %s exceeds %d bytes", deployment.ErrLimitExceeded, f.Key().ID, maxBytes)
	}
	r := recordRow{Repository: repo, Kind: f.Key().Kind, ID: f.Key().ID, GenFrom: generation, Commit: commit, Lineage: lineage, FactDigest: f.Digest(), Payload: b}
	if f.Node != nil {
		r.FactKind, r.Name = f.Node.Kind, f.Node.Name
		if d, ok := codesearch.FromNode(*f.Node); ok {
			r.SearchHash = d.Hash
		}
	} else {
		r.FactKind, r.SourceID, r.TargetID = f.Edge.Kind, f.Edge.SourceID, f.Edge.TargetID
	}
	return r, nil
}

// planChange validates a change for generation G and derives its rows. New
// versions always open at G; closed versions must have opened below G.
func planChange(repo string, generation uint64, commit string, c graph.Change, maxBytes int) (changePlan, error) {
	if err := c.Validate(); err != nil {
		return changePlan{}, err
	}
	p := changePlan{Op: c.Op}
	if c.Before != nil {
		if c.Before.GenFrom >= generation {
			return changePlan{}, fmt.Errorf("%w: %s %s: before-image opened at %d, loading %d", graph.ErrInvalid, c.Key.Kind, c.Key.ID, c.Before.GenFrom, generation)
		}
		p.Close = &closeOp{Repository: repo, Kind: c.Key.Kind, ID: c.Key.ID, GenFrom: c.Before.GenFrom, GenTo: generation, Commit: commit, Retired: c.Op == graph.OpRetire}
	}
	if c.After != nil {
		r, err := newRecordRow(repo, generation, commit, c.Lineage, *c.After, maxBytes)
		if err != nil {
			return changePlan{}, err
		}
		p.Insert = &r
		if c.After.Node != nil {
			if d, ok := newSearchDocumentRow(repo, generation, *c.After.Node); ok {
				p.Document = &d
			}
		}
	}
	return p, nil
}

// Search documents are committed separately from records so a record batch
// never approaches Spanner's per-commit mutation limit because of the search
// index, and in bounded sub-batches by row count and indexed bytes.
const (
	documentRowsPerCommit  = 100
	documentBytesPerCommit = 1 << 20
)

// --- Loader -----------------------------------------------------------------

type LoaderOptions struct {
	BatchRecords int // changes per commit (default 1000)
	// BatchBytes bounds the record bytes of one commit (default 16 MiB):
	// a thousand large declarations would otherwise approach Spanner's
	// per-commit size limit.
	BatchBytes  int
	Concurrency int // concurrent commits (default 8)
}

type LoaderStats struct{ Added, Updated, Retired, Reopened, Batches uint64 }

// Loader applies changes for one generation under a repository lease. Every
// batch is committed by fencedCommit: one read-write transaction that proves
// the lease still holds and that live has not reached the loader's generation
// before the mutations are buffered, so a worker whose lease was taken over
// cannot land a batch after its successor's sweep. Batches are idempotent, so
// a transaction the client retries after an abort is harmless. Renewing the
// lease is the caller's job.
type Loader struct {
	store      *Store
	lease      deployment.Lease
	repo       string
	generation uint64
	commit     string
	batch      int
	batchBytes int
	sem        chan struct{}

	mu           sync.Mutex
	buf          []*spanner.Mutation
	docs         []searchDocumentRow
	pending      int
	pendingBytes int
	wg           sync.WaitGroup

	errMu sync.Mutex
	err   error

	added, updated, retired, reopened, batches atomic.Uint64
}

func (s *Store) NewLoader(lease deployment.Lease, generation uint64, commit string, o LoaderOptions) *Loader {
	if o.BatchRecords <= 0 {
		o.BatchRecords = 1000
	}
	if o.Concurrency <= 0 {
		o.Concurrency = 8
	}
	if o.BatchBytes <= 0 {
		o.BatchBytes = 16 << 20
	}
	l := &Loader{store: s, lease: lease, repo: lease.Fence.Key.RepositoryID, generation: generation, commit: commit, batch: o.BatchRecords, batchBytes: o.BatchBytes, sem: make(chan struct{}, o.Concurrency)}
	if err := lease.Validate(); err != nil {
		l.fail(err)
	} else if generation == 0 || !deployment.ValidCommit(commit) {
		l.fail(fmt.Errorf("%w: loader generation and commit", deployment.ErrInvalidRequest))
	}
	return l
}

func (l *Loader) fail(err error) {
	l.errMu.Lock()
	defer l.errMu.Unlock()
	if l.err == nil {
		l.err = err
	}
}

func (l *Loader) failed() error {
	l.errMu.Lock()
	defer l.errMu.Unlock()
	return l.err
}

// Apply buffers changes and commits full batches in the background. The first
// commit failure is sticky and returned by Apply and Flush.
func (l *Loader) Apply(ctx context.Context, changes []graph.Change) error {
	if err := l.failed(); err != nil {
		return err
	}
	for _, c := range changes {
		p, err := planChange(l.repo, l.generation, l.commit, c, l.store.limits.MaxRecordBytes)
		if err != nil {
			l.fail(err)
			return err
		}
		l.mu.Lock()
		l.buf = append(l.buf, p.mutations()...)
		if p.Document != nil {
			l.docs = append(l.docs, *p.Document)
		}
		l.pending++
		if p.Insert != nil {
			l.pendingBytes += len(p.Insert.Payload) + len(p.Insert.Name)
		}
		var full []*spanner.Mutation
		var fullDocs []searchDocumentRow
		if l.pending >= l.batch || l.pendingBytes >= l.batchBytes {
			full, fullDocs, l.buf, l.docs, l.pending, l.pendingBytes = l.buf, l.docs, nil, nil, 0, 0
		}
		l.mu.Unlock()
		switch c.Op {
		case graph.OpAdd:
			l.added.Add(1)
		case graph.OpUpdate:
			l.updated.Add(1)
		case graph.OpRetire:
			l.retired.Add(1)
		case graph.OpReopen:
			l.reopened.Add(1)
		}
		if full != nil {
			if err := l.dispatch(ctx, full, fullDocs); err != nil {
				return err
			}
		}
	}
	return l.failed()
}

func (l *Loader) dispatch(ctx context.Context, ms []*spanner.Mutation, docs []searchDocumentRow) error {
	select {
	case l.sem <- struct{}{}:
	case <-ctx.Done():
		l.fail(ctx.Err())
		return ctx.Err()
	}
	if err := l.failed(); err != nil {
		<-l.sem
		return err
	}
	l.wg.Add(1)
	go func() {
		defer l.wg.Done()
		defer func() { <-l.sem }()
		defer func() {
			if r := recover(); r != nil {
				l.fail(fmt.Errorf("spanner: load batch panicked: %v", r))
			}
		}()
		if err := l.commitBatch(ctx, ms, docs); err != nil {
			l.fail(err)
		}
	}()
	return nil
}

// commitBatch commits the record batch in one transaction with the lease
// check and the guarantee that the loader's generation is still above live,
// then the batch's search documents in bounded fenced sub-batches.
func (l *Loader) commitBatch(ctx context.Context, ms []*spanner.Mutation, docs []searchDocumentRow) error {
	if err := l.store.fencedCommit(ctx, l.lease, belowLive(l.generation), ms); err != nil {
		return err
	}
	l.batches.Add(1)
	var pending []*spanner.Mutation
	size := 0
	flush := func() error {
		if len(pending) == 0 {
			return nil
		}
		err := l.store.fencedCommit(ctx, l.lease, belowLive(l.generation), pending)
		pending, size = nil, 0
		return err
	}
	for _, d := range docs {
		pending = append(pending, d.mutation())
		size += d.bytes()
		if len(pending) >= documentRowsPerCommit || size >= documentBytesPerCommit {
			if err := flush(); err != nil {
				return err
			}
		}
	}
	return flush()
}

// --- fenced commits -----------------------------------------------------------

// fenceCheck inspects the repository row read inside a fenced transaction.
type fenceCheck func(repoRow) error

// belowLive requires that generation has not been published yet.
func belowLive(generation uint64) fenceCheck {
	return func(r repoRow) error {
		if uint64(r.LiveGeneration) >= generation {
			return fmt.Errorf("%w: live generation %d reached generation %d", deployment.ErrFenceLost, r.LiveGeneration, generation)
		}
		return nil
	}
}

// liveIs requires that live is still exactly the generation the caller read
// when it planned its writes.
func liveIs(generation uint64) fenceCheck {
	return func(r repoRow) error {
		if uint64(r.LiveGeneration) != generation {
			return fmt.Errorf("%w: live generation moved from %d to %d", deployment.ErrFenceLost, generation, r.LiveGeneration)
		}
		return nil
	}
}

// fencedCommit writes mutations in one read-write transaction that first reads
// the repository row's fence cells (checkFence), verifies the lease identity
// and applies check. A holder whose lease was taken over therefore cannot
// commit anything: either the read fails inside the transaction, or the
// concurrent acquisition aborts the transaction and the retry sees the new
// token. Only those cells are read, so lease renewals, which write the expiry
// alone, never queue behind the loader's commits. Every caller passes
// idempotent mutations so a client-side retry after an abort is harmless.
func (s *Store) fencedCommit(ctx context.Context, lease deployment.Lease, check fenceCheck, ms []*spanner.Mutation) error {
	if len(ms) == 0 {
		return nil
	}
	return s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		r, err := checkFence(ctx, t, lease)
		if err != nil {
			return err
		}
		if check != nil {
			if err := check(r); err != nil {
				return err
			}
		}
		return t.BufferWrite(ms)
	})
}

// Flush commits the partial batch and waits for every in-flight commit.
func (l *Loader) Flush(ctx context.Context) error {
	l.mu.Lock()
	rest, restDocs := l.buf, l.docs
	l.buf, l.docs, l.pending, l.pendingBytes = nil, nil, 0, 0
	l.mu.Unlock()
	if len(rest) > 0 && l.failed() == nil {
		if err := l.dispatch(ctx, rest, restDocs); err != nil {
			l.wg.Wait()
			return err
		}
	}
	l.wg.Wait()
	return l.failed()
}

func (l *Loader) Stats() LoaderStats {
	return LoaderStats{Added: l.added.Load(), Updated: l.updated.Load(), Retired: l.retired.Load(), Reopened: l.reopened.Load(), Batches: l.batches.Load()}
}

// --- recovery and cascade -----------------------------------------------------

const sweepBatch = 500

// SweepAboveLive removes every trace of an abandoned generation: record
// versions opened above live are deleted, versions closed above live are
// reopened, and file identity maps and generation inputs written above live
// are deleted. Each commit
// carries at most 500 idempotent mutations and is fenced: the lease must hold
// and live must still be the generation the sweep was planned against.
func (s *Store) SweepAboveLive(ctx context.Context, lease deployment.Lease) (deleted uint64, reopened uint64, err error) {
	r, err := s.leaseSnapshot(ctx, lease)
	if err != nil {
		return 0, 0, err
	}
	repo, live := lease.Fence.Key.RepositoryID, r.LiveGeneration
	page := func(sql string, build func(*spanner.Row) (*spanner.Mutation, error)) (uint64, error) {
		var total uint64
		for {
			it := s.client.Single().Query(ctx, spanner.Statement{SQL: sql, Params: map[string]any{"repo": repo, "live": live, "limit": int64(sweepBatch)}})
			var ms []*spanner.Mutation
			for {
				row, err := nextRow(it)
				if err != nil {
					it.Stop()
					return total, err
				}
				if row == nil {
					break
				}
				m, err := build(row)
				if err != nil {
					it.Stop()
					return total, err
				}
				ms = append(ms, m)
			}
			it.Stop()
			if len(ms) == 0 {
				return total, nil
			}
			if err := s.fencedCommit(ctx, lease, liveIs(uint64(live)), ms); err != nil {
				return total, err
			}
			total += uint64(len(ms))
			if len(ms) < sweepBatch {
				return total, nil
			}
		}
	}
	recordKey := func(row *spanner.Row) (spanner.Key, error) {
		var kind, id string
		var genFrom int64
		if err := row.Columns(&kind, &id, &genFrom); err != nil {
			return nil, err
		}
		return spanner.Key{repo, kind, id, genFrom}, nil
	}
	deleted, err = page("SELECT RecordKind, RecordID, GenFrom FROM CGRecords@{FORCE_INDEX=CGRecordsByGenFrom} WHERE RepositoryID=@repo AND GenFrom>@live ORDER BY GenFrom, RecordKind, RecordID LIMIT @limit",
		func(row *spanner.Row) (*spanner.Mutation, error) {
			k, err := recordKey(row)
			if err != nil {
				return nil, err
			}
			return spanner.Delete("CGRecords", k), nil
		})
	if err != nil {
		return deleted, 0, err
	}
	reopened, err = page(nullFilteredHint+"SELECT RecordKind, RecordID, GenFrom FROM CGRecords@{FORCE_INDEX=CGRecordsByGenTo} WHERE RepositoryID=@repo AND GenTo IS NOT NULL AND GenTo>@live ORDER BY GenTo, RecordKind, RecordID LIMIT @limit",
		func(row *spanner.Row) (*spanner.Mutation, error) {
			k, err := recordKey(row)
			if err != nil {
				return nil, err
			}
			return spanner.Update("CGRecords", []string{"RepositoryID", "RecordKind", "RecordID", "GenFrom", "GenTo", "CommitTo", "Retired"}, append(append([]any{}, k...), spanner.NullInt64{}, "", false)), nil
		})
	if err != nil {
		return deleted, reopened, err
	}
	_, err = page("SELECT Lineage, Generation FROM CGFileIdentities@{FORCE_INDEX=CGFileIdentitiesByGeneration} WHERE RepositoryID=@repo AND Generation>@live ORDER BY Generation, Lineage LIMIT @limit",
		func(row *spanner.Row) (*spanner.Mutation, error) {
			var lineage string
			var gen int64
			if err := row.Columns(&lineage, &gen); err != nil {
				return nil, err
			}
			return spanner.Delete("CGFileIdentities", spanner.Key{repo, lineage, gen}), nil
		})
	if err != nil {
		return deleted, reopened, err
	}
	_, err = page("SELECT Generation FROM CGGenerationInputs WHERE RepositoryID=@repo AND Generation>@live ORDER BY Generation LIMIT @limit",
		func(row *spanner.Row) (*spanner.Mutation, error) {
			var gen int64
			if err := row.Columns(&gen); err != nil {
				return nil, err
			}
			return spanner.Delete("CGGenerationInputs", spanner.Key{repo, gen}), nil
		})
	if err != nil {
		return deleted, reopened, err
	}
	return deleted, reopened, s.restoreSearchDocuments(ctx, lease, repo, live)
}

// restoreSearchDocuments repairs search-document rows an abandoned generation
// wrote: a node still open at live gets its document rebuilt from the live
// record, a node that is not open at live loses its row.
func (s *Store) restoreSearchDocuments(ctx context.Context, lease deployment.Lease, repo string, live int64) error {
	for {
		it := s.client.Single().Query(ctx, spanner.Statement{
			SQL:    "SELECT NodeID FROM CGSearchDocuments WHERE RepositoryID=@repo AND Generation>@live ORDER BY NodeID LIMIT @limit",
			Params: map[string]any{"repo": repo, "live": live, "limit": int64(documentRowsPerCommit)},
		})
		var ids []string
		for {
			row, err := nextRow(it)
			if err != nil {
				it.Stop()
				return err
			}
			if row == nil {
				break
			}
			var id string
			if err = row.Column(0, &id); err != nil {
				it.Stop()
				return err
			}
			ids = append(ids, id)
		}
		it.Stop()
		if len(ids) == 0 {
			return nil
		}
		open, err := readOpenNodes(ctx, s.client.Single(), repo, ids, uint64(live), uint64(live))
		if err != nil {
			return err
		}
		ms := make([]*spanner.Mutation, 0, len(ids))
		for _, id := range ids {
			if v, ok := open[id]; ok {
				if r, ok := newSearchDocumentRow(repo, uint64(live), *v.Fact.Node); ok {
					ms = append(ms, r.mutation())
					continue
				}
			}
			ms = append(ms, spanner.Delete("CGSearchDocuments", spanner.Key{repo, id}))
		}
		if err := s.fencedCommit(ctx, lease, liveIs(uint64(live)), ms); err != nil {
			return err
		}
		if len(ids) < documentRowsPerCommit {
			return nil
		}
	}
}

// CloseEdgesTouching retires, at generation, every edge open at generation-1
// whose source or target is one of nodeIDs. Edges the loader already closed at
// generation are skipped, and so is every node that is open at generation:
// an entity that continued under another lineage (a renamed file) is not
// gone, and the edges into it from unchanged files must survive. Generation
// must be the one being loaded (live+1) and every commit is fenced.
func (s *Store) CloseEdgesTouching(ctx context.Context, lease deployment.Lease, generation uint64, commit string, nodeIDs []string) (closed uint64, err error) {
	if generation == 0 || !deployment.ValidCommit(commit) {
		return 0, fmt.Errorf("%w: generation and commit", deployment.ErrInvalidRequest)
	}
	for _, id := range nodeIDs {
		if !graph.ValidID(id) {
			return 0, fmt.Errorf("%w: node id %q", graph.ErrInvalid, id)
		}
	}
	r, err := s.leaseSnapshot(ctx, lease)
	if err != nil {
		return 0, err
	}
	if uint64(r.LiveGeneration) != generation-1 {
		return 0, fmt.Errorf("%w: live generation %d, closing at %d", deployment.ErrInvalidRequest, r.LiveGeneration, generation)
	}
	repo, prev := lease.Fence.Key.RepositoryID, generation-1
	type edgeKey struct {
		ID      string
		GenFrom int64
	}
	seen := map[edgeKey]bool{}
	var ms []*spanner.Mutation
	flush := func() error {
		if len(ms) == 0 {
			return nil
		}
		if err := s.fencedCommit(ctx, lease, liveIs(prev), ms); err != nil {
			return err
		}
		closed += uint64(len(ms))
		ms = nil
		return nil
	}
	const chunk = 200
	for start := 0; start < len(nodeIDs); start += chunk {
		candidates := nodeIDs[start:min(start+chunk, len(nodeIDs))]
		open, err := readOpenNodes(ctx, s.client.Single(), repo, candidates, generation, generation)
		if err != nil {
			return closed, err
		}
		ids := make([]string, 0, len(candidates))
		for _, id := range candidates {
			if _, stillOpen := open[id]; !stillOpen {
				ids = append(ids, id)
			}
		}
		if len(ids) == 0 {
			continue
		}
		for _, side := range [][2]string{{"CGEdgesBySource", "SourceID"}, {"CGEdgesByTarget", "TargetID"}} {
			it := s.client.Single().Query(ctx, spanner.Statement{
				SQL:    nullFilteredHint + "SELECT RecordID, GenFrom, GenTo FROM CGRecords@{FORCE_INDEX=" + side[0] + "} WHERE RepositoryID=@repo AND " + side[1] + " IS NOT NULL AND " + side[1] + " IN UNNEST(@ids) AND " + openPredicate(""),
				Params: map[string]any{"repo": repo, "ids": ids, "gen": int64(prev)},
			})
			for {
				row, err := nextRow(it)
				if err != nil {
					it.Stop()
					return closed, err
				}
				if row == nil {
					break
				}
				var id string
				var genFrom int64
				var genTo spanner.NullInt64
				if err = row.Columns(&id, &genFrom, &genTo); err != nil {
					it.Stop()
					return closed, err
				}
				k := edgeKey{id, genFrom}
				if genTo.Valid || seen[k] { // already closed at generation, or listed twice
					continue
				}
				seen[k] = true
				ms = append(ms, closeOp{Repository: repo, Kind: graph.RecordEdge, ID: id, GenFrom: uint64(genFrom), GenTo: generation, Commit: commit, Retired: true}.mutation())
				if len(ms) >= sweepBatch {
					if err = flush(); err != nil {
						it.Stop()
						return closed, err
					}
				}
			}
			it.Stop()
		}
	}
	return closed, flush()
}

// Publish flips the repository to run.Generation in one transaction: the lease
// must hold, live must be exactly run.Generation-1, the run's deployment
// sequence must be above the live run's (or equal for an analysis refresh),
// the run must be at its stored revision and allowed to succeed. Older
// ACCEPTED, RUNNING and FAILED runs are superseded, so a stalled older
// deployment can never publish after this one. Their jobs stay queued; a
// worker that claims one sees the terminal phase and completes it.
func (s *Store) Publish(ctx context.Context, lease deployment.Lease, run deployment.Run) (deployment.Run, error) {
	if err := run.Validate(); err != nil {
		return deployment.Run{}, err
	}
	if err := lease.Validate(); err != nil {
		return deployment.Run{}, err
	}
	if run.Key != lease.Fence.Key || run.Generation == 0 {
		return deployment.Run{}, fmt.Errorf("%w: publish run key and generation", deployment.ErrInvalidRequest)
	}
	var out deployment.Run
	err := s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		r, n, err := checkLease(ctx, t, lease)
		if err != nil {
			return err
		}
		if uint64(r.LiveGeneration) != run.Generation-1 {
			return fmt.Errorf("%w: live generation %d, publishing %d", deployment.ErrStaleDeployment, r.LiveGeneration, run.Generation)
		}
		stored, identity, err := readRun(ctx, t, run.Key)
		if err != nil {
			return err
		}
		if stored.Revision != run.Revision {
			return fmt.Errorf("%w: run revision %d, stored %d", deployment.ErrConflictingReplay, run.Revision, stored.Revision)
		}
		repo, err := r.repository()
		if err != nil {
			return err
		}
		if stored.Request.Branch == "" || stored.Request.Branch != repo.Branch {
			return fmt.Errorf("%w: publication must use the repository's tracked branch", deployment.ErrConflictingReplay)
		}
		if stored.BaselineCommit != r.LiveCommit || stored.BaselineGeneration != uint64(r.LiveGeneration) {
			return fmt.Errorf("%w: publication baseline changed", deployment.ErrStaleDeployment)
		}
		if err = deployment.ValidateTransition(stored.Phase, deployment.Succeeded); err != nil {
			return err
		}
		if r.LiveRunID != "" {
			live, _, err := readRun(ctx, t, deployment.RunKey{RepositoryID: run.Key.RepositoryID, RunID: r.LiveRunID})
			if err != nil {
				return err
			}
			if stored.Request.SupersededBy(live.Request) {
				return fmt.Errorf("%w: sequence %d cannot publish over live sequence %d", deployment.ErrStaleDeployment, stored.Request.DeploymentSequence, live.Request.DeploymentSequence)
			}
		}
		out = run
		out.SchemaVersion, out.Key, out.Request, out.AcceptedAt = stored.SchemaVersion, stored.Key, stored.Request, stored.AcceptedAt
		out.Phase, out.Revision, out.UpdatedAt = deployment.Succeeded, stored.Revision+1, n
		if out.Index != nil {
			if out.Index.Status != deployment.IndexRunning {
				return fmt.Errorf("%w: publication requires a running index", deployment.ErrInvalidTransition)
			}
			out.Phase = deployment.Running
			out.FinishedAt = nil
		}
		if out.Phase == deployment.Succeeded && out.FinishedAt == nil {
			finished := n
			out.FinishedAt = &finished
		}
		m, err := runMutation(out, identity)
		if err != nil {
			return err
		}
		ms := []*spanner.Mutation{
			spanner.Update("CGRepositories", []string{"RepositoryID", "LiveGeneration", "LiveCommit", "LiveRunID"}, []any{run.Key.RepositoryID, int64(run.Generation), out.Request.TargetCommitSHA, run.Key.RunID}),
			m,
		}
		it := t.Query(ctx, spanner.Statement{
			SQL:    "SELECT RunID, IdentityDigest, Payload FROM CGRuns@{FORCE_INDEX=CGRunsByPhase} WHERE RepositoryID=@repo AND Phase IN UNNEST(@phases) AND DeploymentSequence<@seq",
			Params: map[string]any{"repo": run.Key.RepositoryID, "phases": []string{string(deployment.Accepted), string(deployment.Running), string(deployment.Failed)}, "seq": int64(out.Request.DeploymentSequence)},
		})
		defer it.Stop()
		for {
			row, err := nextRow(it)
			if err != nil {
				return err
			}
			if row == nil {
				break
			}
			var id, olderIdentity string
			var b []byte
			if err = row.Columns(&id, &olderIdentity, &b); err != nil {
				return err
			}
			older, err := decodeRun(b, deployment.RunKey{RepositoryID: run.Key.RepositoryID, RunID: id})
			if err != nil {
				return err
			}
			if older.Key == run.Key {
				continue
			}
			older.Phase, older.Revision, older.UpdatedAt = deployment.Superseded, older.Revision+1, n
			if older.FinishedAt == nil {
				finished := n
				older.FinishedAt = &finished
			}
			m, err := runMutation(older, olderIdentity)
			if err != nil {
				return err
			}
			ms = append(ms, m)
		}
		return t.BufferWrite(ms)
	})
	if err != nil {
		return deployment.Run{}, err
	}
	return out, nil
}
