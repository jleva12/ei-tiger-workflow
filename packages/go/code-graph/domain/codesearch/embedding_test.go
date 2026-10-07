package codesearch

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

// fakeProvider embeds deterministically from the input text and records
// every request it receives.
type fakeProvider struct {
	dims     int
	maxBytes int
	fail     error
	mu       sync.Mutex
	requests [][]string
}

func (f *fakeProvider) Model() string      { return "fake" }
func (f *fakeProvider) Dimensions() int    { return f.dims }
func (f *fakeProvider) MaxInputBytes() int { return f.maxBytes }
func (f *fakeProvider) Embed(_ context.Context, in []string) ([][]float64, error) {
	f.mu.Lock()
	f.requests = append(f.requests, append([]string(nil), in...))
	f.mu.Unlock()
	if f.fail != nil {
		return nil, f.fail
	}
	if len(in) == 0 || len(in) > MaxInputsPerRequest {
		return nil, fmt.Errorf("batch of %d", len(in))
	}
	out := make([][]float64, len(in))
	for i, s := range in {
		sum := sha256.Sum256([]byte(s))
		v := make([]float64, f.dims)
		for j := range v {
			v[j] = float64(sum[j%len(sum)]) + 1
		}
		out[i] = v
	}
	return out, nil
}

// fakeStore serves fixed pages by cursor and records writes.
type fakeStore struct {
	pages   [][]Document
	failPut error
	mu      sync.Mutex
	written []Embedding
	commits int
}

func (s *fakeStore) SearchDocuments(_ context.Context, _, _ string, _ int, cursor string) (DocumentPage, error) {
	i := 0
	if cursor != "" {
		i, _ = strconv.Atoi(cursor)
	}
	if i >= len(s.pages) {
		return DocumentPage{}, nil
	}
	next := ""
	if i+1 < len(s.pages) {
		next = strconv.Itoa(i + 1)
	}
	return DocumentPage{Generation: 1, Documents: s.pages[i], NextCursor: next}, nil
}

func (s *fakeStore) PutEmbeddings(_ context.Context, _, _ string, _ uint64, rows []Embedding) error {
	if s.failPut != nil {
		return s.failPut
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.commits++
	s.written = append(s.written, rows...)
	return nil
}

func fixtureDocs(n int, prefix string) []Document {
	docs := make([]Document, n)
	for i := range docs {
		// Lengths vary so some documents need several windows of the
		// provider's tiny window size and batches straddle documents.
		text := strings.Repeat(fmt.Sprintf("%s%d ", prefix, i), 1+i%5)
		docs[i] = Document{NodeID: fmt.Sprintf("%s-%d", prefix, i), Text: text}
	}
	return docs
}

func TestIndexConcurrentBatchesAcrossDocumentsAndMatchesEmbedText(t *testing.T) {
	p := &fakeProvider{dims: 4, maxBytes: 8}
	st := &fakeStore{pages: [][]Document{fixtureDocs(40, "a"), fixtureDocs(70, "b"), fixtureDocs(3, "c")}}
	var progress []uint64
	report, err := IndexConcurrent(context.Background(), st, p, "repo", IndexOptions{Concurrency: 3, Progress: func(n uint64) { progress = append(progress, n) }})
	if err != nil {
		t.Fatal(err)
	}
	if report.Embedded != 113 || report.Pages != 3 || len(st.written) != 113 {
		t.Fatalf("report %+v, written %d", report, len(st.written))
	}
	windowsTotal := 0
	for _, page := range st.pages {
		for _, d := range page {
			ws, err := windows(d.Text, p.maxBytes)
			if err != nil {
				t.Fatal(err)
			}
			windowsTotal += len(ws)
		}
	}
	if report.Inputs != uint64(windowsTotal) || windowsTotal <= 113 {
		t.Fatalf("inputs %d, windows %d: long documents must span several windows", report.Inputs, windowsTotal)
	}
	// Requests are packed to the batch limit within each page.
	wantRequests := 0
	for _, page := range st.pages {
		n := 0
		for _, d := range page {
			ws, _ := windows(d.Text, p.maxBytes)
			n += len(ws)
		}
		wantRequests += (n + MaxInputsPerRequest - 1) / MaxInputsPerRequest
	}
	if int(report.Requests) != wantRequests || len(p.requests) != wantRequests {
		t.Fatalf("requests %d (provider saw %d), want %d", report.Requests, len(p.requests), wantRequests)
	}
	for _, r := range p.requests {
		if len(r) == 0 || len(r) > MaxInputsPerRequest {
			t.Fatalf("request of %d inputs", len(r))
		}
	}
	// A batched document's vector equals the single-document transform.
	for _, row := range st.written {
		want, err := EmbedText(context.Background(), p, row.Document.Text)
		if err != nil {
			t.Fatal(err)
		}
		for j := range want {
			if diff := want[j] - row.Vector[j]; diff > 1e-12 || diff < -1e-12 {
				t.Fatalf("%s: batched vector differs from EmbedText at %d: %v vs %v", row.Document.NodeID, j, row.Vector[j], want[j])
			}
		}
	}
	// Writes are chunked and progress reports durable totals in order.
	if st.commits != 2+3+1 {
		t.Fatalf("commits %d, want 6 chunks of at most %d rows", st.commits, embeddingRowsPerCommit)
	}
	if len(progress) != 3 || progress[0] != 40 || progress[1] != 110 || progress[2] != 113 {
		t.Fatalf("progress %v", progress)
	}
}

func TestIndexConcurrentEmptyAndErrors(t *testing.T) {
	ctx := context.Background()
	p := &fakeProvider{dims: 4, maxBytes: 8}
	report, err := IndexConcurrent(ctx, &fakeStore{}, p, "repo", IndexOptions{})
	if err != nil || report.Embedded != 0 || report.Requests != 0 {
		t.Fatalf("empty pass: %+v %v", report, err)
	}
	boom := errors.New("boom")
	_, err = IndexConcurrent(ctx, &fakeStore{pages: [][]Document{fixtureDocs(5, "a")}}, &fakeProvider{dims: 4, maxBytes: 8, fail: boom}, "repo", IndexOptions{})
	if !errors.Is(err, boom) {
		t.Fatalf("provider failure must surface: %v", err)
	}
	_, err = IndexConcurrent(ctx, &fakeStore{pages: [][]Document{fixtureDocs(5, "a")}, failPut: boom}, p, "repo", IndexOptions{})
	if !errors.Is(err, boom) || !strings.Contains(err.Error(), "store embeddings") {
		t.Fatalf("store failure must surface: %v", err)
	}
	report, err = IndexConcurrent(ctx, &fakeStore{pages: [][]Document{{{NodeID: "bad", Text: "\xff"}, {NodeID: "good", Text: "fine"}}}}, p, "repo", IndexOptions{})
	if err != nil || report.Embedded != 1 || report.Skipped != 1 || strings.Join(report.SkippedNodes, ",") != "bad" {
		t.Fatalf("a document that cannot be embedded is skipped and named: %+v %v", report, err)
	}
}

// refusingProvider refuses, as a provider refuses content, every request
// that carries a poisoned text, or every request at all.
type refusingProvider struct {
	fakeProvider
	everything bool
	refused    atomic.Int64
}

func (f *refusingProvider) Embed(ctx context.Context, in []string) ([][]float64, error) {
	for _, s := range in {
		if f.everything || strings.Contains(s, "poison") {
			f.mu.Lock()
			f.requests = append(f.requests, in)
			f.mu.Unlock()
			f.refused.Add(1)
			return nil, &ProviderError{Status: 400}
		}
	}
	return f.fakeProvider.Embed(ctx, in)
}

func TestARefusedDocumentIsSkippedAndTheRestEmbedded(t *testing.T) {
	docs := fixtureDocs(40, "doc")
	docs[17].Text = "poison " + docs[17].Text
	docs[31].Text = "poison " + docs[31].Text
	store := &fakeStore{pages: [][]Document{docs}}
	p := &refusingProvider{fakeProvider: fakeProvider{dims: 4, maxBytes: 7500}}
	report, err := IndexConcurrent(context.Background(), store, p, "repo", IndexOptions{Concurrency: 1})
	if err != nil || report.Embedded != 38 || report.Skipped != 2 || len(store.written) != 38 {
		t.Fatalf("refusals: %+v %v", report, err)
	}
	for _, id := range report.SkippedNodes {
		if id != docs[17].NodeID && id != docs[31].NodeID {
			t.Fatalf("skipped %s", id)
		}
	}
	// Halving finds each refused text in log2(16) steps; nothing is resent
	// more than that.
	if len(p.requests) > 3+2*(1+2*4) {
		t.Fatalf("%d requests for 40 documents", len(p.requests))
	}
}

func TestAProviderThatRefusesEverythingFailsThePass(t *testing.T) {
	store := &fakeStore{pages: [][]Document{fixtureDocs(40, "doc")}}
	p := &refusingProvider{fakeProvider: fakeProvider{dims: 4, maxBytes: 7500}, everything: true}
	report, err := IndexConcurrent(context.Background(), store, p, "repo", IndexOptions{Concurrency: 1})
	if err == nil || !strings.Contains(err.Error(), "configuration") || report.Embedded != 0 || report.Skipped != 0 {
		t.Fatalf("misconfigured provider: %+v %v", report, err)
	}
	if p.refused.Load() > 2 {
		t.Fatalf("%d refused requests before giving up", p.refused.Load())
	}
}

func TestIndexConcurrentCanceledBeforeStartFails(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := IndexConcurrent(ctx, &fakeStore{}, &fakeProvider{dims: 4, maxBytes: 8}, "repo", IndexOptions{})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled empty pass returned %v", err)
	}
}

type partialWriteStore struct{ fakeStore }

func (s *partialWriteStore) PutEmbeddings(ctx context.Context, repo, model string, generation uint64, rows []Embedding) error {
	if s.commits == 1 {
		return io.ErrUnexpectedEOF
	}
	return s.fakeStore.PutEmbeddings(ctx, repo, model, generation, rows)
}

func TestIndexReportsDurablePartialPage(t *testing.T) {
	store := &partialWriteStore{fakeStore: fakeStore{pages: [][]Document{fixtureDocs(40, "partial")}}}
	report, err := IndexConcurrent(context.Background(), store, &fakeProvider{dims: 4, maxBytes: 7500}, "repo", IndexOptions{})
	if !errors.Is(err, io.ErrUnexpectedEOF) || report.Embedded != 32 || len(store.written) != 32 {
		t.Fatalf("partial commit: %+v %v", report, err)
	}
}
