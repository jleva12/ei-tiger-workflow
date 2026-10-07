package graphanalysis

import (
	"context"
	"fmt"
	"sort"

	"ei-aitiger-codegraph/pkg/graph"
)

// PreviousReader reads the baseline generation. RecordsByLineage returns only
// the versions open at the generation for one lineage. GetNodes and GetEdges
// return, per ID, the version open at the generation or, failing that, the
// most recent version retired at or before it; IDs with no version are
// omitted. Nothing above the generation is ever returned.
type PreviousReader interface {
	RecordsByLineage(ctx context.Context, repositoryID, lineage string, generation uint64) ([]graph.Version, error)
	GetNodes(ctx context.Context, repositoryID string, ids []string, generation uint64) (map[string]graph.Version, error)
	GetEdges(ctx context.Context, repositoryID string, ids []string, generation uint64) (map[string]graph.Version, error)
}

// DefaultLineagelessBatch bounds how many lineage-less facts the Differ holds
// before reading their previous versions in one batch.
const DefaultLineagelessBatch = 512

// Differ turns the projector's fact stream into graph.Change batches, one per
// file lineage, against the baseline generation. Consecutive anchored facts
// are grouped by their anchor lineage; when the lineage changes (or on
// Finish) the group is diffed against the lineage's open versions and handed
// to Apply, after which the group is forgotten. Facts without an anchor are
// lineage-less: they are only ever added, updated or reopened, never retired
// here, because an external entity stays live while any open edge still
// references it. Retiring unreferenced externals is a later cleanup.
//
// The zero Baseline means an initial run: no reads, every fact is an add.
type Differ struct {
	Repo     string
	Baseline uint64
	Reader   PreviousReader
	Apply    func(context.Context, []graph.Change) error
	// LineagelessBatch overrides DefaultLineagelessBatch when positive.
	LineagelessBatch int

	open      bool
	lineage   string
	facts     []graph.Fact
	digests   map[graph.Key]string
	global    []graph.Fact
	seen      map[graph.Key]string
	projected map[graph.Key]bool // every key emitted this run, in any lineage
	retired   []string
	finished  bool

	nodes, edges uint64      // distinct facts accepted this run
	duplicates   uint64      // facts dropped because their key was already projected
	conflicts    []graph.Key // the first few dropped facts whose content differed
}

// maxConflicts bounds the conflicting keys a Differ remembers for reporting.
const maxConflicts = 8

// Accepted counts the distinct nodes and edges the Differ accepted this run:
// what an initial generation holds once it is loaded.
func (d *Differ) Accepted() (nodes, edges uint64) { return d.nodes, d.edges }

// Duplicates reports how many facts were dropped because a fact with the same
// key had already been projected this run, and the first few whose content
// differed from the fact kept. A duplicate with identical content is harmless;
// one with different content means two sources claimed the same ID, and the
// first one wins instead of failing the run.
func (d *Differ) Duplicates() (uint64, []graph.Key) {
	return d.duplicates, append([]graph.Key(nil), d.conflicts...)
}

// accept records a fact's key as projected, or reports that it already was:
// a repeated key is dropped, first projection wins. same says whether the
// dropped fact matched the kept one; a conflict is remembered for reporting.
func (d *Differ) accept(key graph.Key, same bool) bool {
	if d.projected[key] {
		d.duplicates++
		if !same && len(d.conflicts) < maxConflicts {
			d.conflicts = append(d.conflicts, key)
		}
		return false
	}
	d.projected[key] = true
	if key.Kind == graph.RecordNode {
		d.nodes++
	} else {
		d.edges++
	}
	return true
}

func (d *Differ) check() error {
	if d.finished {
		return fmt.Errorf("%w: differ is finished", graph.ErrInvalid)
	}
	if d.Apply == nil || d.Repo == "" || (d.Baseline != 0 && d.Reader == nil) {
		return fmt.Errorf("%w: differ needs a repository, an apply function and a reader for a baseline", graph.ErrInvalid)
	}
	if d.digests == nil {
		d.digests = map[graph.Key]string{}
	}
	if d.seen == nil {
		d.seen = map[graph.Key]string{}
	}
	if d.projected == nil {
		d.projected = map[graph.Key]bool{}
	}
	return nil
}

func (d *Differ) batch() int {
	if d.LineagelessBatch > 0 {
		return d.LineagelessBatch
	}
	return DefaultLineagelessBatch
}

// Emit accepts the next projected fact. Anchor-less facts take the
// lineage-less path.
func (d *Differ) Emit(ctx context.Context, f graph.Fact) error {
	if err := d.check(); err != nil {
		return err
	}
	if err := f.Validate(); err != nil {
		return err
	}
	a := f.Anchor()
	if a == nil {
		return d.EmitLineageless(ctx, f)
	}
	if len(d.global) > 0 {
		if err := d.flushLineageless(ctx); err != nil {
			return err
		}
	}
	if !d.open || a.Lineage != d.lineage {
		if err := d.flushLineage(ctx); err != nil {
			return err
		}
		d.open, d.lineage = true, a.Lineage
	}
	key, digest := f.Key(), f.Digest()
	prior, inGroup := d.digests[key]
	if !d.accept(key, inGroup && prior == digest) {
		return nil
	}
	d.digests[key] = digest
	d.facts = append(d.facts, f)
	return nil
}

// EmitLineageless accepts a fact that belongs to no file lineage.
func (d *Differ) EmitLineageless(ctx context.Context, f graph.Fact) error {
	if err := d.check(); err != nil {
		return err
	}
	if err := f.Validate(); err != nil {
		return err
	}
	key, digest := f.Key(), f.Digest()
	prior, seen := d.seen[key]
	if !d.accept(key, seen && prior == digest) {
		return nil
	}
	d.seen[key] = digest
	d.global = append(d.global, f)
	if len(d.global) >= d.batch() {
		return d.flushLineageless(ctx)
	}
	return nil
}

// RetireLineage retires every version open at the baseline for a lineage
// whose file no longer exists, except records this run projected under
// another lineage: a renamed file continues its entities, and the update that
// moved them to the new lineage already closed the old versions. Retiring them
// again would mark continued entities retired and feed them to the edge
// cascade, which would then close valid edges from unchanged files.
func (d *Differ) RetireLineage(ctx context.Context, lineage string) error {
	if err := d.check(); err != nil {
		return err
	}
	if !graph.ValidID(lineage) {
		return fmt.Errorf("%w: lineage %q", graph.ErrInvalid, lineage)
	}
	if d.open && d.lineage == lineage {
		return fmt.Errorf("%w: lineage %s has projected facts and cannot be retired", graph.ErrInvalid, lineage)
	}
	if d.Baseline == 0 {
		return nil
	}
	previous, err := d.Reader.RecordsByLineage(ctx, d.Repo, lineage, d.Baseline)
	if err != nil {
		return err
	}
	changes := make([]graph.Change, 0, len(previous))
	for _, v := range previous {
		if v.GenTo != 0 || d.projected[v.Fact.Key()] {
			continue
		}
		changes = append(changes, d.retire(v, lineage))
	}
	return d.apply(ctx, changes)
}

// Finish flushes the last lineage group and any pending lineage-less facts.
func (d *Differ) Finish(ctx context.Context) error {
	if err := d.check(); err != nil {
		return err
	}
	if err := d.flushLineage(ctx); err != nil {
		return err
	}
	if err := d.flushLineageless(ctx); err != nil {
		return err
	}
	d.finished = true
	return nil
}

// RetiredNodeIDs lists every node retired so far, for the cascade that
// retires open edges of unaffected files that still reference them.
func (d *Differ) RetiredNodeIDs() []string {
	return append([]string(nil), d.retired...)
}

func (d *Differ) retire(v graph.Version, lineage string) graph.Change {
	before := v
	if before.Fact.Node != nil {
		d.retired = append(d.retired, before.Fact.Node.ID)
	}
	return graph.Change{Op: graph.OpRetire, Key: before.Fact.Key(), Lineage: lineage, Before: &before}
}

func (d *Differ) apply(ctx context.Context, changes []graph.Change) error {
	if len(changes) == 0 {
		return nil
	}
	for _, c := range changes {
		if err := c.Validate(); err != nil {
			return fmt.Errorf("%s %s %s: %w", c.Op, c.Key.Kind, c.Key.ID, err)
		}
	}
	return d.Apply(ctx, changes)
}

func versionDigest(v graph.Version) string {
	if v.FactDigest != "" {
		return v.FactDigest
	}
	return v.Fact.Digest()
}

// history reads the latest baseline version of each fact's key, open or retired.
func (d *Differ) history(ctx context.Context, facts []graph.Fact) (map[graph.Key]graph.Version, error) {
	var nodes, edges []string
	for _, f := range facts {
		if f.Node != nil {
			nodes = append(nodes, f.Node.ID)
		} else {
			edges = append(edges, f.Edge.ID)
		}
	}
	found := make(map[graph.Key]graph.Version, len(facts))
	if len(nodes) > 0 {
		versions, err := d.Reader.GetNodes(ctx, d.Repo, nodes, d.Baseline)
		if err != nil {
			return nil, err
		}
		for id, v := range versions {
			found[graph.Key{Kind: graph.RecordNode, ID: id}] = v
		}
	}
	if len(edges) > 0 {
		versions, err := d.Reader.GetEdges(ctx, d.Repo, edges, d.Baseline)
		if err != nil {
			return nil, err
		}
		for id, v := range versions {
			found[graph.Key{Kind: graph.RecordEdge, ID: id}] = v
		}
	}
	return found, nil
}

// classify decides the change for a fact whose key is not open in its own
// lineage: absent history is an add, a retired version is a reopen, and a
// version still open elsewhere is updated in place when its content differs.
func classify(f graph.Fact, digest string, history map[graph.Key]graph.Version, lineage string) *graph.Change {
	key := f.Key()
	v, ok := history[key]
	fact := f
	switch {
	case !ok:
		return &graph.Change{Op: graph.OpAdd, Key: key, Lineage: lineage, After: &fact}
	case v.GenTo != 0:
		return &graph.Change{Op: graph.OpReopen, Key: key, Lineage: lineage, After: &fact}
	case versionDigest(v) == digest:
		return nil
	default:
		before := v
		return &graph.Change{Op: graph.OpUpdate, Key: key, Lineage: lineage, Before: &before, After: &fact}
	}
}

func (d *Differ) flushLineage(ctx context.Context) error {
	if !d.open {
		return nil
	}
	lineage, facts, digests := d.lineage, d.facts, d.digests
	d.open, d.lineage, d.facts, d.digests = false, "", nil, map[graph.Key]string{}

	previous := map[graph.Key]graph.Version{}
	if d.Baseline != 0 {
		versions, err := d.Reader.RecordsByLineage(ctx, d.Repo, lineage, d.Baseline)
		if err != nil {
			return err
		}
		for _, v := range versions {
			if v.GenTo != 0 {
				continue
			}
			key := v.Fact.Key()
			if _, dup := previous[key]; dup {
				return fmt.Errorf("%w: lineage %s has two open versions of %s %s", graph.ErrIntegrity, lineage, key.Kind, key.ID)
			}
			previous[key] = v
		}
	}

	changes := make([]graph.Change, 0, len(facts))
	var unseen []graph.Fact
	for _, f := range facts {
		key := f.Key()
		v, ok := previous[key]
		if !ok {
			unseen = append(unseen, f)
			continue
		}
		delete(previous, key)
		if versionDigest(v) == digests[key] {
			continue
		}
		before, fact := v, f
		changes = append(changes, graph.Change{Op: graph.OpUpdate, Key: key, Lineage: lineage, Before: &before, After: &fact})
	}
	history := map[graph.Key]graph.Version{}
	if d.Baseline != 0 && len(unseen) > 0 {
		var err error
		if history, err = d.history(ctx, unseen); err != nil {
			return err
		}
	}
	for _, f := range unseen {
		if c := classify(f, digests[f.Key()], history, lineage); c != nil {
			changes = append(changes, *c)
		}
	}
	gone := make([]graph.Key, 0, len(previous))
	for key := range previous {
		if d.projected[key] {
			continue // continued under a lineage flushed earlier this run
		}
		gone = append(gone, key)
	}
	sort.Slice(gone, func(i, j int) bool {
		if gone[i].Kind != gone[j].Kind {
			return gone[i].Kind < gone[j].Kind
		}
		return gone[i].ID < gone[j].ID
	})
	for _, key := range gone {
		changes = append(changes, d.retire(previous[key], lineage))
	}
	return d.apply(ctx, changes)
}

func (d *Differ) flushLineageless(ctx context.Context) error {
	if len(d.global) == 0 {
		return nil
	}
	facts := d.global
	d.global = nil
	history := map[graph.Key]graph.Version{}
	if d.Baseline != 0 {
		var err error
		if history, err = d.history(ctx, facts); err != nil {
			return err
		}
	}
	changes := make([]graph.Change, 0, len(facts))
	for _, f := range facts {
		if c := classify(f, d.seen[f.Key()], history, ""); c != nil {
			changes = append(changes, *c)
		}
	}
	return d.apply(ctx, changes)
}
