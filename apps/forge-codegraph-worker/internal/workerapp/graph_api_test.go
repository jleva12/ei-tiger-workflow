package workerapp

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"testing"

	"ei-aitiger-codegraph/agentquery"
	"ei-aitiger-codegraph/pkg/graph"
)

// fakeGraph records the queries it answers.
type fakeGraph struct {
	views     []agentquery.GraphViewRequest
	neighbors []graph.NeighborQuery
	symbols   []string
	sources   []agentquery.SourceRequest
	err       error
}

func (f *fakeGraph) View(_ context.Context, q agentquery.GraphViewRequest) (agentquery.GraphViewResult, error) {
	f.views = append(f.views, q)
	return agentquery.GraphViewResult{RepositoryID: q.RepositoryID, Generation: 7, Nodes: []graph.Version{}, Edges: []graph.Version{}}, f.err
}

func (f *fakeGraph) Neighbors(_ context.Context, q graph.NeighborQuery) (graph.NeighborPage, error) {
	f.neighbors = append(f.neighbors, q)
	return graph.NeighborPage{Generation: 7}, f.err
}

func (f *fakeGraph) Symbols(_ context.Context, repo, name, qualified string, generation uint64, limit int) ([]graph.Version, error) {
	f.symbols = append(f.symbols, fmt.Sprintf("%s|%s|%s|%d|%d", repo, name, qualified, generation, limit))
	return nil, f.err
}

func (f *fakeGraph) Source(_ context.Context, q agentquery.SourceRequest) (agentquery.SourceResult, error) {
	f.sources = append(f.sources, q)
	return agentquery.SourceResult{Text: "class App {}", StartLine: 1, EndLine: 1}, f.err
}

func graphServer(t *testing.T, fake *fakeGraph) func(path string) (int, map[string]any) {
	t.Helper()
	server := apiServer(t, newFakeStore())
	// apiServer builds its own API; rebuild it around this fake.
	server.Config.Handler = (&healthStatus{}).handler(newWorkerAPI(testAdmissionToken, newFakeStore(), fake, &fakeLinks{}, &fakeAudit{}, &fakeSearch{}, slog.New(slog.NewTextHandler(io.Discard, nil))))
	return func(path string) (int, map[string]any) {
		return call(t, server, http.MethodGet, path, testAdmissionToken, "")
	}
}

func TestGraphReadsPassTheirQuery(t *testing.T) {
	fake := &fakeGraph{}
	get := graphServer(t, fake)
	repo := "/v1/repositories/repo:abc_-1"
	node := url.QueryEscape("node:x/y z")

	code, body := get(repo + "/graph?kind=class&generation=3&cursor=c1")
	if code != http.StatusOK || body["generation"] != float64(7) {
		t.Fatalf("graph %d %v", code, body)
	}
	if got := fake.views[0]; got.RepositoryID != "repo:abc_-1" || got.Kind != "class" || got.Generation != 3 || got.Cursor != "c1" {
		t.Fatalf("view query %+v", got)
	}

	if code, _ = get(repo + "/neighbors?node=" + node + "&generation=3&direction=in&limit=5&cursor=c2"); code != http.StatusOK {
		t.Fatalf("neighbors %d", code)
	}
	if got := fake.neighbors[0]; got.NodeID != "node:x/y z" || got.Direction != graph.Incoming || got.Limit != 5 || got.Cursor != "c2" || got.Generation != 3 {
		t.Fatalf("neighbor query %+v", got)
	}
	// Both directions and 40 at a time unless asked otherwise.
	get(repo + "/neighbors?node=" + node)
	if got := fake.neighbors[1]; got.Direction != graph.Both || got.Limit != 40 || got.Generation != 0 {
		t.Fatalf("neighbor defaults %+v", got)
	}

	code, body = get(repo + "/symbols?name=parse&generation=3")
	if code != http.StatusOK || fake.symbols[0] != "repo:abc_-1|parse||3|20" {
		t.Fatalf("symbols %d %v %v", code, body, fake.symbols)
	}
	if nodes, ok := body["nodes"].([]any); !ok || len(nodes) != 0 {
		t.Fatalf("no matches must be an empty list: %v", body)
	}

	code, body = get(repo + "/source?node=" + node + "&context=5")
	if code != http.StatusOK || body["text"] != "class App {}" || fake.sources[0].ContextLines != 5 || fake.sources[0].NodeID != "node:x/y z" {
		t.Fatalf("source %d %v %+v", code, body, fake.sources)
	}
}

func TestGraphReadsRejectBadInput(t *testing.T) {
	fake := &fakeGraph{}
	get := graphServer(t, fake)
	repo := "/v1/repositories/repo:1"
	for _, path := range []string{
		repo + "/graph?generation=-1",
		repo + "/graph?generation=x",
		repo + "/neighbors",
		repo + "/neighbors?node=n&direction=sideways",
		repo + "/neighbors?node=n&limit=0",
		repo + "/neighbors?node=n&limit=500",
		repo + "/symbols",
		repo + "/symbols?name=a&qualified_name=b",
		repo + "/source",
		repo + "/source?node=n&context=500",
	} {
		if code, body := get(path); code != http.StatusBadRequest {
			t.Errorf("%s: %d %v", path, code, body)
		}
	}
	if len(fake.views)+len(fake.neighbors)+len(fake.symbols)+len(fake.sources) != 0 {
		t.Fatal("bad input reached the graph")
	}

	fake.err = fmt.Errorf("%w: node n", graph.ErrNotFound)
	if code, body := get(repo + "/source?node=n"); code != http.StatusNotFound || body["code"] != "not_found" {
		t.Fatalf("missing node: %d %v", code, body)
	}
	fake.err = fmt.Errorf("%w: generation is not published", graph.ErrInvalid)
	if code, _ := get(repo + "/graph?generation=99"); code != http.StatusBadRequest {
		t.Fatalf("unpublished generation: %d", code)
	}
	// Like every call here, reads need the token.
	server := apiServer(t, newFakeStore())
	if code, _ := call(t, server, http.MethodGet, repo+"/graph", "", ""); code != http.StatusUnauthorized {
		t.Fatalf("read without a token: %d", code)
	}
}
