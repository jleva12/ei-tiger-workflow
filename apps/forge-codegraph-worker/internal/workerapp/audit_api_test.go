package workerapp

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

// fakeAudit records the reads it answers.
type fakeAudit struct {
	stats []string
	runs  []string
	page  []deployment.Run
	next  string
	err   error
}

func (f *fakeAudit) Stats(_ context.Context, repo string) (spannerstore.RepositoryStats, error) {
	f.stats = append(f.stats, repo)
	return spannerstore.RepositoryStats{
		RepositoryID: repo, Branch: "main", Generation: 3, CommitSHA: headSHA, RunID: "run-3",
		Nodes: 5, Edges: 2, NodeKinds: map[string]uint64{"class": 2, "method": 3}, EdgeKinds: map[string]uint64{"calls": 2},
		Searchable: 4, Embeddings: spannerstore.EmbeddingStats{Model: "text-embedding-3-large", Dimensions: 1024, Current: 3, Stored: 4},
	}, f.err
}

func (f *fakeAudit) Runs(_ context.Context, repo string, limit int, cursor string) ([]deployment.Run, string, error) {
	f.runs = append(f.runs, fmt.Sprintf("%s|%d|%s", repo, limit, cursor))
	return f.page, f.next, f.err
}

func auditServer(t *testing.T, store *fakeStore, fake *fakeAudit) func(path string) (int, map[string]any) {
	t.Helper()
	server := apiServer(t, store)
	// apiServer builds its own API; rebuild it around this fake.
	server.Config.Handler = (&healthStatus{}).handler(newWorkerAPI(testAdmissionToken, store, &fakeGraph{}, &fakeLinks{}, fake, &fakeSearch{}, slog.New(slog.NewTextHandler(io.Discard, nil))))
	return func(path string) (int, map[string]any) {
		return call(t, server, http.MethodGet, path, testAdmissionToken, "")
	}
}

func TestAuditStatsPassTheTotals(t *testing.T) {
	fake := &fakeAudit{}
	get := auditServer(t, newFakeStore(), fake)
	code, body := get("/v1/repositories/repo:abc_-1/stats")
	if code != http.StatusOK || len(fake.stats) != 1 || fake.stats[0] != "repo:abc_-1" {
		t.Fatalf("stats %d %v %v", code, body, fake.stats)
	}
	if body["repository_id"] != "repo:abc_-1" || body["branch"] != "main" || body["generation"] != float64(3) || body["commit_sha"] != headSHA || body["run_id"] != "run-3" ||
		body["nodes"] != float64(5) || body["edges"] != float64(2) || body["searchable"] != float64(4) {
		t.Fatalf("stats body %v", body)
	}
	if kinds := body["node_kinds"].(map[string]any); kinds["class"] != float64(2) || kinds["method"] != float64(3) || body["edge_kinds"].(map[string]any)["calls"] != float64(2) {
		t.Fatalf("kinds %v %v", body["node_kinds"], body["edge_kinds"])
	}
	embeddings := body["embeddings"].(map[string]any)
	if embeddings["model"] != "text-embedding-3-large" || embeddings["dimensions"] != float64(1024) || embeddings["current"] != float64(3) || embeddings["stored"] != float64(4) {
		t.Fatalf("embeddings %v", embeddings)
	}
}

func TestAuditRunsPageNewestFirst(t *testing.T) {
	store := newFakeStore()
	store.repos["repo:1"] = deployment.Repository{RepositoryID: "repo:1"}
	finished := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	fake := &fakeAudit{next: "c2", page: []deployment.Run{
		{Key: deployment.RunKey{RepositoryID: "repo:1", RunID: "run-2"}, Phase: deployment.Succeeded, Generation: 2, FinishedAt: &finished,
			Metrics: &deployment.Metrics{Files: 10, Nodes: 40, Durations: map[string]int64{"parse": 1200}},
			Index:   &deployment.IndexState{Model: "text-embedding-3-large", Dimensions: 1024, Status: deployment.IndexComplete, Embedded: 30}},
		{Key: deployment.RunKey{RepositoryID: "repo:1", RunID: "run-1"}, Phase: deployment.Failed, ErrorCode: "permanent_failure", ErrorMessage: "ingestion: build context: Maven prepare failed"},
	}}
	get := auditServer(t, store, fake)

	code, body := get("/v1/repositories/repo:1/runs")
	if code != http.StatusOK || fake.runs[0] != "repo:1|10|" || body["next_cursor"] != "c2" {
		t.Fatalf("runs %d %v %v", code, body, fake.runs)
	}
	runs := body["runs"].([]any)
	first := runs[0].(map[string]any)
	if len(runs) != 2 || first["key"].(map[string]any)["run_id"] != "run-2" || first["phase"] != "SUCCEEDED" ||
		first["metrics"].(map[string]any)["nodes"] != float64(40) || first["index"].(map[string]any)["status"] != "COMPLETE" {
		t.Fatalf("runs body %v", body)
	}
	// A failed run says why; a successful one has no message.
	if _, ok := first["error_message"]; ok || runs[1].(map[string]any)["error_message"] != "ingestion: build context: Maven prepare failed" {
		t.Fatalf("error messages %v", runs)
	}
	if code, _ = get("/v1/repositories/repo:1/runs?limit=50&cursor=c2"); code != http.StatusOK || fake.runs[1] != "repo:1|50|c2" {
		t.Fatalf("second page %d %v", code, fake.runs)
	}

	// The last page has no cursor, and no runs is an empty list.
	fake.page, fake.next = nil, ""
	code, body = get("/v1/repositories/repo:1/runs")
	if runs, ok := body["runs"].([]any); code != http.StatusOK || !ok || len(runs) != 0 {
		t.Fatalf("no runs must be an empty list: %d %v", code, body)
	}
	if _, ok := body["next_cursor"]; ok {
		t.Fatalf("no next cursor at the end: %v", body)
	}
	// A repository never registered has no runs page at all.
	if code, body = get("/v1/repositories/repo:unknown/runs"); code != http.StatusNotFound || body["code"] != "not_found" {
		t.Fatalf("unknown repository: %d %v", code, body)
	}
}

func TestAuditReadsRejectBadInput(t *testing.T) {
	fake := &fakeAudit{}
	get := auditServer(t, newFakeStore(), fake)
	repo := "/v1/repositories/repo:1"
	for _, path := range []string{
		repo + "/runs?limit=0",
		repo + "/runs?limit=51",
		repo + "/runs?limit=x",
	} {
		if code, body := get(path); code != http.StatusBadRequest {
			t.Errorf("%s: %d %v", path, code, body)
		}
	}
	if len(fake.runs) != 0 {
		t.Fatal("bad input reached the store")
	}

	for _, c := range []struct {
		err  error
		path string
		want int
	}{
		{fmt.Errorf("%w: cursor signature or scope", deployment.ErrInvalidRequest), repo + "/runs?cursor=forged", http.StatusBadRequest},
		{fmt.Errorf("%w: cursor too long", deployment.ErrLimitExceeded), repo + "/runs?cursor=long", http.StatusBadRequest},
		{fmt.Errorf("%w: repository id", deployment.ErrInvalidRequest), "/v1/repositories/bad%20id/runs", http.StatusBadRequest},
		{fmt.Errorf("%w: repository repo:1", graph.ErrNotFound), repo + "/stats", http.StatusNotFound},
		{fmt.Errorf("%w: repository id", graph.ErrInvalid), "/v1/repositories/bad%20id/stats", http.StatusBadRequest},
	} {
		fake.err = c.err
		if code, body := get(c.path); code != c.want {
			t.Errorf("%s with %v: %d %v", c.path, c.err, code, body)
		}
	}

	// Like every call here, reads need the token.
	server := apiServer(t, newFakeStore())
	for _, path := range []string{repo + "/stats", repo + "/runs"} {
		if code, _ := call(t, server, http.MethodGet, path, "", ""); code != http.StatusUnauthorized {
			t.Fatalf("%s without a token: %d", path, code)
		}
	}
}
