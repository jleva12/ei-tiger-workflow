//go:build integration

package ingestion_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
	"ei-aitiger-codegraph/worker/internal/languages/builtin"
	"ei-aitiger-codegraph/worker/internal/repository/github"
	"ei-aitiger-codegraph/worker/internal/workerapp"
)

// The fixture is a two-module Maven reactor: util declares an interface, a
// class implementing it with a lambda, a method reference and an override;
// app calls into util across the module boundary.
const utilService = `package util;
public class Service implements Handler {
    public void call() {
        Handler h = s -> System.out.println(s);
        Handler m = this::handle;
        h.handle("x");
        m.handle("y");
    }
    @Override public void handle(String s) { }
    @Override public String toString() { return "Service"; }
}
`
const utilHandler = `package util;
public interface Handler { void handle(String s); }
`
const appMain = `package app;
import util.Service;
public class Main {
    public static void main(String[] args) { new Service().call(); }
}
`

// The front end has no build to run: project discovery reads its tsconfig
// path mapping, the barrel re-exports the API module, and the syntax-tier
// resolver binds the alias import through the barrel.
const webTSConfig = `{
  // path alias used by the application
  "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["src/*"] }, "outDir": "dist" },
}
`
const webApi = `export class ApiClient {
  get(path: string): string {
    return path;
  }
}

export function createClient(): ApiClient {
  return new ApiClient();
}
`
const webBarrel = `export { ApiClient, createClient } from "./api";
`
const webApp = `import { ApiClient, createClient } from "@/index";

export class AppService {
  private client: ApiClient = createClient();

  run(): string {
    return this.client.get("/users");
  }
}
`

func parentPOM(release string) string {
	return `<project><modelVersion>4.0.0</modelVersion><groupId>fixture</groupId><artifactId>parent</artifactId><version>1</version><packaging>pom</packaging><properties><maven.compiler.release>` + release + `</maven.compiler.release></properties><modules><module>util</module><module>app</module></modules></project>`
}

type fixture struct {
	t      *testing.T
	remote string // working clone whose .git is the remote the service clones
	bare   string
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	dir := t.TempDir()
	bare := filepath.Join(dir, "remote.git")
	work := filepath.Join(dir, "work")
	f := &fixture{t: t, remote: work, bare: bare}
	f.git(dir, "init", "--bare", "--quiet", "-b", "main", bare)
	f.git(dir, "clone", "--quiet", bare, work)
	f.write("pom.xml", parentPOM("21"))
	f.write("util/pom.xml", `<project><modelVersion>4.0.0</modelVersion><parent><groupId>fixture</groupId><artifactId>parent</artifactId><version>1</version></parent><artifactId>util</artifactId></project>`)
	f.write("app/pom.xml", `<project><modelVersion>4.0.0</modelVersion><parent><groupId>fixture</groupId><artifactId>parent</artifactId><version>1</version></parent><artifactId>app</artifactId><dependencies><dependency><groupId>fixture</groupId><artifactId>util</artifactId><version>1</version></dependency></dependencies></project>`)
	f.write("util/src/main/java/util/Handler.java", utilHandler)
	f.write("util/src/main/java/util/Service.java", utilService)
	f.write("app/src/main/java/app/Main.java", appMain)
	f.write("web/tsconfig.json", webTSConfig)
	f.write("web/src/api.ts", webApi)
	f.write("web/src/index.ts", webBarrel)
	f.write("web/src/app.ts", webApp)
	f.write("web/dist/app.js", "export {};\n")
	return f
}

func (f *fixture) git(dir string, args ...string) string {
	f.t.Helper()
	cmd := exec.Command("git", append([]string{"-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"}, args...)...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		f.t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func (f *fixture) write(path, content string) {
	f.t.Helper()
	full := filepath.Join(f.remote, path)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		f.t.Fatal(err)
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fixture) remove(path string) {
	f.t.Helper()
	if err := os.RemoveAll(filepath.Join(f.remote, path)); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fixture) commit(message string) string {
	f.t.Helper()
	f.git(f.remote, "add", "--all")
	f.git(f.remote, "commit", "--quiet", "--allow-empty", "-m", message)
	f.git(f.remote, "push", "--quiet", "origin", "HEAD:main")
	return f.git(f.remote, "rev-parse", "HEAD")
}

func TestPipelineGenerations(t *testing.T) {
	if os.Getenv("SPANNER_EMULATOR_HOST") == "" || os.Getenv("CODEGRAPH_TEST_JAVA_HOME") == "" || os.Getenv("CODEGRAPH_TEST_MAVEN") == "" {
		t.Skip("requires SPANNER_EMULATOR_HOST, CODEGRAPH_TEST_JAVA_HOME and CODEGRAPH_TEST_MAVEN")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	fx := newFixture(t)
	first := fx.commit("initial")

	c := workerapp.DefaultConfig()
	c.WorkDir = t.TempDir()
	c.Spanner.CursorSigningKey = strings.Repeat("k", 32)
	c.Spanner.AutoProvision = true
	c.Embedding.Dimensions = 2 // the fixture embeds two-dimensional vectors
	// A local embedding endpoint: batches of up to sixteen texts, one
	// two-dimensional vector each, so the index pass runs for real.
	embedder := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Input []string `json:"input"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || len(req.Input) == 0 || len(req.Input) > 16 {
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
			out.Data = append(out.Data, item{Index: i, Embedding: []float64{float64(len(text)), 1}})
		}
		_ = json.NewEncoder(w).Encode(out)
	}))
	defer embedder.Close()
	c.Embedding.Model, c.Embedding.APIKey, c.Embedding.BaseURL = "fixture-embedding", "fixture-key", embedder.URL
	c.Spanner.Database = fmt.Sprintf("projects/codegraph-test/instances/pipeline/databases/g%x", time.Now().UnixNano()%1_000_000_000)
	// Runs are driven here directly; the job queue is never opened.
	c.JobsMySQLDSN = "unused:unused@tcp(127.0.0.1:1)/unused"
	c.StartupTimeout = time.Minute
	c.BuildMode = "maven-resolved"
	c.Timeout = 10 * time.Minute
	c.Queue = "fixture-jobs"
	c.LeaseTTL = time.Minute
	c.RenewInterval = 10 * time.Second
	language, _ := json.Marshal(map[string]any{"fallback_release": 21, "java_home": os.Getenv("CODEGRAPH_TEST_JAVA_HOME"), "maven_executable": os.Getenv("CODEGRAPH_TEST_MAVEN"), "cache_dir": filepath.Join(c.WorkDir, "java-cache"), "work_dir": filepath.Join(c.WorkDir, "java-work"), "max_heap_mib": 512})
	// The syntax tier alone binds the front end, whether or not the compiler
	// bridge is built on this machine (make worker-install builds it).
	c.Languages = map[string]json.RawMessage{"java": language, "typescript": json.RawMessage(`{"compiler":{"enabled":false}}`)}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	if testing.Verbose() {
		logger = slog.New(slog.NewTextHandler(os.Stdout, nil))
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
	github.SetRemoteURL(func(github.Request) string { return fx.bare })
	defer github.SetRemoteURL(nil)
	runtime, err := workerapp.NewRuntime(c, deps, storage, "test-owner", logger)
	if err != nil {
		t.Fatal(err)
	}
	defer runtime.Close()
	store := storage.Store
	repoID := graph.ID("repo", "https://github.com/fixture/repo")
	if _, err = store.PutRepository(ctx, deployment.Repository{SchemaVersion: deployment.SchemaVersion, RepositoryID: repoID, GitHubURL: "https://github.com/fixture/repo", IntegrationID: "test"}); err != nil {
		t.Fatal(err)
	}
	digest := workerapp.ConfigurationDigest(c, deps)

	run := func(sha string, seq uint64) deployment.Run {
		t.Helper()
		request := deployment.Request{SchemaVersion: deployment.SchemaVersion, Branch: "main", RepositoryID: repoID, DeploymentID: fmt.Sprintf("deployment-%d", seq), DeploymentSequence: seq, TargetCommitSHA: sha, DeployedAt: time.Date(2026, 9, 13, 0, 0, int(seq), 0, time.UTC), AnalysisConfigDigest: digest, TriggerKind: deployment.TriggerDeployment}
		admitted, e := store.Admit(ctx, deployment.Admission{SubmissionID: fmt.Sprintf("submission-%d", seq), Request: request})
		if e != nil {
			t.Fatal("admit", e)
		}
		again, e := store.Admit(ctx, deployment.Admission{SubmissionID: fmt.Sprintf("submission-%d", seq), Request: request})
		if e != nil || !again.Reused || again.Run.Key != admitted.Run.Key {
			t.Fatalf("admission replay: %+v %v", again, e)
		}
		out, e := runtime.Pipeline.Run(ctx, admitted.Run.Key)
		if e != nil {
			t.Fatalf("run %d: %v", seq, e)
		}
		if out.Phase != deployment.Succeeded || out.Metrics == nil {
			t.Fatalf("run %d did not succeed: %+v", seq, out)
		}
		state, e := store.State(ctx, repoID)
		if e != nil || state.LiveGeneration != out.Generation || state.LiveCommit != sha {
			t.Fatalf("live state %+v %v (want generation %d)", state, e, out.Generation)
		}
		t.Logf("seq=%d generation=%d files=%d affected=%d nodes=%d edges=%d added=%d updated=%d retired=%d reopened=%d resolved=%d unresolved=%d durations=%v",
			seq, out.Generation, out.Metrics.Files, out.Metrics.AffectedFiles, out.Metrics.Nodes, out.Metrics.Edges, out.Metrics.Added, out.Metrics.Updated, out.Metrics.Retired, out.Metrics.Reopened, out.Metrics.Resolved, out.Metrics.Unresolved, out.Metrics.Durations)
		return out
	}
	edgesOfKind := func(kind string, generation uint64) []graph.Version {
		t.Helper()
		page, e := store.ListEdges(ctx, graph.ListQuery{RepositoryID: repoID, Kind: kind, Generation: generation, Limit: 500})
		if e != nil {
			t.Fatal(e)
		}
		return page.Edges
	}

	// Generation 1: initial full run.
	r1 := run(first, 1)
	if r1.Generation != 1 || r1.Metrics.Added == 0 || r1.Metrics.Nodes == 0 || r1.Metrics.Edges == 0 || r1.Metrics.Unresolved != 0 {
		t.Fatalf("initial run metrics: %+v", r1.Metrics)
	}
	// The index pass ran after publication and its outcome is on the run.
	if r1.Index == nil || r1.Index.Status != deployment.IndexComplete || r1.Index.Embedded == 0 || r1.Index.FinishedAt == nil || r1.Metrics.Embedded != r1.Index.Embedded {
		t.Fatalf("index state after the initial run: %+v metrics %+v", r1.Index, r1.Metrics)
	}
	if _, ok := r1.Metrics.Durations["embed"]; !ok {
		t.Fatalf("embed duration missing: %v", r1.Metrics.Durations)
	}
	if n := len(edgesOfKind(graph.EdgeImplements, 0)); n < 2 {
		t.Fatalf("expected implements edges for the lambda and the method reference, got %d", n)
	}
	if n := len(edgesOfKind(graph.EdgeOverrides, 0)); n < 2 {
		t.Fatalf("expected overrides edges for handle and toString, got %d", n)
	}
	if n := len(edgesOfKind(graph.EdgeCalls, 0)); n == 0 {
		t.Fatal("expected call edges")
	}
	// The app → util call is a cross-module edge anchored in Main.java, whose
	// source_file node ID is that file's lineage.
	files, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repoID, Kind: graph.NodeSourceFile, Limit: 50})
	if err != nil {
		t.Fatal(err)
	}
	mainLineage := ""
	for _, n := range files.Nodes {
		if strings.HasSuffix(n.Fact.Node.Name, "Main.java") {
			mainLineage = n.Fact.Node.ID
		}
	}
	if mainLineage == "" {
		t.Fatalf("no source_file node for Main.java among %d files", len(files.Nodes))
	}
	var mainCall graph.Version
	for _, e := range edgesOfKind(graph.EdgeCalls, 0) {
		if e.Fact.Edge.Source != nil && e.Fact.Edge.Source.Lineage == mainLineage {
			mainCall = e
		}
	}
	if mainCall.Fact.Edge == nil {
		t.Fatal("no call edge anchored in Main.java")
	}
	// The TypeScript front end is bound by its own syntax-tier resolver in the
	// same generation (the compiler tier is off here, so every binding carries
	// syntax provenance): the app → api call is anchored in app.ts and the
	// class nodes carry the language.
	appLineage := ""
	for _, n := range files.Nodes {
		if strings.HasSuffix(n.Fact.Node.Name, "web/src/app.ts") {
			appLineage = n.Fact.Node.ID
		}
	}
	if appLineage == "" {
		t.Fatalf("no source_file node for app.ts among %d files", len(files.Nodes))
	}
	tsCalls := 0
	for _, e := range edgesOfKind(graph.EdgeCalls, 0) {
		if e.Fact.Edge.Source != nil && e.Fact.Edge.Source.Lineage == appLineage {
			tsCalls++
			if provenance := e.Fact.Edge.Properties["provenance"]; provenance.String == nil || *provenance.String != "syntax" {
				t.Fatalf("TypeScript call edge without syntax provenance: %+v", e.Fact.Edge)
			}
		}
	}
	if tsCalls < 2 {
		t.Fatalf("expected the createClient() and get() call edges anchored in app.ts, got %d", tsCalls)
	}
	tsClassNodes, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repoID, Kind: "class", Limit: 50})
	if err != nil {
		t.Fatal(err)
	}
	tsClasses := 0
	for _, n := range tsClassNodes.Nodes {
		if n.Fact.Node.Name == "AppService" || n.Fact.Node.Name == "ApiClient" {
			tsClasses++
		}
	}
	if tsClasses != 2 {
		t.Fatalf("expected AppService and ApiClient class nodes, got %d among %d classes", tsClasses, len(tsClassNodes.Nodes))
	}

	// Generation 2: add a method to Service and call it; only util and its
	// consumer app are re-projected; Handler's records must stay at generation 1.
	fx.write("util/src/main/java/util/Service.java", strings.Replace(strings.Replace(utilService, "h.handle(\"x\");", "h.handle(\"x\");\n        added();", 1), "@Override public String toString()", "public void added() { }\n    @Override public String toString()", 1))
	second := fx.commit("add method")
	r2 := run(second, 2)
	if r2.Generation != 2 || r2.Metrics.Added == 0 {
		t.Fatalf("incremental run should add records: %+v", r2.Metrics)
	}
	// Adding a method retires nothing: identities of untouched declarations
	// and their call sites continue, and moved anchors are updates.
	if r2.Metrics.Retired != 0 {
		t.Fatalf("adding a method retired %d records: %+v", r2.Metrics.Retired, r2.Metrics)
	}
	// Every node of Handler.java is still the generation-1 version.
	page, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repoID, Kind: "interface", Limit: 10})
	if err != nil || len(page.Nodes) == 0 {
		t.Fatalf("interface nodes: %v %v", page, err)
	}
	for _, n := range page.Nodes {
		if n.GenFrom != 1 {
			t.Fatalf("unchanged interface was rewritten: %+v", n)
		}
	}
	// The new method exists and has exactly one version.
	found := false
	nodes, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repoID, Kind: "method", Limit: 500})
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range nodes.Nodes {
		if n.Fact.Node.Name == "added" {
			found = true
			history, e := store.History(ctx, repoID, n.Fact.Key())
			if e != nil || len(history) != 1 || history[0].GenFrom != 2 || history[0].CommitFrom != second {
				t.Fatalf("history of added(): %+v %v", history, e)
			}
		}
	}
	if !found {
		t.Fatal("added() not projected")
	}

	// Generation 3: delete Main.java; its records are retired with the commit.
	fx.remove("app/src/main/java/app/Main.java")
	third := fx.commit("delete main")
	r3 := run(third, 3)
	if r3.Metrics.Retired == 0 {
		t.Fatalf("deletion should retire records: %+v", r3.Metrics)
	}
	classes, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repoID, Kind: "class", Limit: 100})
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range classes.Nodes {
		if n.Fact.Node.Name == "Main" {
			t.Fatal("Main is still live after deletion")
		}
	}
	history, err := store.History(ctx, repoID, mainCall.Fact.Key())
	if err != nil || len(history) == 0 {
		t.Fatalf("history of the app call edge: %v %v", history, err)
	}
	last := history[len(history)-1]
	if !last.Retired || last.GenTo != 3 || last.CommitTo != third {
		t.Fatalf("app call edge should be retired at generation 3 by %s: %+v", third, last)
	}
	asOf, err := store.GetEdge(ctx, repoID, mainCall.Fact.Edge.ID, 2)
	if err != nil || !asOf.OpenAt(2) {
		t.Fatalf("as-of read at generation 2 should still see the edge: %+v %v", asOf, err)
	}

	// Generation 4: Main.java comes back unchanged; identities continue and
	// the records reopen instead of being minted again.
	fx.write("app/src/main/java/app/Main.java", appMain)
	fourth := fx.commit("restore main")
	r4 := run(fourth, 4)
	if r4.Metrics.Reopened == 0 {
		t.Fatalf("restored file should reopen its records: %+v", r4.Metrics)
	}
	history, err = store.History(ctx, repoID, mainCall.Fact.Key())
	if err != nil || len(history) != 2 || history[1].GenFrom != 4 || history[1].GenTo != 0 {
		t.Fatalf("reopened edge history: %+v %v", history, err)
	}

	// Generation 5: the same commit deployed again publishes an empty generation.
	r5 := run(fourth, 5)
	if r5.Metrics.Added+r5.Metrics.Updated+r5.Metrics.Retired+r5.Metrics.Reopened != 0 {
		t.Fatalf("repeated commit changed the graph: %+v", r5.Metrics)
	}

	// A stale sequence is refused.
	stale := deployment.Request{SchemaVersion: deployment.SchemaVersion, Branch: "main", RepositoryID: repoID, DeploymentID: "stale", DeploymentSequence: 2, TargetCommitSHA: first, DeployedAt: time.Now().UTC(), AnalysisConfigDigest: digest, TriggerKind: deployment.TriggerDeployment}
	if _, err = store.Admit(ctx, deployment.Admission{Request: stale}); !errors.Is(err, deployment.ErrStaleDeployment) {
		t.Fatalf("stale deployment accepted: %v", err)
	}

	// Generation 6: a package-private helper that Service calls. Its file can
	// be renamed later without touching its content because the class is not
	// public.
	fx.write("util/src/main/java/util/Helper.java", "package util;\nclass Helper {\n    static void ping() { }\n}\n")
	serviceWithAdded := strings.Replace(strings.Replace(utilService, "h.handle(\"x\");", "h.handle(\"x\");\n        added();", 1), "@Override public String toString()", "public void added() { }\n    @Override public String toString()", 1)
	fx.write("util/src/main/java/util/Service.java", strings.Replace(serviceWithAdded, "added();", "added();\n        Helper.ping();", 1))
	sixth := fx.commit("add helper")
	run(sixth, 6)
	methods6, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repoID, Kind: "method", Limit: 500})
	if err != nil {
		t.Fatal(err)
	}
	pingID := ""
	for _, n := range methods6.Nodes {
		if n.Fact.Node.Name == "ping" {
			pingID = n.Fact.Node.ID
		}
	}
	if pingID == "" {
		t.Fatal("Helper.ping not projected")
	}
	pingBefore, err := store.GetNode(ctx, repoID, pingID, 0)
	if err != nil {
		t.Fatal(err)
	}
	var pingCall graph.Version
	for _, e := range edgesOfKind(graph.EdgeCalls, 0) {
		if e.Fact.Edge.TargetID == pingID {
			if pingCall.Fact.Edge != nil {
				t.Fatalf("two calls into ping: %s and %s", pingCall.Fact.Edge.ID, e.Fact.Edge.ID)
			}
			pingCall = e
		}
	}
	if pingCall.Fact.Edge == nil {
		t.Fatal("no call edge into Helper.ping")
	}
	sourceFile := func(suffix string) (graph.Version, bool) {
		t.Helper()
		page, e := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repoID, Kind: graph.NodeSourceFile, Limit: 50})
		if e != nil {
			t.Fatal(e)
		}
		for _, n := range page.Nodes {
			if strings.HasSuffix(n.Fact.Node.Name, suffix) {
				return n, true
			}
		}
		return graph.Version{}, false
	}
	helperFile, ok := sourceFile("Helper.java")
	if !ok {
		t.Fatal("no source_file node for Helper.java")
	}

	// Generation 7: the helper file is renamed with its content unchanged.
	// The class and its method continue their IDs under the new lineage and
	// the call edge anchored in Service.java survives (audit finding 4).
	fx.git(fx.remote, "mv", "util/src/main/java/util/Helper.java", "util/src/main/java/util/Helpers.java")
	seventh := fx.commit("rename helper")
	r7 := run(seventh, 7)
	pingAfter, err := store.GetNode(ctx, repoID, pingID, 0)
	if err != nil || pingAfter.GenFrom != 7 || pingAfter.Lineage == pingBefore.Lineage {
		t.Fatalf("ping must continue its ID under the new lineage: %+v %v", pingAfter, err)
	}
	if _, ok = sourceFile("Helpers.java"); !ok {
		t.Fatal("no source_file node for Helpers.java")
	}
	if _, err = store.GetNode(ctx, repoID, helperFile.Fact.Node.ID, 0); !errors.Is(err, graph.ErrNotFound) {
		t.Fatalf("old file node must be retired: %v", err)
	}
	callAfter, err := store.GetEdge(ctx, repoID, pingCall.Fact.Edge.ID, 0)
	if err != nil || !callAfter.OpenAt(7) || callAfter.GenFrom != pingCall.GenFrom {
		t.Fatalf("call edge into the renamed class must survive unchanged: %+v %v", callAfter, err)
	}
	pingHistory, err := store.History(ctx, repoID, graph.Key{Kind: graph.RecordNode, ID: pingID})
	if err != nil || len(pingHistory) != 2 || pingHistory[0].GenTo != 7 || pingHistory[0].Retired || pingHistory[1].GenFrom != 7 || pingHistory[1].GenTo != 0 {
		t.Fatalf("continued method must be replaced, never retired: %+v %v", pingHistory, err)
	}
	if r7.Metrics.Retired != 2 {
		t.Fatalf("a pure rename retires only the old file node and its contains edge: %+v", r7.Metrics)
	}

	// Deployment ordering (audit finding 2): a newer deployment admitted first
	// publishes while an older one is stalled in RUNNING. The older run ends
	// SUPERSEDED and its job completes without doing any work.
	eighth := fx.commit("empty change")
	admitSeq := func(seq uint64, sha string) deployment.Run {
		t.Helper()
		request := deployment.Request{SchemaVersion: deployment.SchemaVersion, Branch: "main", RepositoryID: repoID, DeploymentID: fmt.Sprintf("deployment-%d", seq), DeploymentSequence: seq, TargetCommitSHA: sha, DeployedAt: time.Date(2026, 9, 13, 0, 0, int(seq), 0, time.UTC), AnalysisConfigDigest: digest, TriggerKind: deployment.TriggerDeployment}
		admitted, e := store.Admit(ctx, deployment.Admission{Request: request})
		if e != nil {
			t.Fatal("admit", e)
		}
		return admitted.Run
	}
	newer := admitSeq(9, eighth)
	older := admitSeq(8, seventh)
	older.Phase = deployment.Running
	if older, err = store.UpdateRun(ctx, older); err != nil {
		t.Fatal(err)
	}
	// The newer run is ingested while the older one is stalled, as when
	// another worker claims it first.
	out9, err := runtime.Pipeline.Run(ctx, newer.Key)
	if err != nil || out9.Phase != deployment.Succeeded || out9.Generation != 8 {
		t.Fatalf("newer deployment: %+v %v", out9, err)
	}
	if got, e := store.GetRun(ctx, older.Key); e != nil || got.Phase != deployment.Superseded {
		t.Fatalf("stalled older run must be superseded by the newer publish: %+v %v", got, e)
	}
	out8, err := runtime.Pipeline.Run(ctx, older.Key)
	if err != nil || out8.Phase != deployment.Superseded {
		t.Fatalf("superseded run must complete without work: %+v %v", out8, err)
	}
	if state, e := store.State(ctx, repoID); e != nil || state.LiveGeneration != 8 || state.LiveCommit != eighth {
		t.Fatalf("live must stay at the newer deployment: %+v %v", state, e)
	}

	// Build inputs (audit finding 3): a POM-only commit changes no source
	// file, yet every compilation context it touches must be re-attributed.
	// Lowering the compiler release rewrites the profile of every context.
	// The TypeScript front end has no Maven build, so its three files are
	// not touched by a Maven input change (dist/ is excluded by its tsconfig).
	fx.write("pom.xml", parentPOM("17"))
	ninth := fx.commit("compiler release 17")
	r9 := run(ninth, 10)
	const typescriptFiles = 3
	if r9.Metrics.AffectedFiles != r9.Metrics.Files-typescriptFiles || r9.Metrics.InvalidatedContexts == 0 || r9.Metrics.Resolved == 0 {
		t.Fatalf("POM-only change must re-attribute every Java context: %+v", r9.Metrics)
	}
	if r9.Metrics.Added+r9.Metrics.Updated+r9.Metrics.Retired+r9.Metrics.Reopened != 0 {
		t.Fatalf("re-attribution under the same inputs must not rewrite the graph: %+v", r9.Metrics)
	}
	recorded, err := store.GetGenerationInputs(ctx, repoID, r9.Generation)
	if err != nil || recorded.AnalysisConfigDigest != digest || len(recorded.SourceSets) == 0 {
		t.Fatalf("generation inputs must be recorded: %+v %v", recorded, err)
	}

	// An analysis refresh of the live sequence under another configuration
	// recomputes everything even though the commit is unchanged, and the next
	// deployment under the original configuration recomputes everything again
	// because its baseline was analysed under a different one.
	c2 := c
	c2.WorkDir = t.TempDir()
	c2.DiscoveryLimits.MaxIssues = c.DiscoveryLimits.MaxIssues + 1
	deps2, err := workerapp.Bootstrap(c2, registry)
	if err != nil {
		t.Fatal(err)
	}
	runtime2, err := workerapp.NewRuntime(c2, deps2, storage, "test-owner-2", logger)
	if err != nil {
		t.Fatal(err)
	}
	defer runtime2.Close()
	digest2 := workerapp.ConfigurationDigest(c2, deps2)
	if digest2 == digest {
		t.Fatal("the refresh needs a different analysis configuration")
	}
	refresh := deployment.Request{SchemaVersion: deployment.SchemaVersion, Branch: "main", RepositoryID: repoID, DeploymentID: "deployment-10", DeploymentSequence: 10, TargetCommitSHA: ninth, DeployedAt: time.Date(2026, 9, 13, 0, 0, 10, 0, time.UTC), AnalysisConfigDigest: digest2, TriggerKind: deployment.TriggerAnalysisRefresh}
	admittedRefresh, err := store.Admit(ctx, deployment.Admission{Request: refresh})
	if err != nil || admittedRefresh.Reused {
		t.Fatalf("admit refresh: %+v %v", admittedRefresh, err)
	}
	r10, err := runtime2.Pipeline.Run(ctx, admittedRefresh.Run.Key)
	if err != nil || r10.Phase != deployment.Succeeded || r10.Generation != r9.Generation+1 {
		t.Fatalf("refresh run: %+v %v", r10, err)
	}
	if r10.Metrics.AffectedFiles != r10.Metrics.Files || r10.Metrics.Resolved == 0 || r10.Metrics.Added+r10.Metrics.Updated+r10.Metrics.Retired+r10.Metrics.Reopened != 0 {
		t.Fatalf("a same-commit refresh must recompute everything without rewriting the graph: %+v", r10.Metrics)
	}
	if state, e := store.State(ctx, repoID); e != nil || state.LiveGeneration != r10.Generation || state.LiveRunID != r10.Key.RunID {
		t.Fatalf("refresh must be live: %+v %v", state, e)
	}
	eleventh := fx.commit("after refresh")
	r11 := run(eleventh, 11)
	if r11.Metrics.AffectedFiles != r11.Metrics.Files || r11.Metrics.Resolved == 0 {
		t.Fatalf("a baseline analysed under another configuration must be recomputed: %+v", r11.Metrics)
	}
	// With inputs and configuration unchanged, a no-op commit is empty again.
	twelfth := fx.commit("no-op")
	r12 := run(twelfth, 12)
	if r12.Metrics.AffectedFiles != 0 || r12.Metrics.InvalidatedContexts != 0 {
		t.Fatalf("unchanged inputs must not recompute anything: %+v", r12.Metrics)
	}

	// Lexical search needs no embedding provider: the exact name tier, the
	// identifier tokens and the graph rerank all come from the search index
	// the loader maintains.
	lexical, err := store.HybridSearch(ctx, spannerstore.SearchRequest{RepositoryIDs: []string{repoID}, Text: "ping"})
	if err != nil || len(lexical) == 0 || lexical[0].Node.Fact.Node.Name != "ping" || !lexical[0].ExactMatch || lexical[0].Callers != 1 || lexical[0].Snippet == "" {
		t.Fatalf("lexical search for an exact method name: %+v %v", lexical, err)
	}
	if lexical, err = store.HybridSearch(ctx, spannerstore.SearchRequest{RepositoryIDs: []string{repoID}, Text: "Helper", Kinds: []string{"class"}}); err != nil || len(lexical) != 1 || lexical[0].Node.Fact.Node.Name != "Helper" || !lexical[0].ExactMatch {
		t.Fatalf("lexical search for a class: %+v %v", lexical, err)
	}
	if lexical, err = store.HybridSearch(ctx, spannerstore.SearchRequest{RepositoryIDs: []string{repoID}, Text: "handle string", Mode: spannerstore.SearchLexical, NearPath: "util/src/main/java/util/Service.java"}); err != nil || len(lexical) == 0 || lexical[0].Callers == 0 {
		t.Fatalf("lexical natural-language search with rerank: %+v %v", lexical, err)
	}
	symbols, err := store.FindNodes(ctx, repoID, "", "com.google.adk.agents.LlmAgent", nil, 0, 10)
	if err != nil || len(symbols) != 0 {
		t.Fatalf("unknown qualified name: %+v %v", symbols, err)
	}
	if symbols, err = store.FindNodes(ctx, repoID, "ping", "", nil, 0, 10); err != nil || len(symbols) != 1 || symbols[0].Fact.Node.ID != pingID {
		t.Fatalf("find by name: %+v %v", symbols, err)
	}

	// Search documents carry method text; embeddings and hybrid search work.
	docs, err := store.SearchDocuments(ctx, repoID, "fixture-model", 2, "")
	if err != nil || len(docs.Documents) == 0 {
		t.Fatalf("documents: %+v %v", docs, err)
	}
	withCode := 0
	rows := make([]codesearch.Embedding, 0, len(docs.Documents))
	for _, d := range docs.Documents {
		if strings.Contains(d.Text, "Code:") {
			withCode++
		}
		rows = append(rows, codesearch.Embedding{Document: d, Vector: []float64{1, 0}})
	}
	if withCode == 0 {
		t.Fatal("no document carries source text")
	}
	if err = store.PutEmbeddings(ctx, repoID, "fixture-model", docs.Generation, rows); err != nil {
		t.Fatal(err)
	}
	hits, err := store.HybridSearch(ctx, spannerstore.SearchRequest{RepositoryIDs: []string{repoID}, Model: "fixture-model", Text: "Service call", Vector: []float64{1, 0}, Limit: 5})
	if err != nil || len(hits) == 0 {
		t.Fatalf("hybrid search: %+v %v", hits, err)
	}
	// Source evidence round-trips through the content store.
	for _, n := range nodes.Nodes {
		if n.Fact.Node.Source != nil {
			data, e := store.GetSource(ctx, repoID, n.Fact.Node.Source.ContentSHA256)
			if e != nil || len(data) == 0 {
				t.Fatalf("source evidence: %v", e)
			}
			break
		}
	}
	// Run-local state is gone; the syntax cache and mirror remain.
	entries, _ := os.ReadDir(filepath.Join(c.WorkDir, "runs"))
	if len(entries) != 0 {
		t.Fatalf("run indexes retained: %v", entries)
	}
	// An old commit cannot publish even when an internal caller bypasses
	// API validation and gives it a higher ingestion sequence.
	olderRequest := r12.Request
	olderRequest.DeploymentID, olderRequest.DeploymentSequence, olderRequest.TargetCommitSHA = "rollback-guard", 13, first
	olderRun, err := store.Admit(ctx, deployment.Admission{Request: olderRequest})
	if err != nil {
		t.Fatal(err)
	}
	blocked, err := runtime.Pipeline.Run(ctx, olderRun.Run.Key)
	if err != nil || blocked.Phase != deployment.Superseded || blocked.ErrorCode != "commit_not_forward" {
		t.Fatalf("ancestor with higher sequence must be superseded: %+v %v", blocked, err)
	}
	if state, err := store.State(ctx, repoID); err != nil || state.LiveCommit != twelfth || state.LiveGeneration != r12.Generation || state.Branch != "main" {
		t.Fatalf("rollback changed the live graph: %+v %v", state, err)
	}
}
