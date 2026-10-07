package workerapp

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

const (
	testAdmissionToken = "admission-token-for-tests-0123456789"
	testDigest         = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
	headSHA            = "2222222222222222222222222222222222222222"
	pinnedSHA          = "3333333333333333333333333333333333333333"
)

// fakeStore is the graph's store: repositories, runs and submissions.
type fakeStore struct {
	mu          sync.Mutex
	repos       map[string]deployment.Repository
	puts        int
	admitted    []deployment.Admission
	reused      bool
	runs        map[deployment.RunKey]deployment.Run
	submissions map[string]deployment.RunKey
	failedRuns  []string
	state       graph.RepositoryState
	// events, when set, is shared with a fakeQueue to see the order of
	// writes to both.
	events *[]string
}

func newFakeStore() *fakeStore {
	return &fakeStore{repos: map[string]deployment.Repository{}, runs: map[deployment.RunKey]deployment.Run{}, submissions: map[string]deployment.RunKey{}}
}

func (f *fakeStore) event(e string) {
	if f.events != nil {
		*f.events = append(*f.events, e)
	}
}

func (f *fakeStore) GetRepository(_ context.Context, id string) (deployment.Repository, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r, ok := f.repos[id]; ok {
		return r, nil
	}
	return deployment.Repository{}, deployment.ErrNotFound
}

func (f *fakeStore) PutRepository(_ context.Context, r deployment.Repository) (deployment.Repository, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.puts++
	r.Revision++
	f.repos[r.RepositoryID] = r
	return r, nil
}

func (f *fakeStore) AdmitIngestion(_ context.Context, a deployment.Admission) (deployment.AdmissionResult, error) {
	if err := a.ValidateIngestion(); err != nil {
		return deployment.AdmissionResult{}, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.event("admit")
	f.admitted = append(f.admitted, a)
	key := deployment.RunKey{RepositoryID: a.Request.RepositoryID, RunID: fmt.Sprintf("run-%d", len(f.admitted))}
	run := deployment.Run{Key: key, Request: a.Request, Phase: deployment.Accepted}
	f.runs[key] = run
	f.submissions[a.SubmissionID] = key
	return deployment.AdmissionResult{Run: run, Reused: f.reused}, nil
}

func (f *fakeStore) SubmissionRun(_ context.Context, id string) (deployment.RunKey, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if key, ok := f.submissions[id]; ok {
		return key, nil
	}
	return deployment.RunKey{}, deployment.ErrNotFound
}

func (f *fakeStore) GetRun(_ context.Context, k deployment.RunKey) (deployment.Run, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r, ok := f.runs[k]; ok {
		return r, nil
	}
	return deployment.Run{}, deployment.ErrNotFound
}

func (f *fakeStore) FailRun(_ context.Context, k deployment.RunKey, code string, _ bool) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.event("fail run")
	f.failedRuns = append(f.failedRuns, code)
	return nil
}

func (f *fakeStore) State(context.Context, string) (graph.RepositoryState, error) {
	return f.state, nil
}

// fakeBranches records which repository each branch was read from.
type fakeBranches struct {
	err error
	as  []string
}

func (f *fakeBranches) ResolveBranch(_ context.Context, owner, name, branch string) (string, error) {
	f.as = append(f.as, owner+"/"+name)
	return headSHA, f.err
}

func apiServer(t *testing.T, store *fakeStore) *httptest.Server {
	t.Helper()
	status := &healthStatus{}
	api := newWorkerAPI(testAdmissionToken, store, &fakeGraph{}, &fakeLinks{}, &fakeAudit{}, &fakeSearch{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	server := httptest.NewServer(status.handler(api))
	t.Cleanup(server.Close)
	return server
}

func call(t *testing.T, server *httptest.Server, method, path, token, body string) (int, map[string]any) {
	t.Helper()
	req, err := http.NewRequest(method, server.URL+path, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	res, err := server.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(res.Body).Decode(&out)
	return res.StatusCode, out
}

func TestAPIRequiresTheToken(t *testing.T) {
	store := newFakeStore()
	key := deployment.RunKey{RepositoryID: "repo:abc_-1", RunID: "run-1"}
	store.runs[key] = deployment.Run{Key: key, Phase: deployment.Running}
	server := apiServer(t, store)
	for _, token := range []string{"", "wrong", testAdmissionToken + "x"} {
		if code, _ := call(t, server, http.MethodGet, "/v1/repositories/repo:abc_-1/runs/run-1", token, ""); code != http.StatusUnauthorized {
			t.Errorf("token %q: status %d", token, code)
		}
	}
	// Probes stay open.
	if code, _ := call(t, server, http.MethodGet, "/livez", "", ""); code != http.StatusOK {
		t.Fatalf("livez: %d", code)
	}
}

func TestAPINoLongerAdmitsIngestions(t *testing.T) {
	server := apiServer(t, newFakeStore())
	body := `{"url":"https://github.com/acme/widgets","branch":"main","requested_by":"user-1"}`
	if code, _ := call(t, server, http.MethodPost, "/v1/runs", testAdmissionToken, body); code != http.StatusNotFound && code != http.StatusMethodNotAllowed {
		t.Fatalf("POST /v1/runs: %d", code)
	}
}

func TestAPIInspectsRuns(t *testing.T) {
	store := newFakeStore()
	key := deployment.RunKey{RepositoryID: "repo:abc_-1", RunID: "run-1"}
	store.runs[key] = deployment.Run{Key: key, Phase: deployment.Running}
	server := apiServer(t, store)
	code, body := call(t, server, http.MethodGet, "/v1/repositories/repo:abc_-1/runs/run-1", testAdmissionToken, "")
	if code != http.StatusOK || body["run"].(map[string]any)["phase"] != string(deployment.Running) || body["job"] != nil {
		t.Fatalf("inspect %d %v", code, body)
	}
	if code, _ := call(t, server, http.MethodGet, "/v1/repositories/repo:abc_-1/runs/missing", testAdmissionToken, ""); code != http.StatusNotFound {
		t.Fatalf("missing run: %d", code)
	}
}

func TestStaticTokenSource(t *testing.T) {
	// Every fetch, whichever repository it reads, uses the one token.
	source := staticTokenSource("github_pat_test")
	for _, repo := range [][2]string{{"acme", "widgets"}, {"other", "gadgets"}} {
		if got, err := source(context.Background(), repo[0], repo[1]); err != nil || got != "github_pat_test" {
			t.Fatalf("%s/%s: token %q, %v", repo[0], repo[1], got, err)
		}
	}
	// Without one, there is no source and Git reads anonymously.
	if staticTokenSource("") != nil {
		t.Fatal("an empty token made a source")
	}
}

func TestConfigValidation(t *testing.T) {
	valid := DefaultConfig()
	valid.Spanner.CursorSigningKey = strings.Repeat("k", 32)
	valid.JobsMySQLDSN = "forge_admin:secret@tcp(127.0.0.1:13326)/forge_admin"
	if err := valid.Validate(); err != nil {
		t.Fatal(err)
	}
	// The API needs no GitHub token: without one the worker reads public
	// repositories anonymously.
	withAPI := valid
	withAPI.AdmissionToken = testAdmissionToken
	if err := withAPI.Validate(); err != nil {
		t.Fatalf("API without a GitHub token: %v", err)
	}
	withToken := withAPI
	withToken.GitHubToken = "github_pat_" + strings.Repeat("t", 32)
	if err := withToken.Validate(); err != nil {
		t.Fatalf("API with a GitHub token: %v", err)
	}
	for name, mutate := range map[string]func(*Config){
		"short token":          func(c *Config) { c.AdmissionToken = "short" },
		"no listener":          func(c *Config) { c.HealthAddr = "" },
		"GitHub token newline": func(c *Config) { c.GitHubToken = "github_pat_x\n" },
		"GitHub token space":   func(c *Config) { c.GitHubToken = "token github_pat_x" },
		"no job queue":         func(c *Config) { c.JobsMySQLDSN = " " },
	} {
		c := withAPI
		mutate(&c)
		if err := c.Validate(); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}
