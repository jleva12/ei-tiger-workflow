//go:build integration

package ingestion_test

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
	"ei-aitiger-codegraph/worker/internal/languages/builtin"
	"ei-aitiger-codegraph/worker/internal/repository/github"
	"ei-aitiger-codegraph/worker/internal/workerapp"
)

// localRun is what one repository's ingestion did, for the report.
type localRun struct {
	Repository string              `json:"repository"`
	Commit     string              `json:"commit"`
	Phase      deployment.Phase    `json:"phase"`
	Error      string              `json:"error,omitempty"`
	Warning    string              `json:"warning,omitempty"`
	Metrics    *deployment.Metrics `json:"metrics,omitempty"`
	Index      string              `json:"index,omitempty"`
	Seconds    float64             `json:"seconds"`
	// Unresolved groups the references the run left unresolved, ambiguous
	// or unsupported by status, kind, cause and reason, most frequent first.
	Unresolved []unresolvedGroup `json:"unresolved,omitempty"`
}

type unresolvedGroup struct {
	Status  string   `json:"status"`
	Kind    string   `json:"kind"`
	Cause   string   `json:"cause"`
	Reason  string   `json:"reason"`
	Count   int      `json:"count"`
	Samples []string `json:"samples"`
}

// TestIngestLocalRepositories runs local Git repositories through the real
// worker runtime (build, parse, resolve, match, project, load into the
// Spanner emulator, publish, embed) and fails for every run that does not
// succeed. It is how a change to the pipeline is proven on real code bases:
//
//	SPANNER_EMULATOR_HOST=127.0.0.1:19030 \
//	CODEGRAPH_TEST_REPOS='/path/repo-a,/path/mirror-b.git#develop' \
//	CODEGRAPH_TEST_LANGUAGES='{"java":{...},"typescript":{},"python":{}}' \
//	CODEGRAPH_TEST_TIKTOKEN_PYTHON=/path/to/python-with-tiktoken \
//	CODEGRAPH_TEST_REPORT=/tmp/report.json \
//	go test -tags integration -run TestIngestLocalRepositories -timeout 0 ./internal/ingestion/
//
// Each entry is a repository directory or a bare repository, with the
// branch after '#' (its current branch or HEAD otherwise). With a Python
// that has tiktoken, embeddings go to testdata/fake_embedder.py, which
// refuses inputs the real provider would refuse; otherwise to a lenient
// in-process endpoint. CODEGRAPH_TEST_EMBED=off leaves embeddings out.
func TestIngestLocalRepositories(t *testing.T) {
	entries := strings.FieldsFunc(os.Getenv("CODEGRAPH_TEST_REPOS"), func(r rune) bool { return r == ',' })
	if os.Getenv("SPANNER_EMULATOR_HOST") == "" || len(entries) == 0 {
		t.Skip("requires SPANNER_EMULATOR_HOST and CODEGRAPH_TEST_REPOS")
	}
	ctx := context.Background()
	c := workerapp.DefaultConfig()
	c.WorkDir = t.TempDir()
	if dir := os.Getenv("CODEGRAPH_TEST_WORK_DIR"); dir != "" {
		c.WorkDir = dir
	}
	c.Spanner.CursorSigningKey = strings.Repeat("k", 32)
	c.Spanner.AutoProvision = true
	// Its own instance: provisioning replaces an instance of the same name,
	// which would pull the database from under a concurrent run.
	c.Spanner.Database = fmt.Sprintf("projects/codegraph-test/instances/local-%d/databases/r%x", os.Getpid(), time.Now().UnixNano()%1_000_000_000)
	// Runs are driven here directly; the job queue is never opened.
	c.JobsMySQLDSN = "unused:unused@tcp(127.0.0.1:1)/unused"
	c.StartupTimeout = time.Minute
	c.BuildMode = "maven-resolved"
	if mode := os.Getenv("CODEGRAPH_TEST_BUILD_MODE"); mode != "" {
		c.BuildMode = mode
	}
	c.Queue = "local-jobs"
	c.LeaseTTL = 2 * time.Minute
	c.RenewInterval = 20 * time.Second
	if raw := os.Getenv("CODEGRAPH_TEST_LANGUAGES"); raw != "" {
		if err := json.Unmarshal([]byte(raw), &c.Languages); err != nil {
			t.Fatalf("CODEGRAPH_TEST_LANGUAGES: %v", err)
		}
	}
	// CODEGRAPH_TEST_EMBED=off skips the index pass: the emulator reads
	// each page of documents in time proportional to the whole graph.
	if os.Getenv("CODEGRAPH_TEST_EMBED") != "off" {
		c.Embedding.Model, c.Embedding.APIKey, c.Embedding.Dimensions = "text-embedding-3-large", "local-key", 16
		c.Embedding.BaseURL = startEmbedder(t)
	}

	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	if testing.Verbose() {
		logger = slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	}
	registry, err := builtin.Registry()
	if err != nil {
		t.Fatal(err)
	}
	deps, err := workerapp.Bootstrap(c, registry)
	if err != nil {
		t.Fatal(err)
	}
	storage, err := workerapp.OpenStorage(ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	defer storage.Close()
	paths := map[string]string{}
	github.SetRemoteURL(func(r github.Request) string { return paths[r.Name] })
	defer github.SetRemoteURL(nil)
	runtime, err := workerapp.NewRuntime(c, deps, storage, "local-owner", logger)
	if err != nil {
		t.Fatal(err)
	}
	defer runtime.Close()
	store := storage.Store
	digest := workerapp.ConfigurationDigest(c, deps)

	var report []localRun
	for i, entry := range entries {
		path, branch, _ := strings.Cut(strings.TrimSpace(entry), "#")
		name := strings.TrimSuffix(filepath.Base(path), ".git")
		if branch == "" {
			branch = gitOutput(t, path, "rev-parse", "--abbrev-ref", "HEAD")
		}
		commit := gitOutput(t, path, "rev-parse", branch+"^{commit}")
		paths[name] = path
		t.Run(name, func(t *testing.T) {
			started := time.Now()
			result := localRun{Repository: path + "#" + branch, Commit: commit}
			defer func() {
				result.Seconds = time.Since(started).Seconds()
				report = append(report, result)
			}()
			url := "https://github.com/local/" + name
			repoID := graph.ID("repo", url)
			if _, err := store.PutRepository(ctx, deployment.Repository{SchemaVersion: deployment.SchemaVersion, RepositoryID: repoID, GitHubURL: url, IntegrationID: "local"}); err != nil {
				t.Fatal(err)
			}
			request := deployment.Request{SchemaVersion: deployment.SchemaVersion, Branch: branch, RepositoryID: repoID, DeploymentID: fmt.Sprintf("local-%d", i+1), DeploymentSequence: 1, TargetCommitSHA: commit, DeployedAt: time.Now().UTC(), AnalysisConfigDigest: digest, TriggerKind: deployment.TriggerDeployment}
			admitted, err := store.Admit(ctx, deployment.Admission{SubmissionID: fmt.Sprintf("local-%s", name), Request: request})
			if err != nil {
				t.Fatal("admit: ", err)
			}
			run, runErr := runtime.Pipeline.Run(ctx, admitted.Run.Key)
			if latest, err := store.GetRun(ctx, admitted.Run.Key); err == nil {
				run = latest
			}
			result.Phase, result.Warning, result.Metrics = run.Phase, run.WarningMessage, run.Metrics
			if run.Index != nil {
				result.Index = string(run.Index.Status)
			}
			if runErr != nil {
				result.Error = runErr.Error()
			} else if run.ErrorMessage != "" {
				result.Error = run.ErrorMessage
			}
			if run.Phase != deployment.Succeeded {
				t.Errorf("%s did not succeed: phase %s: %s", name, run.Phase, result.Error)
				return
			}
			m := run.Metrics
			total := m.Resolved + m.Unresolved + m.Ambiguous + m.Unsupported
			rate := 0.0
			if total > 0 {
				rate = 100 * float64(m.Resolved) / float64(total)
			}
			t.Logf("%s: files=%d symbols=%d lookups=%d resolved=%.1f%% unresolved=%d ambiguous=%d unsupported=%d nodes=%d edges=%d embedded=%d warning=%q", name, m.Files, m.Symbols, total, rate, m.Unresolved, m.Ambiguous, m.Unsupported, m.Nodes, m.Edges, m.Embedded, run.WarningMessage)
			groups, err := unresolvedGroups(ctx, store, repoID)
			if err != nil {
				t.Errorf("%s: reading unresolved references: %v", name, err)
			}
			result.Unresolved = groups
		})
	}
	if out := os.Getenv("CODEGRAPH_TEST_REPORT"); out != "" {
		body, _ := json.MarshalIndent(report, "", "  ")
		if err := os.WriteFile(out, body, 0o644); err != nil {
			t.Error(err)
		}
	}
}

func gitOutput(t *testing.T, dir string, args ...string) string {
	t.Helper()
	out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).Output()
	if err != nil {
		t.Fatalf("git %v in %s: %v", args, dir, err)
	}
	return strings.TrimSpace(string(out))
}

// startEmbedder serves embeddings for the index pass: testdata/fake_embedder.py
// when CODEGRAPH_TEST_TIKTOKEN_PYTHON names a Python with tiktoken, else an
// in-process endpoint that only enforces the batch size.
func startEmbedder(t *testing.T) string {
	t.Helper()
	python := os.Getenv("CODEGRAPH_TEST_TIKTOKEN_PYTHON")
	if python == "" {
		return lenientEmbedder(t)
	}
	cmd := exec.Command(python, filepath.Join("testdata", "fake_embedder.py"))
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })
	port, err := bufio.NewReader(stdout).ReadString('\n')
	if err != nil {
		t.Fatalf("fake embedder: %v", err)
	}
	return "http://127.0.0.1:" + strings.TrimSpace(port)
}

func lenientEmbedder(t *testing.T) string {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Input      []string `json:"input"`
			Dimensions int      `json:"dimensions"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || len(req.Input) == 0 || len(req.Input) > 2048 {
			http.Error(w, "bad batch", http.StatusBadRequest)
			return
		}
		type item struct {
			Index     int       `json:"index"`
			Embedding []float64 `json:"embedding"`
		}
		var out struct {
			Data []item `json:"data"`
		}
		for i, text := range req.Input {
			vector := make([]float64, req.Dimensions)
			for d := range vector {
				vector[d] = float64((len(text)+d*17)%101)/101 + 0.001
			}
			out.Data = append(out.Data, item{Index: i, Embedding: vector})
		}
		_ = json.NewEncoder(w).Encode(out)
	}))
	t.Cleanup(server.Close)
	return server.URL
}

// unresolvedGroups reads the live generation's unresolved_reference nodes and
// groups them; each sample is "path:line".
func unresolvedGroups(ctx context.Context, store *spannerstore.Store, repoID string) ([]unresolvedGroup, error) {
	paths := map[string]string{}
	for cursor := ""; ; {
		page, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repoID, Kind: graph.NodeSourceFile, Limit: 500, Cursor: cursor})
		if err != nil {
			return nil, err
		}
		for _, n := range page.Nodes {
			paths[n.Fact.Node.ID] = n.Fact.Node.Name
		}
		if cursor = page.NextCursor; cursor == "" {
			break
		}
	}
	property := func(n *graph.Node, key string) string {
		if v, ok := n.Properties[key]; ok && v.String != nil {
			return *v.String
		}
		return ""
	}
	byKey := map[string]*unresolvedGroup{}
	for cursor := ""; ; {
		page, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repoID, Kind: graph.NodeUnresolvedReference, Limit: 500, Cursor: cursor})
		if err != nil {
			return nil, err
		}
		for _, v := range page.Nodes {
			n := v.Fact.Node
			reason := property(n, "reason")
			// The reason's class, without the per-occurrence detail.
			if i := strings.IndexAny(reason, "\n"); i >= 0 {
				reason = reason[:i]
			}
			if len(reason) > 160 {
				reason = reason[:160]
			}
			g := unresolvedGroup{Status: property(n, "status"), Kind: property(n, "lookup_kind"), Cause: property(n, "cause"), Reason: reason}
			key := g.Status + "|" + g.Kind + "|" + g.Cause + "|" + g.Reason
			if byKey[key] == nil {
				byKey[key] = &g
			}
			byKey[key].Count++
			if len(byKey[key].Samples) < 4 && n.Source != nil {
				byKey[key].Samples = append(byKey[key].Samples, fmt.Sprintf("%s:%d", paths[n.Source.Lineage], n.Source.Span.Start.Line))
			}
		}
		if cursor = page.NextCursor; cursor == "" {
			break
		}
	}
	var groups []unresolvedGroup
	for _, g := range byKey {
		groups = append(groups, *g)
	}
	sort.Slice(groups, func(i, j int) bool { return groups[i].Count > groups[j].Count })
	return groups, nil
}
