package codesearch

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"golang.org/x/sync/errgroup"
)

// DefaultDimensions is the embedding dimension used when none is configured:
// the native width of text-embedding-3-large. The Spanner embedding column
// and its vector index are created with a fixed dimension, so a provider must
// produce exactly the dimension the database was provisioned with.
const DefaultDimensions = 3072

// MaxInputsPerRequest is the most texts one embedding request carries.
const MaxInputsPerRequest = 16

// DefaultIndexConcurrency is the number of embedding requests in flight when
// IndexOptions leaves Concurrency unset.
const DefaultIndexConcurrency = 8

// embeddingRowsPerCommit bounds one PutEmbeddings call: 32 vectors of up to
// 4096 float32 stay well inside Spanner's per-commit limits.
const embeddingRowsPerCommit = 32

// Provider must use the same model and dimensions for documents and queries.
type Provider interface {
	Model() string
	Dimensions() int
	MaxInputBytes() int
	Embed(context.Context, []string) ([][]float64, error)
}

type Embedding struct {
	Document Document
	Vector   []float64
}
type DocumentPage struct {
	Generation uint64
	Documents  []Document
	NextCursor string
}
type IndexStore interface {
	SearchDocuments(context.Context, string, string, int, string) (DocumentPage, error)
	PutEmbeddings(context.Context, string, string, uint64, []Embedding) error
}

// IndexOptions tune IndexConcurrent.
type IndexOptions struct {
	// Concurrency bounds the embedding requests in flight; each request
	// carries up to MaxInputsPerRequest texts. Default DefaultIndexConcurrency.
	Concurrency int
	// Logger receives one line per page, per pass and per request at Debug,
	// and refusals and a stopped pass at Warn: the caller reports the pass's
	// progress. nil disables logging.
	Logger *slog.Logger
	// Progress, when set, is called from the writer after each page's
	// embeddings are durable, with the running total.
	Progress func(embedded uint64)
}

// IndexReport summarizes one embedding pass.
type IndexReport struct {
	Embedded uint64 // documents whose embeddings were written
	Pages    uint64
	Requests uint64 // embedding requests sent
	Inputs   uint64 // windows embedded (a long document spans several)
	Bytes    uint64 // bytes of text sent to the provider
	// Skipped counts documents left without an embedding because the
	// provider refused their text; SkippedNodes names the first few. They
	// stay findable by name and text, and the next pass tries them again.
	Skipped      uint64
	SkippedNodes []string
	Duration     time.Duration
}

// maxSkippedNodes bounds the node IDs an IndexReport names.
const maxSkippedNodes = 16

// refusals tracks, across a pass, whether the provider accepts anything at
// all. Refusals are blamed on single documents only once it has: a provider
// that refuses everything is misconfigured (a model or dimension it does not
// serve), and skipping every document would only hide that.
type refusals struct {
	accepted atomic.Uint64
	once     sync.Once
	probe    error
}

// providerWorks returns nil once the provider has accepted an input this
// pass. Before that, it sends one trivial input, once per pass, and returns
// that request's failure.
func (r *refusals) providerWorks(ctx context.Context, p Provider) error {
	if r.accepted.Load() > 0 {
		return nil
	}
	r.once.Do(func() {
		out, err := p.Embed(ctx, []string{"func main() {}"})
		if err == nil && len(out) != 1 {
			err = errors.New("code search: embedding count mismatch")
		}
		if err == nil {
			err = ValidateVector(out[0], p.Dimensions())
		}
		if r.probe = err; err == nil {
			r.accepted.Add(1)
		}
	})
	return r.probe
}

// IndexConcurrent embeds every document whose stored embedding is missing or
// stale for the live generation. Documents are read in pages; each page's
// windows are packed MaxInputsPerRequest to a request with Concurrency
// requests in flight, and the page's vectors are written by a separate
// goroutine while the next page is fetched and embedded. The pass is
// idempotent and restartable: each page re-reads what is still missing, and a
// page is durable before Progress reports it.
func IndexConcurrent(ctx context.Context, s IndexStore, p Provider, repo string, o IndexOptions) (IndexReport, error) {
	start := time.Now()
	var report IndexReport
	if err := validateProvider(p); err != nil {
		return report, err
	}
	concurrency := o.Concurrency
	if concurrency < 1 {
		concurrency = DefaultIndexConcurrency
	}
	log := o.Logger
	if log == nil {
		log = slog.New(slog.DiscardHandler)
	}
	type batch struct {
		generation uint64
		rows       []Embedding
	}
	var written atomic.Uint64
	writes := make(chan batch, 1)
	group, groupCtx := errgroup.WithContext(ctx)
	group.Go(func() (err error) {
		defer recoverInto(&err)
		for b := range writes {
			for i := 0; i < len(b.rows); i += embeddingRowsPerCommit {
				end := min(i+embeddingRowsPerCommit, len(b.rows))
				if err := s.PutEmbeddings(groupCtx, repo, p.Model(), b.generation, b.rows[i:end]); err != nil {
					return fmt.Errorf("store embeddings: %w", err)
				}
				written.Add(uint64(end - i))
			}
			n := written.Load()
			if o.Progress != nil {
				o.Progress(n)
			}
		}
		return nil
	})
	var embedded uint64
	var seen refusals
	// The next page is read while the current one is embedded.
	type fetched struct {
		page DocumentPage
		err  error
	}
	fetch := func(cursor string) <-chan fetched {
		out := make(chan fetched, 1)
		go func() {
			var f fetched
			defer func() {
				if r := recover(); r != nil {
					f.err = fmt.Errorf("code search: reading documents panicked: %v", r)
				}
				out <- f
			}()
			f.page, f.err = s.SearchDocuments(groupCtx, repo, p.Model(), p.Dimensions(), cursor)
		}()
		return out
	}
	cursor := ""
	pending := fetch(cursor)
	var loopErr error
	for loopErr == nil && groupCtx.Err() == nil {
		f := <-pending
		if f.err != nil {
			loopErr = f.err
			break
		}
		page := f.page
		if len(page.Documents) == 0 {
			break
		}
		if page.NextCursor != "" && page.NextCursor != cursor {
			pending = fetch(page.NextCursor)
		}
		pageStart := time.Now()
		rows, stats, err := embedPage(groupCtx, p, page.Documents, concurrency, log, &seen)
		if err != nil {
			loopErr = err
			break
		}
		report.Pages++
		report.Requests += stats.requests
		report.Inputs += stats.inputs
		report.Bytes += stats.bytes
		report.Skipped += uint64(len(stats.skipped))
		for _, id := range stats.skipped {
			if len(report.SkippedNodes) < maxSkippedNodes {
				report.SkippedNodes = append(report.SkippedNodes, id)
			}
		}
		embedded += uint64(len(rows))
		log.Debug("embedding page", "documents", len(rows), "skipped", len(stats.skipped), "requests", stats.requests, "inputs", stats.inputs, "bytes", stats.bytes, "elapsed_ms", time.Since(pageStart).Milliseconds(), "embedded", embedded)
		select {
		case writes <- batch{page.Generation, rows}:
		case <-groupCtx.Done():
			loopErr = groupCtx.Err()
		}
		if page.NextCursor == "" {
			break
		}
		if page.NextCursor == cursor {
			loopErr = errors.New("code search: embedding pass did not advance")
		}
		cursor = page.NextCursor
	}
	close(writes)
	// Keep both errors: a permanent provider failure must not be hidden by
	// a concurrent canceled write. A canceled empty pass is not success.
	err := errors.Join(group.Wait(), loopErr, ctx.Err())
	report.Embedded = written.Load()
	report.Duration = time.Since(start)
	attrs := []any{"embedded", report.Embedded, "skipped", report.Skipped, "pages", report.Pages, "requests", report.Requests, "inputs", report.Inputs, "bytes", report.Bytes, "elapsed_ms", report.Duration.Milliseconds()}
	if err != nil {
		log.Warn("embedding pass stopped", append(attrs, "error", err)...)
		return report, err
	}
	log.Debug("embedding pass complete", attrs...)
	return report, nil
}

type pageStats struct {
	requests, inputs, bytes uint64
	skipped                 []string // node IDs left without an embedding
}

// embedPage windows every document of a page, packs the windows across
// documents into requests of MaxInputsPerRequest, sends them with the given
// concurrency and combines each document's window vectors as EmbedText does.
//
// A request the provider refuses for its content is split in halves until
// the refused window is alone; that window's document is skipped and every
// other document of the request is still embedded. Any other failure ends
// the page.
func embedPage(ctx context.Context, p Provider, docs []Document, concurrency int, log *slog.Logger, seen *refusals) ([]Embedding, pageStats, error) {
	type input struct {
		doc, window int
		text        string
	}
	var mu sync.Mutex
	skipped := map[int]string{}
	skip := func(doc int, err error) {
		mu.Lock()
		defer mu.Unlock()
		if _, ok := skipped[doc]; !ok {
			skipped[doc] = err.Error()
			log.Warn("document left without an embedding", "node", docs[doc].NodeID, "error", err)
		}
	}
	windowsOf := make([][]string, len(docs))
	var inputs []input
	for i, d := range docs {
		ws, err := windows(d.Text, p.MaxInputBytes())
		if err != nil {
			skip(i, err)
			continue
		}
		windowsOf[i] = ws
		for j, w := range ws {
			inputs = append(inputs, input{i, j, w})
		}
	}
	vectors := make([][][]float64, len(docs))
	for i := range vectors {
		vectors[i] = make([][]float64, len(windowsOf[i]))
	}
	var requests, sent, bytes atomic.Uint64
	var send func(ctx context.Context, chunk []input) error
	send = func(ctx context.Context, chunk []input) error {
		if err := ctx.Err(); err != nil {
			return err // another request of the page failed
		}
		texts := make([]string, len(chunk))
		size := 0
		for k, in := range chunk {
			texts[k] = in.text
			size += len(in.text)
		}
		t0 := time.Now()
		out, err := p.Embed(ctx, texts)
		requests.Add(1)
		if err == nil && len(out) != len(texts) {
			err = errors.New("code search: embedding count mismatch")
		}
		if err != nil {
			if !Rejected(err) || ctx.Err() != nil {
				return fmt.Errorf("embed %s: %w", docs[chunk[0].doc].NodeID, err)
			}
			if probe := seen.providerWorks(ctx, p); probe != nil {
				return fmt.Errorf("embed %s: the provider refuses even a trivial input, so its configuration is wrong: %w", docs[chunk[0].doc].NodeID, probe)
			}
			if len(chunk) > 1 {
				half := len(chunk) / 2
				if err := send(ctx, chunk[:half]); err != nil {
					return err
				}
				return send(ctx, chunk[half:])
			}
			skip(chunk[0].doc, err)
			return nil
		}
		seen.accepted.Add(uint64(len(texts)))
		for k, v := range out {
			if err := ValidateVector(v, p.Dimensions()); err != nil {
				skip(chunk[k].doc, err)
				continue
			}
			vectors[chunk[k].doc][chunk[k].window] = v
		}
		sent.Add(uint64(len(texts)))
		bytes.Add(uint64(size))
		log.Debug("embedding request", "inputs", len(texts), "bytes", size, "elapsed_ms", time.Since(t0).Milliseconds(), "first", docs[chunk[0].doc].NodeID)
		return nil
	}
	group, gctx := errgroup.WithContext(ctx)
	group.SetLimit(concurrency)
	for start := 0; start < len(inputs); start += MaxInputsPerRequest {
		chunk := inputs[start:min(start+MaxInputsPerRequest, len(inputs))]
		group.Go(func() (err error) {
			defer recoverInto(&err)
			return send(gctx, chunk)
		})
	}
	stats := pageStats{}
	if err := group.Wait(); err != nil {
		return nil, stats, err
	}
	stats = pageStats{requests: requests.Load(), inputs: sent.Load(), bytes: bytes.Load()}
	rows := make([]Embedding, 0, len(docs))
	for i, d := range docs {
		if _, ok := skipped[i]; ok {
			stats.skipped = append(stats.skipped, d.NodeID)
			continue
		}
		v, err := combine(windowsOf[i], vectors[i], p.Dimensions())
		if err != nil {
			log.Warn("document left without an embedding", "node", d.NodeID, "error", err)
			stats.skipped = append(stats.skipped, d.NodeID)
			continue
		}
		rows = append(rows, Embedding{Document: d, Vector: v})
	}
	return rows, stats, nil
}

// recoverInto turns a panic in an embedding goroutine into its error, so
// it fails the pass instead of the process.
func recoverInto(err *error) {
	if r := recover(); r != nil {
		*err = fmt.Errorf("code search: embedding panicked: %v", r)
	}
}

func validateProvider(p Provider) error {
	if p == nil || p.Model() == "" || len(p.Model()) > 256 || p.Dimensions() < 1 || p.Dimensions() > 4096 || p.MaxInputBytes() < 4 || p.MaxInputBytes() > 8000 {
		return errors.New("code search: invalid embedding provider")
	}
	return nil
}

// EmbedText covers every UTF-8 byte in bounded windows and length-weights their
// embeddings. Long bodies are never silently truncated. The same transform is
// applied to natural-language queries and source documents.
func EmbedText(ctx context.Context, p Provider, text string) ([]float64, error) {
	if err := validateProvider(p); err != nil {
		return nil, err
	}
	ws, err := windows(text, p.MaxInputBytes())
	if err != nil {
		return nil, err
	}
	vectors := make([][]float64, 0, len(ws))
	for start := 0; start < len(ws); start += MaxInputsPerRequest {
		chunk := ws[start:min(start+MaxInputsPerRequest, len(ws))]
		out, err := p.Embed(ctx, chunk)
		if err != nil {
			return nil, err
		}
		if len(out) != len(chunk) {
			return nil, errors.New("code search: embedding count mismatch")
		}
		vectors = append(vectors, out...)
	}
	return combine(ws, vectors, p.Dimensions())
}

// windows splits text into pieces of at most maxBytes on rune boundaries so
// that every byte is embedded exactly once.
func windows(text string, maxBytes int) ([]string, error) {
	if text == "" || !utf8.ValidString(text) {
		return nil, errors.New("code search: empty or invalid UTF-8 document")
	}
	var out []string
	for len(text) > 0 {
		n := min(len(text), maxBytes)
		for n < len(text) && !utf8.RuneStart(text[n]) {
			n--
		}
		out = append(out, text[:n])
		text = text[n:]
	}
	return out, nil
}

// combine validates one vector per window and returns their unit-length,
// byte-length-weighted mean: the embedding of the whole text.
func combine(ws []string, vectors [][]float64, dimensions int) ([]float64, error) {
	if len(vectors) != len(ws) || len(ws) == 0 {
		return nil, errors.New("code search: embedding count mismatch")
	}
	mean := make([]float64, dimensions)
	for i, v := range vectors {
		if err := ValidateVector(v, dimensions); err != nil {
			return nil, err
		}
		norm := 0.0
		for _, x := range v {
			norm += x * x
		}
		weight := float64(len(ws[i])) / math.Sqrt(norm)
		for j, x := range v {
			mean[j] += x * weight
		}
	}
	if err := ValidateVector(mean, dimensions); err != nil {
		return nil, err
	}
	norm := 0.0
	for _, x := range mean {
		norm += x * x
	}
	for i := range mean {
		mean[i] /= math.Sqrt(norm)
	}
	return mean, nil
}

func ValidateVector(v []float64, dimensions int) error {
	if dimensions < 1 || dimensions > 4096 || len(v) != dimensions {
		return errors.New("code search: embedding dimension mismatch")
	}
	norm := 0.0
	for _, x := range v {
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return errors.New("code search: non-finite embedding")
		}
		norm += x * x
	}
	if norm == 0 || math.IsInf(norm, 0) {
		return fmt.Errorf("code search: invalid embedding norm")
	}
	return nil
}
