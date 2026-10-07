package ingestion

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/sync/errgroup"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/discovery"
	"ei-aitiger-codegraph/worker/internal/localindex"
)

type parseStats struct {
	parsed, cached, skipped, uploaded atomic.Uint64
	// generated counts the skipped files that are generated or minified
	// (a committed bundle): left out on purpose, not worth a warning.
	generated    atomic.Uint64
	mu           sync.Mutex
	skippedFiles []skippedFile // the first maxSkippedFiles, for the log
	progress     *progress     // optional: files parsed, cached or skipped
}

// fileDone counts one file through the parse phase, however it ended.
func (s *parseStats) fileDone() {
	if s.progress != nil {
		s.progress.add(1)
	}
}

type skippedFile struct {
	Path, Reason string
	Generated    bool
}

const maxSkippedFiles = 50

// warning names the files the parser could not read, with the first few
// reasons: the graph has no current syntax for them.
func (s *parseStats) warning() string {
	n := s.skipped.Load() - s.generated.Load()
	if n == 0 {
		return ""
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	var examples []string
	for _, f := range s.skippedFiles {
		if len(examples) == 3 {
			break
		}
		if !f.Generated {
			examples = append(examples, shorten(f.Path, 160)+" ("+shorten(f.Reason, 120)+")")
		}
	}
	return fmt.Sprintf("%d source files could not be parsed, so the graph keeps only what an earlier commit showed of them, if anything: %s.", n, strings.Join(examples, "; "))
}

func (s *parseStats) skip(path, reason string) { s.record(skippedFile{Path: path, Reason: reason}) }

func (s *parseStats) record(f skippedFile) {
	s.skipped.Add(1)
	if f.Generated {
		s.generated.Add(1)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.skippedFiles) < maxSkippedFiles {
		s.skippedFiles = append(s.skippedFiles, f)
	}
}

// parseTimeout bounds one file's parse: a pathological file is skipped.
const parseTimeout = 2 * time.Minute

// parserCrash is a parser panic, recovered: the file is skipped.
type parserCrash struct{ value any }

func (c *parserCrash) Error() string { return fmt.Sprintf("the parser crashed: %v", c.value) }

// parseOne parses one file within parseTimeout, turning a parser panic
// into an error so one file cannot take the worker down.
func parseOne(ctx context.Context, worker *parser.Worker, input parser.Input) (file ir.SourceFile, err error) {
	ctx, cancel := context.WithTimeout(ctx, parseTimeout)
	defer cancel()
	defer func() {
		if r := recover(); r != nil {
			err = &parserCrash{value: r}
		}
	}()
	file, err = worker.Parse(ctx, input)
	if err != nil && ctx.Err() != nil && errors.Is(ctx.Err(), context.DeadlineExceeded) {
		err = fmt.Errorf("parsing took longer than %s", parseTimeout)
	}
	return file, err
}

type sourceSink interface {
	PutSource(ctx context.Context, repositoryID, sha256 string, data []byte) (bool, error)
}

// parseAffected fills the syntax cache for every affected file and retains
// each file's bytes in the content store. A file the parser cannot handle —
// above its byte limit, beyond its structural limits, not UTF-8, rejected by
// IR validation, crashing it, or taking longer than parseTimeout — is
// skipped and reported, never the run; dispatch and I/O failures abort it.
func (p *Pipeline) parseAffected(ctx context.Context, checkoutPath, repoID string, files []semantic.SourceInput, profiles map[string]parser.Profile, sets map[string]bc.SourceSet, descriptorKey string, cache *localindex.SyntaxCache, sink sourceSink, stats *parseStats) ([]semantic.SourceInput, error) {
	root, err := os.OpenRoot(checkoutPath)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	group, workCtx := errgroup.WithContext(ctx)
	jobs := make(chan semantic.SourceInput)
	group.Go(func() error {
		defer close(jobs)
		for _, in := range files {
			select {
			case jobs <- in:
			case <-workCtx.Done():
				return workCtx.Err()
			}
		}
		return nil
	})
	skipped := make(chan ir.FileID, len(files))
	for range p.workers {
		group.Go(func() (err error) {
			// A panic here would take the whole worker process and every
			// run it holds down with it; it fails this run instead.
			defer func() {
				if r := recover(); r != nil {
					err = fmt.Errorf("ingestion: parse worker panicked: %v", r)
				}
			}()
			worker := p.config.Dispatcher.NewWorker()
			defer func() { err = errors.Join(err, worker.Close(context.Background())) }()
			for in := range jobs {
				src := in.Source
				profile, exists := profiles[src.SourceSetID]
				if !exists || profile.Language != src.Language || profile.Version != src.LanguageVersion {
					return fmt.Errorf("ingestion: source %s does not match its source-set profile", src.Path)
				}
				if src.SizeBytes > p.config.ParserLimits.MaxSourceBytes {
					skip := skippedFile{Path: src.Path, Reason: fmt.Sprintf("%d bytes exceed the %d-byte source limit", src.SizeBytes, p.config.ParserLimits.MaxSourceBytes)}
					if head, tail, err := discovery.SniffSource(workCtx, root, src, 64<<10, 1<<10); err == nil {
						if why := oversizedGenerated(head, tail); why != "" {
							skip.Reason, skip.Generated = why+"; "+skip.Reason, true
						}
					}
					stats.record(skip)
					skipped <- src.FileID
					stats.fileDone()
					continue
				}
				content, err := discovery.ReadSource(workCtx, root, src, p.config.ParserLimits.MaxSourceBytes)
				if err != nil {
					return fmt.Errorf("ingestion: read %s: %w", src.Path, err)
				}
				if sink != nil {
					if _, err := sink.PutSource(workCtx, repoID, src.ContentSHA256, content); err != nil {
						return fmt.Errorf("ingestion: retain source %s: %w", src.Path, err)
					}
					stats.uploaded.Add(1)
				}
				key := localindex.SyntaxKey(descriptorKey, sets[src.SourceSetID], in)
				if _, hit, err := cache.Get(workCtx, src.ContentSHA256, key); err != nil {
					return err
				} else if hit {
					stats.cached.Add(1)
					stats.fileDone()
					continue
				}
				file, err := parseOne(workCtx, worker, parser.Input{Source: src, Content: content, Options: profile.Options, Limits: p.config.ParserLimits})
				if err != nil {
					if workCtx.Err() != nil {
						return workCtx.Err()
					}
					var crash *parserCrash
					if errors.As(err, &crash) {
						// A parser that panicked may be left inconsistent.
						_ = worker.Close(context.Background())
						worker = p.config.Dispatcher.NewWorker()
					}
					stats.record(skippedFile{Path: src.Path, Reason: err.Error(), Generated: errors.Is(err, parser.ErrGeneratedSource)})
					skipped <- src.FileID
					stats.fileDone()
					continue
				}
				if err := cache.Put(workCtx, src.ContentSHA256, key, file); err != nil {
					return fmt.Errorf("ingestion: cache syntax %s: %w", src.Path, err)
				}
				stats.parsed.Add(1)
				stats.fileDone()
			}
			return nil
		})
	}
	if err := group.Wait(); err != nil {
		return nil, err
	}
	close(skipped)
	drop := map[ir.FileID]bool{}
	for id := range skipped {
		drop[id] = true
	}
	if len(drop) == 0 {
		return files, nil
	}
	kept := files[:0:0]
	for _, in := range files {
		if !drop[in.Source.FileID] {
			kept = append(kept, in)
		}
	}
	return kept, nil
}

// descriptorDigest fingerprints the parser implementations of every
// registered language and the IR schema. Combined with the file's language
// and syntax profile by localindex.SyntaxKey it keys the syntax cache, so a
// changed parser for any language never reuses stale results.
func descriptorDigest(d *parser.Dispatcher) string {
	return digestOf(struct {
		Registry, IR string
	}{d.Digest(), ir.SchemaVersion})
}

// Markers generators write in a comment near the top of their output.
var generationMarkers = [][]byte{[]byte("@generated"), []byte("DO NOT EDIT"), []byte("Code generated by"), []byte("automatically generated"), []byte("auto-generated")}

// oversizedGenerated reports why a file past the source limit is generated
// output rather than source the graph is missing, judged on its first 64 KiB
// and last KiB: a generation marker on a comment line, a source-map comment,
// a UMD wrapper, or minified lines (a thousand bytes each on average, or one
// of 50,000). A file that shows none is left out as a parse gap.
func oversizedGenerated(head, tail []byte) string {
	start := head[:min(len(head), 4096)]
	for _, line := range bytes.Split(start, []byte("\n")) {
		trimmed := bytes.TrimLeft(line, " \t")
		if !(bytes.HasPrefix(trimmed, []byte("//")) || bytes.HasPrefix(trimmed, []byte("/*")) || bytes.HasPrefix(trimmed, []byte("*")) || bytes.HasPrefix(trimmed, []byte("#"))) {
			continue
		}
		for _, marker := range generationMarkers {
			if bytes.Contains(trimmed, marker) {
				return "generated: marker " + string(marker)
			}
		}
	}
	if bytes.Contains(tail, []byte("//# sourceMappingURL=")) || bytes.Contains(tail, []byte("/*# sourceMappingURL=")) {
		return "generated: compiled output with a source map comment"
	}
	if first := head[:min(len(head), 1024)]; bytes.Contains(first, []byte("typeof exports")) && bytes.Contains(first, []byte("define.amd")) {
		return "generated: UMD bundle"
	}
	lines, longest, from := bytes.Count(head, []byte("\n")), 0, 0
	for i, b := range head {
		if b == '\n' {
			longest, from = max(longest, i-from), i+1
		}
	}
	longest = max(longest, len(head)-from)
	if lines == 0 || len(head)/max(lines, 1) >= 1000 || longest >= 50000 {
		return fmt.Sprintf("minified: %d lines in its first %d bytes, longest %d", lines, len(head), longest)
	}
	return ""
}
