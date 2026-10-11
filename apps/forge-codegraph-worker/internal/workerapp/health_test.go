package workerapp

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
)

func probe(t *testing.T, server *httptest.Server, method, path string) (int, string) {
	t.Helper()
	req, err := http.NewRequest(method, server.URL+path, nil)
	if err != nil {
		t.Fatal(err)
	}
	res, err := server.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	body, _ := io.ReadAll(res.Body)
	return res.StatusCode, string(body)
}

func TestProbes(t *testing.T) {
	status := &healthStatus{}
	server := httptest.NewServer(status.handler())
	t.Cleanup(server.Close)
	if code, body := probe(t, server, http.MethodGet, "/livez"); code != http.StatusOK || body != "ok\n" {
		t.Fatalf("livez: %d %q", code, body)
	}
	if code, _ := probe(t, server, http.MethodGet, "/readyz"); code != http.StatusServiceUnavailable {
		t.Fatalf("readyz before startup: %d", code)
	}
	status.ready.Store(true)
	status.spannerOK.Store(true)
	status.queueOK.Store(true)
	if code, body := probe(t, server, http.MethodGet, "/readyz"); code != http.StatusOK || body != "ready\n" {
		t.Fatalf("readyz: %d %q", code, body)
	}
	// Either database failing its probe makes the worker unready.
	status.queueOK.Store(false)
	if code, _ := probe(t, server, http.MethodGet, "/readyz"); code != http.StatusServiceUnavailable {
		t.Fatalf("readyz without the job queue: %d", code)
	}
	status.queueOK.Store(true)
	status.spannerOK.Store(false)
	if code, _ := probe(t, server, http.MethodGet, "/readyz"); code != http.StatusServiceUnavailable {
		t.Fatalf("readyz without Spanner: %d", code)
	}
}

// The worker serves the probes only: graph reads, search, runs, the audit
// and cross-repository links live in the Python package
// forge_codegraph_query.
func TestTheWorkerServesNoAPI(t *testing.T) {
	status := &healthStatus{}
	status.ready.Store(true)
	server := httptest.NewServer(status.handler())
	t.Cleanup(server.Close)
	for _, route := range [][2]string{
		{http.MethodGet, "/v1/repositories/repo:abc_-1/runs/run-1"},
		{http.MethodGet, "/v1/repositories/repo:abc_-1/runs"},
		{http.MethodGet, "/v1/repositories/repo:abc_-1/stats"},
		{http.MethodGet, "/v1/repositories/repo:abc_-1/graph"},
		{http.MethodGet, "/v1/repositories/repo:abc_-1/node"},
		{http.MethodGet, "/v1/repositories/repo:abc_-1/cross-links"},
		{http.MethodGet, "/v1/search"},
		{http.MethodPut, "/v1/cross-links/team-1"},
		{http.MethodPost, "/v1/runs"},
	} {
		if code, _ := probe(t, server, route[0], route[1]); code != http.StatusNotFound && code != http.StatusMethodNotAllowed {
			t.Errorf("%s %s: %d", route[0], route[1], code)
		}
	}
}

func TestStartHealthServesTheProbes(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	if server, done, err := startHealth(context.Background(), "", &healthStatus{}, logger); server != nil || done != nil || err != nil {
		t.Fatalf("an empty address must disable the listener: %v %v %v", server, done, err)
	}
	server, _, err := startHealth(context.Background(), "127.0.0.1:0", &healthStatus{}, logger)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = server.Close() })
	if server.WriteTimeout <= 0 || server.ReadHeaderTimeout <= 0 {
		t.Fatalf("timeouts %v %v", server.WriteTimeout, server.ReadHeaderTimeout)
	}
}
