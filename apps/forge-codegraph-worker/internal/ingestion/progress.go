package ingestion

import (
	"context"
	"log/slog"
	"slices"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// phases are the stages of a full run in order, named as the run's metrics
// name their durations. A run that has nothing to recompute skips from
// discover to publish; a resumed run only embeds.
var phases = []string{"prepare", "fetch", "build", "discover", "parse", "resolve", "match", "load", "verify", "publish", "embed"}

// progressInterval is the least time between two progress lines of one
// phase: a phase shorter than it logs only its start and finish.
const progressInterval = 20 * time.Second

// step is a phase's place in a full run, e.g. "5/11".
func step(phase string) string {
	return strconv.Itoa(slices.Index(phases, phase)+1) + "/" + strconv.Itoa(len(phases))
}

// begin logs the start of a phase. Every phase line carries "phase" and
// "step", so one run's progress reads as a short sequence of lines.
func (c *stageClock) begin(log *slog.Logger, phase string, attrs ...any) {
	c.current = phase
	log.Info("phase started", append([]any{"phase", phase, "step", step(phase)}, attrs...)...)
}

// finish ends a phase: its time is recorded in the run's metrics and logged
// with what it did.
func (c *stageClock) finish(log *slog.Logger, phase string, attrs ...any) {
	elapsed := c.mark(phase)
	log.Info("phase finished", append([]any{"phase", phase, "step", step(phase), "elapsed", roundDuration(elapsed)}, attrs...)...)
}

// progress counts one phase's units of work (files, documents) and logs how
// many are done and left at most once per progressInterval. It is safe for
// concurrent use. A total of 0 means the total is unknown.
type progress struct {
	log   *slog.Logger
	phase string
	unit  string
	total uint64
	start time.Time
	done  atomic.Uint64
	mu    sync.Mutex
	next  time.Time
}

func newProgress(log *slog.Logger, phase, unit string, total uint64) *progress {
	now := time.Now()
	return &progress{log: log, phase: phase, unit: unit, total: total, start: now, next: now.Add(progressInterval)}
}

// add counts n more units done.
func (p *progress) add(n uint64) { p.report(p.done.Add(n)) }

// set records the running total of units done.
func (p *progress) set(done uint64) {
	p.done.Store(done)
	p.report(done)
}

func (p *progress) report(done uint64) {
	now := time.Now()
	p.mu.Lock()
	if now.Before(p.next) {
		p.mu.Unlock()
		return
	}
	p.next = now.Add(progressInterval)
	p.mu.Unlock()
	elapsed := now.Sub(p.start)
	attrs := []any{"phase", p.phase, "step", step(p.phase), "unit", p.unit, "done", done}
	if p.total > 0 {
		left := p.total - min(done, p.total)
		attrs = append(attrs, "total", p.total, "left", left, "percent", done*100/p.total)
		if done > 0 && left > 0 {
			attrs = append(attrs, "eta", roundDuration(time.Duration(float64(elapsed)/float64(done)*float64(left))))
		}
	}
	p.log.Info("phase progress", append(attrs, "elapsed", roundDuration(elapsed))...)
}

func roundDuration(d time.Duration) string {
	if d < time.Second {
		return d.Round(time.Millisecond).String()
	}
	return d.Round(time.Second).String()
}

// lookupProgress counts an affected file as resolved when its resolver
// writes its lookups, which every resolver does once per affected file.
type lookupProgress struct {
	semantic.Workspace
	progress *progress
	mu       sync.Mutex
	seen     map[ir.FileID]bool
}

func (w *lookupProgress) PutLookups(ctx context.Context, id ir.FileID, lookups []semantic.Lookup) error {
	if err := w.Workspace.PutLookups(ctx, id, lookups); err != nil {
		return err
	}
	w.mu.Lock()
	first := !w.seen[id]
	w.seen[id] = true
	w.mu.Unlock()
	if first {
		w.progress.add(1)
	}
	return nil
}
