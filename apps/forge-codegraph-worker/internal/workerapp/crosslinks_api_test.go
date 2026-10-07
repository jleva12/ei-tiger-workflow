package workerapp

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"testing"

	"ei-aitiger-codegraph/agentquery"
	"ei-aitiger-codegraph/pkg/graph"
)

// fakeLinks keeps the sets it is given and answers hops from them.
type fakeLinks struct {
	sets  map[string][]graph.CrossLink
	hops  []string
	nodes []string
	err   error
}

func (f *fakeLinks) Node(_ context.Context, repo, id string, generation uint64) (graph.Version, error) {
	f.nodes = append(f.nodes, fmt.Sprintf("%s|%s|%d", repo, id, generation))
	return graph.Version{Fact: graph.Fact{Node: &graph.Node{ID: id, Kind: "method"}}, GenFrom: 1}, f.err
}

func (f *fakeLinks) CrossHops(_ context.Context, repo, id string, direction graph.Direction) ([]agentquery.CrossHop, error) {
	f.hops = append(f.hops, fmt.Sprintf("%s|%s|%s", repo, id, direction))
	return nil, f.err
}

func (f *fakeLinks) ReplaceCrossLinks(_ context.Context, owner string, links []graph.CrossLink) error {
	if f.err != nil {
		return f.err
	}
	if f.sets == nil {
		f.sets = map[string][]graph.CrossLink{}
	}
	f.sets[owner] = links
	return nil
}

func linksServer(t *testing.T, fake *fakeLinks) func(method, path, body string) (int, map[string]any) {
	t.Helper()
	server := apiServer(t, newFakeStore())
	server.Config.Handler = (&healthStatus{}).handler(newWorkerAPI(testAdmissionToken, newFakeStore(), &fakeGraph{}, fake, &fakeAudit{}, &fakeSearch{}, slog.New(slog.NewTextHandler(io.Discard, nil))))
	return func(method, path, body string) (int, map[string]any) {
		return call(t, server, method, path, testAdmissionToken, body)
	}
}

const crossLinkBody = `{"links":[{"id":"l1","owner":"team:t1","kind":"calls_api","source":{"repository_id":"repo:a","node_id":"entity:client","qualified_name":"a.Client.create()","kind":"method"},"target":{"repository_id":"repo:b","node_id":"entity:handler"},"label":"POST /v1/orders","provenance":"manual"}]}`

func TestCrossLinkRoutes(t *testing.T) {
	fake := &fakeLinks{}
	do := linksServer(t, fake)

	code, body := do(http.MethodPut, "/v1/cross-links/team:t1", crossLinkBody)
	if code != http.StatusOK || body["links"] != float64(1) {
		t.Fatalf("replace %d %v", code, body)
	}
	if got := fake.sets["team:t1"]; len(got) != 1 || got[0].Target.NodeID != "entity:handler" || got[0].Label != "POST /v1/orders" {
		t.Fatalf("stored %+v", got)
	}
	// An empty set clears the owner's links.
	if code, _ := do(http.MethodPut, "/v1/cross-links/team:t1", `{"links":[]}`); code != http.StatusOK || len(fake.sets["team:t1"]) != 0 {
		t.Fatalf("clear %d %v", code, fake.sets)
	}

	node := url.QueryEscape("entity:client")
	if code, body := do(http.MethodGet, "/v1/repositories/repo:a/node?node="+node, ""); code != http.StatusOK || fake.nodes[0] != "repo:a|entity:client|0" || body["gen_from"] != float64(1) {
		t.Fatalf("node %d %v %v", code, body, fake.nodes)
	}
	code, body = do(http.MethodGet, "/v1/repositories/repo:a/cross-links?node="+node, "")
	if hops, ok := body["hops"].([]any); code != http.StatusOK || !ok || len(hops) != 0 || fake.hops[0] != "repo:a|entity:client|both" {
		t.Fatalf("hops %d %v %v", code, body, fake.hops)
	}
	do(http.MethodGet, "/v1/repositories/repo:a/cross-links?node="+node+"&direction=in", "")
	if fake.hops[1] != "repo:a|entity:client|in" {
		t.Fatalf("direction %v", fake.hops)
	}

	for _, bad := range []struct{ method, path, body string }{
		{http.MethodPut, "/v1/cross-links/team:t1", `{"links":[],"extra":1}`},
		{http.MethodPut, "/v1/cross-links/team:t1", `{"links":[]} {}`},
		{http.MethodPut, "/v1/cross-links/team%20one", `{"links":[]}`},
		{http.MethodGet, "/v1/repositories/repo:a/node", ""},
		{http.MethodGet, "/v1/repositories/repo:a/cross-links", ""},
		{http.MethodGet, "/v1/repositories/repo:a/cross-links?node=n&direction=sideways", ""},
	} {
		if code, body := do(bad.method, bad.path, bad.body); code != http.StatusBadRequest {
			t.Errorf("%s %s: %d %v", bad.method, bad.path, code, body)
		}
	}
	fake.err = fmt.Errorf("%w: cross link kind", graph.ErrInvalid)
	if code, body := do(http.MethodPut, "/v1/cross-links/team:t1", crossLinkBody); code != http.StatusBadRequest || !strings.Contains(fmt.Sprint(body["message"]), "cross link kind") {
		t.Fatalf("invalid set %d %v", code, body)
	}
	fake.err = fmt.Errorf("%w: node n", graph.ErrNotFound)
	if code, _ := do(http.MethodGet, "/v1/repositories/repo:a/node?node=n", ""); code != http.StatusNotFound {
		t.Fatalf("missing node %d", code)
	}
	// Every route wants the admission token.
	server := apiServer(t, newFakeStore())
	if code, _ := call(t, server, http.MethodPut, "/v1/cross-links/team:t1", "", `{"links":[]}`); code != http.StatusUnauthorized {
		t.Fatalf("no token %d", code)
	}
}
