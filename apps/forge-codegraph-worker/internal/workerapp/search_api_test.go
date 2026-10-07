package workerapp

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"ei-aitiger-codegraph/agentquery"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

// fakeSearch answers searches with hits, and source for the nodes it has.
type fakeSearch struct {
	mu      sync.Mutex
	asked   []agentquery.SearchRequest
	hits    []spannerstore.SearchHit
	sources map[string]agentquery.SourceResult
}

func (f *fakeSearch) Search(_ context.Context, q agentquery.SearchRequest) (agentquery.SearchResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.asked = append(f.asked, q)
	return agentquery.SearchResult{Hits: f.hits, Semantic: true}, nil
}

func (f *fakeSearch) Source(_ context.Context, q agentquery.SourceRequest) (agentquery.SourceResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if s, ok := f.sources[q.RepositoryID+"|"+q.NodeID]; ok {
		return s, nil
	}
	return agentquery.SourceResult{}, deployment.ErrNotFound
}

func hit(repo, id, name, file string, line uint32) spannerstore.SearchHit {
	node := graph.Node{ID: id, Kind: "class", Name: name, QualifiedName: "pkg." + name,
		Properties: map[string]graph.PropertyValue{"file_path": {String: &file}},
		Source:     &graph.SourceAnchor{Span: ir.Span{Start: ir.Position{Line: line}}}}
	return spannerstore.SearchHit{RepositoryID: repo, Node: graph.Version{Fact: graph.Fact{Node: &node}}, Score: 1, Signature: "class " + name, Snippet: "class " + name + ":"}
}

func searchServer(t *testing.T, search *fakeSearch) *httptest.Server {
	t.Helper()
	api := newWorkerAPI(testAdmissionToken, newFakeStore(), &fakeGraph{}, &fakeLinks{}, &fakeAudit{}, search, slog.New(slog.NewTextHandler(io.Discard, nil)))
	server := httptest.NewServer((&healthStatus{}).handler(api))
	t.Cleanup(server.Close)
	return server
}

func TestSearchFindsCodeAcrossRepositoriesWithItsSource(t *testing.T) {
	search := &fakeSearch{
		hits: []spannerstore.SearchHit{hit("repo:a", "entity:1", "Signer", "src/sign.py", 10), hit("repo:b", "entity:2", "Loader", "lib/load.py", 3)},
		sources: map[string]agentquery.SourceResult{
			"repo:a|entity:1": {Text: "class Signer:\n    pass\n", StartLine: 10, EndLine: 11},
		},
	}
	server := searchServer(t, search)
	code, body := call(t, server, http.MethodPost, "/v1/search", testAdmissionToken,
		`{"repository_ids":["repo:a","repo:b"],"query":"  how are tokens signed  ","limit":5,"source":true}`)
	if code != http.StatusOK {
		t.Fatalf("search %d %v", code, body)
	}
	asked := search.asked[0]
	if asked.Text != "how are tokens signed" || asked.Limit != 5 || !asked.Expand || len(asked.RepositoryIDs) != 2 {
		t.Fatalf("asked %+v", asked)
	}
	hits := body["hits"].([]any)
	first, second := hits[0].(map[string]any), hits[1].(map[string]any)
	if first["repository_id"] != "repo:a" || first["id"] != "entity:1" || first["file"] != "src/sign.py" || first["line"] != float64(10) ||
		first["text"] != "class Signer:\n    pass\n" || first["text_start_line"] != float64(10) || first["signature"] != "class Signer" {
		t.Fatalf("first hit %v", first)
	}
	// Source that can't be read leaves the hit with its snippet.
	if second["text"] != nil || second["snippet"] != "class Loader:" {
		t.Fatalf("second hit %v", second)
	}
	if body["semantic"] != true {
		t.Fatalf("semantic %v", body)
	}
}

func TestSearchRefusesBadRequests(t *testing.T) {
	server := searchServer(t, &fakeSearch{})
	for name, body := range map[string]string{
		"no repositories": `{"repository_ids":[],"query":"x"}`,
		"too many":        `{"repository_ids":[` + strings.Repeat(`"r",`, 50) + `"r"],"query":"x"}`,
		"no query":        `{"repository_ids":["repo:a"],"query":"  "}`,
		"limit":           `{"repository_ids":["repo:a"],"query":"x","limit":26}`,
		"unknown field":   `{"repository_ids":["repo:a"],"query":"x","kinds":[]}`,
	} {
		if code, out := call(t, server, http.MethodPost, "/v1/search", testAdmissionToken, body); code != http.StatusBadRequest {
			t.Errorf("%s: %d %v", name, code, out)
		}
	}
	if code, _ := call(t, server, http.MethodPost, "/v1/search", "", `{"repository_ids":["repo:a"],"query":"x"}`); code != http.StatusUnauthorized {
		t.Fatalf("without the token: %d", code)
	}
}

func TestBoundSourceCutsAtALineEnd(t *testing.T) {
	text := strings.Repeat("abcdefghi\n", 10)
	cut, truncated := boundSource(text, 35)
	if !truncated || cut != strings.Repeat("abcdefghi\n", 3)[:29] {
		t.Fatalf("cut %q %v", cut, truncated)
	}
	if same, truncated := boundSource("short", 35); same != "short" || truncated {
		t.Fatal("a short text is kept")
	}
	// Never inside a character.
	if cut, _ := boundSource("ééééé", 5); !strings.HasPrefix("ééééé", cut) || len(cut)%2 != 0 {
		t.Fatalf("multibyte cut %q", cut)
	}
}
