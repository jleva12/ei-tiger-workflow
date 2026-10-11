//go:build integration

package spannerstore

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// Run with SPANNER_EMULATOR_HOST=127.0.0.1:19030 after
//   docker run -d --name codegraph-emulator -p 19030:9010 gcr.io/cloud-spanner-emulator/emulator:1.5.52

var (
	store      *Store
	testConfig = "sha256:" + strings.Repeat("ab", 32)
	testOwner  = "worker-1"
	commits    = []string{"", testCommit1, testCommit2, "3333333333333333333333333333333333333333", "4444444444444444444444444444444444444444"}
)

// testVectorLength is the embedding dimension the test database is created with.
const testVectorLength = 4

func TestMain(m *testing.M) {
	if os.Getenv("SPANNER_EMULATOR_HOST") == "" {
		fmt.Println("SPANNER_EMULATOR_HOST not set; skipping integration tests")
		os.Exit(0)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	db := fmt.Sprintf("projects/codegraph-test/instances/test/databases/cg-%d", time.Now().UnixNano())
	if err := ProvisionEmulator(ctx, db, testVectorLength); err != nil {
		fmt.Println("provision:", err)
		os.Exit(1)
	}
	if err := ProvisionEmulator(ctx, db, testVectorLength); err != nil { // idempotent
		fmt.Println("re-provision:", err)
		os.Exit(1)
	}
	var err error
	store, err = New(ctx, Config{Database: db, Scope: "test", CursorSigningKey: []byte("0123456789abcdef0123456789abcdef"), VectorLength: testVectorLength})
	if err != nil {
		fmt.Println("new:", err)
		os.Exit(1)
	}
	if err = store.Ping(ctx); err != nil {
		fmt.Println("ping:", err)
		os.Exit(1)
	}
	code := m.Run()
	store.Close()
	os.Exit(code)
}

// --- helpers ---------------------------------------------------------------

func register(t *testing.T, repo string) deployment.Repository {
	t.Helper()
	r, err := store.PutRepository(context.Background(), deployment.Repository{SchemaVersion: deployment.SchemaVersion, RepositoryID: repo, GitHubURL: "https://github.com/acme/" + repo, IntegrationID: "integration-1"})
	if err != nil {
		t.Fatalf("register %s: %v", repo, err)
	}
	return r
}

func configFor(t *testing.T) string {
	sum := sha256.Sum256([]byte(t.Name()))
	return "sha256:" + hex.EncodeToString(sum[:])
}

func request(repo string, seq uint64, commit string) deployment.Request {
	return deployment.Request{SchemaVersion: deployment.SchemaVersion, Branch: "main", RepositoryID: repo, DeploymentID: fmt.Sprintf("deploy-%d", seq), DeploymentSequence: seq, TargetCommitSHA: commit, DeployedAt: time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC).Add(time.Duration(seq) * time.Minute), AnalysisConfigDigest: testConfig, TriggerKind: deployment.TriggerDeployment}
}

func admit(t *testing.T, repo string, seq uint64, commit string) deployment.Run {
	t.Helper()
	return admitWith(t, request(repo, seq, commit))
}

func admitWith(t *testing.T, r deployment.Request) deployment.Run {
	t.Helper()
	res, err := store.Admit(context.Background(), deployment.Admission{Request: r})
	if err != nil {
		t.Fatalf("admit %s/%d: %v", r.RepositoryID, r.DeploymentSequence, err)
	}
	return res.Run
}

// startGeneration admits a run for seq, marks it RUNNING at generation and
// takes the repository lease.
func startGeneration(t *testing.T, repo string, seq, generation uint64) (deployment.Run, deployment.Lease) {
	t.Helper()
	ctx := context.Background()
	run := admit(t, repo, seq, commits[generation])
	run.Phase, run.Generation = deployment.Running, generation
	if generation > 1 {
		run.BaselineGeneration, run.BaselineCommit = generation-1, commits[generation-1]
	}
	run, err := store.UpdateRun(ctx, run)
	if err != nil {
		t.Fatalf("run to RUNNING: %v", err)
	}
	lease, err := store.AcquireLease(ctx, run.Key, testOwner, time.Minute)
	if err != nil {
		t.Fatalf("acquire lease: %v", err)
	}
	return run, lease
}

func load(t *testing.T, lease deployment.Lease, generation uint64, changes ...graph.Change) LoaderStats {
	t.Helper()
	ctx := context.Background()
	l := store.NewLoader(lease, generation, commits[generation], LoaderOptions{BatchRecords: 2, Concurrency: 3})
	if err := l.Apply(ctx, changes); err != nil {
		t.Fatalf("apply: %v", err)
	}
	if err := l.Flush(ctx); err != nil {
		t.Fatalf("flush: %v", err)
	}
	return l.Stats()
}

func publish(t *testing.T, lease deployment.Lease, run deployment.Run) deployment.Run {
	t.Helper()
	ctx := context.Background()
	run.Metrics = &deployment.Metrics{Nodes: 1}
	out, err := store.Publish(ctx, lease, run)
	if err != nil {
		t.Fatalf("publish generation %d: %v", run.Generation, err)
	}
	if err = store.ReleaseLease(ctx, lease); err != nil {
		t.Fatalf("release: %v", err)
	}
	return out
}

func add(f graph.Fact, lineage string) graph.Change {
	return graph.Change{Op: graph.OpAdd, Key: f.Key(), Lineage: lineage, After: &f}
}
func reopen(f graph.Fact, lineage string) graph.Change {
	return graph.Change{Op: graph.OpReopen, Key: f.Key(), Lineage: lineage, After: &f}
}
func update(before graph.Version, after graph.Fact) graph.Change {
	return graph.Change{Op: graph.OpUpdate, Key: after.Key(), Lineage: before.Lineage, Before: &before, After: &after}
}
func retire(before graph.Version) graph.Change {
	return graph.Change{Op: graph.OpRetire, Key: before.Fact.Key(), Lineage: before.Lineage, Before: &before}
}

func mustNode(t *testing.T, repo, id string, generation uint64) graph.Version {
	t.Helper()
	v, err := node(context.Background(), repo, id, generation)
	if err != nil {
		t.Fatalf("get node %s at %d: %v", id, generation, err)
	}
	return v
}

// node reads a node through GetNodes, as ingestion does: the version open at
// generation (zero is live), or graph.ErrNotFound.
func node(ctx context.Context, repo, id string, generation uint64) (graph.Version, error) {
	return openVersion(ctx, repo, graph.Key{Kind: graph.RecordNode, ID: id}, generation)
}

// edge is node for edges, through GetEdges.
func edge(ctx context.Context, repo, id string, generation uint64) (graph.Version, error) {
	return openVersion(ctx, repo, graph.Key{Kind: graph.RecordEdge, ID: id}, generation)
}

func openVersion(ctx context.Context, repo string, key graph.Key, generation uint64) (graph.Version, error) {
	read := store.GetNodes
	if key.Kind == graph.RecordEdge {
		read = store.GetEdges
	}
	vs, err := read(ctx, repo, []string{key.ID}, generation)
	if err != nil {
		return graph.Version{}, err
	}
	at := generation
	if at == 0 {
		state, err := store.State(ctx, repo)
		if err != nil {
			return graph.Version{}, err
		}
		at = state.LiveGeneration
	}
	if v, ok := vs[key.ID]; ok && v.OpenAt(at) {
		return v, nil
	}
	return graph.Version{}, fmt.Errorf("%w: %s %s at generation %d", graph.ErrNotFound, key.Kind, key.ID, at)
}

// versions returns every stored version of a record, oldest first, as
// written: a closure by an unpublished generation is not masked.
func versions(t *testing.T, repo string, key graph.Key) []graph.Version {
	t.Helper()
	it := store.client.Single().Query(context.Background(), spanner.Statement{
		SQL:    "SELECT " + recordSelect + " FROM CGRecords WHERE RepositoryID=@repo AND RecordKind=@kind AND RecordID=@id ORDER BY GenFrom",
		Params: map[string]any{"repo": repo, "kind": string(key.Kind), "id": key.ID},
	})
	vs, err := collectVersions(it, math.MaxUint64)
	if err != nil {
		t.Fatalf("versions of %s %s: %v", key.Kind, key.ID, err)
	}
	return vs
}

func wantErr(t *testing.T, err, want error, what string) {
	t.Helper()
	if !errors.Is(err, want) {
		t.Fatalf("%s: got %v, want %v", what, err, want)
	}
}

// --- registry -------------------------------------------------------------

func TestRepositoryRegistration(t *testing.T) {
	ctx := context.Background()
	repo := "reg-" + t.Name()
	r := register(t, repo)
	if r.Revision != 1 {
		t.Fatalf("first revision: %d", r.Revision)
	}
	got, err := store.GetRepository(ctx, repo)
	if err != nil || got != r {
		t.Fatalf("get: %+v %v", got, err)
	}
	_, err = store.PutRepository(ctx, deployment.Repository{SchemaVersion: deployment.SchemaVersion, RepositoryID: repo, GitHubURL: r.GitHubURL, IntegrationID: "x"})
	wantErr(t, err, deployment.ErrConflictingReplay, "duplicate insert")
	stale := r
	stale.Revision = 7
	_, err = store.PutRepository(ctx, stale)
	wantErr(t, err, deployment.ErrConflictingReplay, "revision mismatch")
	r.IntegrationID = "integration-2"
	r2, err := store.PutRepository(ctx, r)
	if err != nil || r2.Revision != 2 || r2.IntegrationID != "integration-2" {
		t.Fatalf("cas update: %+v %v", r2, err)
	}
	_, err = store.GetRepository(ctx, "missing-repo")
	wantErr(t, err, deployment.ErrNotFound, "missing repository")
	st, err := store.State(ctx, repo)
	if err != nil || st.LiveGeneration != 0 || st.RepositoryID != repo {
		t.Fatalf("state: %+v %v", st, err)
	}
	_, err = store.State(ctx, "missing-repo")
	wantErr(t, err, graph.ErrNotFound, "missing state")
}

func TestAdmissionDedupeAndStaleSequence(t *testing.T) {
	ctx := context.Background()
	repo := "adm-" + t.Name()
	_, err := store.Admit(ctx, deployment.Admission{Request: request(repo, 1, testCommit1)})
	wantErr(t, err, deployment.ErrNotFound, "admit before registration")
	register(t, repo)

	first, err := store.Admit(ctx, deployment.Admission{SubmissionID: "sub-1", Request: request(repo, 1, testCommit1)})
	if err != nil || first.Reused || first.Run.Phase != deployment.Accepted || first.Run.Revision != 1 || first.Run.Key.RepositoryID != repo {
		t.Fatalf("first admission: %+v %v", first, err)
	}
	submitted, err := store.SubmissionRun(ctx, "sub-1")
	if err != nil || submitted != first.Run.Key {
		t.Fatalf("submission after admission: %+v %v", submitted, err)
	}
	again, err := store.Admit(ctx, deployment.Admission{SubmissionID: "sub-2", Request: request(repo, 1, testCommit1)})
	if err != nil || !again.Reused || again.Run.Key != first.Run.Key {
		t.Fatalf("identical request reuses run: %+v %v", again, err)
	}
	bySubmission, err := store.Admit(ctx, deployment.Admission{SubmissionID: "sub-1", Request: request(repo, 1, testCommit1)})
	if err != nil || !bySubmission.Reused || bySubmission.Run.Key != first.Run.Key {
		t.Fatalf("submission replay: %+v %v", bySubmission, err)
	}
	other := request(repo, 2, testCommit2)
	_, err = store.Admit(ctx, deployment.Admission{SubmissionID: "sub-1", Request: other})
	wantErr(t, err, deployment.ErrConflictingReplay, "submission id reused for a different request")

	refresh := request(repo, 1, testCommit1)
	refresh.AnalysisConfigDigest = "sha256:" + strings.Repeat("cd", 32)
	refresh.TriggerKind = deployment.TriggerAnalysisRefresh
	res, err := store.Admit(ctx, deployment.Admission{Request: refresh})
	if err != nil || res.Reused || res.Run.Key == first.Run.Key {
		t.Fatalf("different configuration is a new run: %+v %v", res, err)
	}
	// Publish sequence 2 at generation 1, then sequence 1 is stale.
	run, lease := startGeneration(t, repo, 2, 1)
	load(t, lease, 1, add(testNode("class", "Stale", nil), ""))
	publish(t, lease, run)
	_, err = store.Admit(ctx, deployment.Admission{Request: withDeployment(request(repo, 1, testCommit1), "deploy-1b")})
	wantErr(t, err, deployment.ErrStaleDeployment, "sequence below succeeded")
	_, err = store.Admit(ctx, deployment.Admission{Request: withDeployment(request(repo, 2, testCommit2), "deploy-2b")})
	wantErr(t, err, deployment.ErrStaleDeployment, "sequence equal to succeeded")
	sameRefresh := request(repo, 2, testCommit2)
	sameRefresh.AnalysisConfigDigest = "sha256:" + strings.Repeat("ef", 32)
	sameRefresh.TriggerKind = deployment.TriggerAnalysisRefresh
	if _, err = store.Admit(ctx, deployment.Admission{Request: sameRefresh}); err != nil {
		t.Fatalf("analysis refresh at the succeeded sequence: %v", err)
	}
	if _, err = store.Admit(ctx, deployment.Admission{Request: request(repo, 3, commits[3])}); err != nil {
		t.Fatalf("later sequence: %v", err)
	}
}

func withDeployment(r deployment.Request, id string) deployment.Request {
	r.DeploymentID = id
	return r
}

func TestUpdateRunTransitions(t *testing.T) {
	ctx := context.Background()
	repo := "run-" + t.Name()
	register(t, repo)
	run := admit(t, repo, 1, testCommit1)
	got, err := store.GetRun(ctx, run.Key)
	if err != nil || got.Key != run.Key || got.Phase != deployment.Accepted {
		t.Fatalf("get run: %+v %v", got, err)
	}
	_, err = store.GetRun(ctx, deployment.RunKey{RepositoryID: repo, RunID: "run-missing"})
	wantErr(t, err, deployment.ErrNotFound, "missing run")

	bad := run
	bad.Phase = deployment.Succeeded
	_, err = store.UpdateRun(ctx, bad)
	wantErr(t, err, deployment.ErrInvalidTransition, "ACCEPTED -> SUCCEEDED")
	bad = run
	bad.Revision = 9
	bad.Phase = deployment.Running
	_, err = store.UpdateRun(ctx, bad)
	wantErr(t, err, deployment.ErrConflictingReplay, "revision mismatch")

	run.Phase, run.Generation = deployment.Running, 1
	started := time.Now().UTC()
	run.StartedAt = &started
	run.Request.DeploymentID = "tampered" // immutable fields come from the stored row
	running, err := store.UpdateRun(ctx, run)
	if err != nil || running.Phase != deployment.Running || running.Revision != 2 || running.Request.DeploymentID != "deploy-1" || !running.UpdatedAt.After(running.AcceptedAt) || running.StartedAt == nil {
		t.Fatalf("to RUNNING: %+v %v", running, err)
	}
	running.Phase = deployment.Succeeded
	_, err = store.UpdateRun(ctx, running)
	wantErr(t, err, deployment.ErrInvalidTransition, "RUNNING -> SUCCEEDED bypassing Publish")
	running.Phase = deployment.Accepted
	_, err = store.UpdateRun(ctx, running)
	wantErr(t, err, deployment.ErrInvalidTransition, "RUNNING -> ACCEPTED")
	running.Phase, running.ErrorCode = deployment.Failed, "resolver_crash"
	failed, err := store.UpdateRun(ctx, running)
	if err != nil || failed.Phase != deployment.Failed || failed.Revision != 3 {
		t.Fatalf("to FAILED: %+v %v", failed, err)
	}
	failed.Phase = deployment.Running
	if _, err = store.UpdateRun(ctx, failed); err != nil {
		t.Fatalf("retry FAILED -> RUNNING: %v", err)
	}
}

// --- lease ---------------------------------------------------------------

func TestLeaseAcquireRenewExpireTakeover(t *testing.T) {
	ctx := context.Background()
	repo := "lease-" + t.Name()
	register(t, repo)
	run1 := deployment.RunKey{RepositoryID: repo, RunID: "run-1"}
	run2 := deployment.RunKey{RepositoryID: repo, RunID: "run-2"}

	_, err := store.AcquireLease(ctx, deployment.RunKey{RepositoryID: "missing-repo", RunID: "r"}, "a", time.Minute)
	wantErr(t, err, deployment.ErrNotFound, "lease on unknown repository")
	a, err := store.AcquireLease(ctx, run1, "owner-a", time.Minute)
	if err != nil || a.Fence.Token != 1 || a.OwnerID != "owner-a" || a.Fence.Key != run1 {
		t.Fatalf("acquire: %+v %v", a, err)
	}
	_, err = store.AcquireLease(ctx, run2, "owner-b", time.Minute)
	wantErr(t, err, deployment.ErrRepositoryBusy, "second run while held")
	_, err = store.AcquireLease(ctx, run1, "owner-b", time.Minute)
	wantErr(t, err, deployment.ErrRepositoryBusy, "same run, other owner")
	renewed, err := store.RenewLease(ctx, a, 2*time.Minute)
	if err != nil || !renewed.ExpiresAt.After(a.ExpiresAt) || renewed.Fence != a.Fence {
		t.Fatalf("renew: %+v %v", renewed, err)
	}
	forged := a
	forged.Fence.Token = 99
	_, err = store.RenewLease(ctx, forged, time.Minute)
	wantErr(t, err, deployment.ErrFenceLost, "renew with wrong token")
	wantErr(t, store.ReleaseLease(ctx, forged), deployment.ErrFenceLost, "release with wrong token")
	if err = store.ReleaseLease(ctx, a); err != nil {
		t.Fatalf("release: %v", err)
	}
	if err = store.ReleaseLease(ctx, a); err != nil {
		t.Fatalf("release is idempotent: %v", err)
	}
	_, err = store.RenewLease(ctx, a, time.Minute)
	wantErr(t, err, deployment.ErrFenceLost, "renew after release")

	b, err := store.AcquireLease(ctx, run2, "owner-b", time.Second)
	if err != nil || b.Fence.Token != 2 {
		t.Fatalf("acquire after release: %+v %v", b, err)
	}
	time.Sleep(1500 * time.Millisecond)
	_, err = store.RenewLease(ctx, b, time.Minute)
	wantErr(t, err, deployment.ErrFenceLost, "renew an expired lease")
	c, err := store.AcquireLease(ctx, run1, "owner-c", time.Minute)
	if err != nil || c.Fence.Token != 3 {
		t.Fatalf("take over expired lease: %+v %v", c, err)
	}
	wantErr(t, store.ReleaseLease(ctx, b), deployment.ErrFenceLost, "release a taken-over lease")
	if err = store.ReleaseLease(ctx, c); err != nil {
		t.Fatal(err)
	}
}

// --- graph generations ----------------------------------------------------

func anchor(lineage, sha string) *graph.SourceAnchor {
	return &graph.SourceAnchor{Lineage: lineage, ContentSHA256: sha, Span: ir.Span{Start: ir.Position{ByteOffset: 0, Line: 1, Column: 1}, End: ir.Position{ByteOffset: 10, Line: 2, Column: 1}}}
}

func TestLoaderAcrossGenerations(t *testing.T) {
	ctx := context.Background()
	repo := "graph-" + t.Name()
	register(t, repo)
	fooLineage := graph.Lineage(repo, "m", "main", "src/Foo.java")
	barLineage := graph.Lineage(repo, "m", "main", "src/Bar.java")
	sha := strings.Repeat("0a", 32)
	fooFile := graph.Fact{Node: &graph.Node{ID: fooLineage, Kind: graph.NodeSourceFile, Name: "Foo.java", Properties: map[string]graph.PropertyValue{"file_path": graph.StringValue("src/Foo.java")}}}
	foo := testNode("class", "Foo", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/Foo.java")})
	foo.Node.Source = anchor(fooLineage, sha)
	bar := testNode("method", "Foo.bar", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/Foo.java"), "source_text": graph.StringValue("void bar() {}")})
	baz := testNode("class", "Baz", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/Bar.java")})
	contains := testEdge(graph.EdgeContains, foo.Node.ID, bar.Node.ID)
	fileContains := testEdge(graph.EdgeContains, fooLineage, foo.Node.ID)
	calls := testEdge(graph.EdgeCalls, baz.Node.ID, bar.Node.ID)

	// Generation 1: everything is added.
	run1, lease1 := startGeneration(t, repo, 1, 1)
	st := load(t, lease1, 1, add(fooFile, fooLineage), add(foo, fooLineage), add(bar, fooLineage), add(baz, barLineage), add(contains, fooLineage), add(fileContains, fooLineage), add(calls, barLineage))
	if st.Added != 7 || st.Batches != 4 || st.Updated+st.Retired+st.Reopened != 0 {
		t.Fatalf("generation 1 stats: %+v", st)
	}
	if _, err := node(ctx, repo, foo.Node.ID, 0); !errors.Is(err, graph.ErrNotFound) {
		t.Fatalf("unpublished generation is invisible at live: %v", err)
	}
	if _, err := node(ctx, repo, foo.Node.ID, 1); !errors.Is(err, graph.ErrInvalid) {
		t.Fatalf("explicit generation above live is rejected: %v", err)
	}
	if n, e, err := store.CountOpen(ctx, repo, 1); err != nil || n != 4 || e != 3 {
		t.Fatalf("count open at 1 before publish: %d %d %v", n, e, err)
	}
	if n, e, err := store.CountOpen(ctx, repo, 0); err != nil || n != 0 || e != 0 {
		t.Fatalf("count open at live before publish: %d %d %v", n, e, err)
	}
	published := publish(t, lease1, run1)
	if published.Phase != deployment.Succeeded || published.FinishedAt == nil || published.Metrics == nil {
		t.Fatalf("published run: %+v", published)
	}
	state, err := store.State(ctx, repo)
	if err != nil || state.LiveGeneration != 1 || state.LiveCommit != testCommit1 || state.LiveRunID != run1.Key.RunID {
		t.Fatalf("state after publish: %+v %v", state, err)
	}
	v := mustNode(t, repo, foo.Node.ID, 0)
	if v.GenFrom != 1 || v.GenTo != 0 || v.CommitFrom != testCommit1 || v.Lineage != fooLineage || v.FactDigest != foo.Digest() || v.Fact.Node.Source == nil {
		t.Fatalf("foo at live: %+v", v)
	}
	if _, err = store.Publish(ctx, lease1, published); !errors.Is(err, deployment.ErrFenceLost) {
		t.Fatalf("publish with a released lease: %v", err)
	}

	// Generation 2: bar changes, foo is retired (edges cascade), baz's file is untouched.
	run2, lease2 := startGeneration(t, repo, 2, 2)
	barV1 := mustNode(t, repo, bar.Node.ID, 0)
	fooV1 := mustNode(t, repo, foo.Node.ID, 0)
	bar2 := bar
	bar2.Node.Properties = map[string]graph.PropertyValue{"file_path": graph.StringValue("src/Foo.java"), "source_text": graph.StringValue("void bar() { log(); }")}
	st = load(t, lease2, 2, update(barV1, bar2), retire(fooV1))
	if st.Updated != 1 || st.Retired != 1 {
		t.Fatalf("generation 2 stats: %+v", st)
	}
	closed, err := store.CloseEdgesTouching(ctx, lease2, 2, testCommit2, []string{foo.Node.ID})
	if err != nil || closed != 2 {
		t.Fatalf("cascade closes contains edges: %d %v", closed, err)
	}
	closed, err = store.CloseEdgesTouching(ctx, lease2, 2, testCommit2, []string{foo.Node.ID})
	if err != nil || closed != 0 {
		t.Fatalf("cascade is idempotent: %d %v", closed, err)
	}
	if n, e, err := store.CountOpen(ctx, repo, 2); err != nil || n != 3 || e != 1 {
		t.Fatalf("count open at 2: %d %d %v", n, e, err)
	}
	// Live readers still see generation 1 untouched.
	if v := mustNode(t, repo, bar.Node.ID, 0); v.GenFrom != 1 || v.GenTo != 0 {
		t.Fatalf("bar at live before publish: %+v", v)
	}
	// Generation 2 wrote foo's closure; reads mask it until the flip.
	if h := versions(t, repo, foo.Key()); len(h) != 1 || h[0].GenTo != 2 || !h[0].Retired || h[0].CommitTo != testCommit2 {
		t.Fatalf("foo's closure by generation 2: %+v", h)
	}
	if v, err := node(ctx, repo, foo.Node.ID, 0); err != nil || v.GenTo != 0 || v.Retired || v.CommitTo != "" {
		t.Fatalf("a closure above live is masked: %+v %v", v, err)
	}
	wrongGen := run2
	wrongGen.Generation = 3
	_, err = store.Publish(ctx, lease2, wrongGen)
	wantErr(t, err, deployment.ErrStaleDeployment, "publish must target live+1")
	publish(t, lease2, run2)

	if v := mustNode(t, repo, bar.Node.ID, 0); v.GenFrom != 2 || graph.Text(v.Fact.Node.Properties, "source_text") != "void bar() { log(); }" {
		t.Fatalf("bar at live: %+v", v)
	}
	if v := mustNode(t, repo, bar.Node.ID, 1); v.GenFrom != 1 || v.GenTo != 2 || v.CommitTo != testCommit2 || v.Retired {
		t.Fatalf("bar at 1: %+v", v)
	}
	if _, err = node(ctx, repo, foo.Node.ID, 0); !errors.Is(err, graph.ErrNotFound) {
		t.Fatalf("retired foo at live: %v", err)
	}
	if v := mustNode(t, repo, foo.Node.ID, 1); v.GenFrom != 1 || v.GenTo != 2 || !v.Retired || v.CommitTo != testCommit2 {
		t.Fatalf("foo at 1 shows its published closure: %+v", v)
	}
	if _, err = edge(ctx, repo, contains.Edge.ID, 0); !errors.Is(err, graph.ErrNotFound) {
		t.Fatalf("cascaded edge at live: %v", err)
	}
	if e, err := edge(ctx, repo, contains.Edge.ID, 1); err != nil || e.GenFrom != 1 {
		t.Fatalf("cascaded edge at 1: %+v %v", e, err)
	}
	if e, err := edge(ctx, repo, calls.Edge.ID, 0); err != nil || e.GenFrom != 1 || e.GenTo != 0 {
		t.Fatalf("untouched edge survives: %+v %v", e, err)
	}
	if n, e, err := store.CountOpen(ctx, repo, 0); err != nil || n != 3 || e != 1 {
		t.Fatalf("open at live: %d nodes %d edges %v", n, e, err)
	}
	h := versions(t, repo, foo.Key())
	if len(h) != 1 || h[0].GenTo != 2 || !h[0].Retired || h[0].CommitTo != testCommit2 {
		t.Fatalf("foo history: %+v", h)
	}
	h = versions(t, repo, bar.Key())
	if len(h) != 2 || h[0].GenFrom != 1 || h[0].GenTo != 2 || h[0].Retired || h[1].GenFrom != 2 || h[1].GenTo != 0 {
		t.Fatalf("bar history: %+v", h)
	}

	// Listing and lineage reads at both generations.
	page, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repo, Kind: "class"})
	if err != nil || page.Generation != 2 || len(page.Nodes) != 1 || page.Nodes[0].Fact.Node.ID != baz.Node.ID {
		t.Fatalf("classes at live: %+v %v", page, err)
	}
	page, err = store.ListNodes(ctx, graph.ListQuery{RepositoryID: repo, Kind: "class", Generation: 1})
	if err != nil || len(page.Nodes) != 2 {
		t.Fatalf("classes at 1: %+v %v", page, err)
	}
	var ids []string
	cursor := ""
	for {
		p, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repo, Generation: 1, Limit: 2, Cursor: cursor})
		if err != nil {
			t.Fatal(err)
		}
		for _, n := range p.Nodes {
			ids = append(ids, n.Fact.Node.ID)
		}
		if cursor = p.NextCursor; cursor == "" {
			break
		}
	}
	if len(ids) != 4 {
		t.Fatalf("paged nodes at 1: %v", ids)
	}
	_, err = store.ListNodes(ctx, graph.ListQuery{RepositoryID: repo, Limit: 2, Cursor: page.NextCursor})
	if page.NextCursor != "" && err == nil {
		t.Fatal("cursor from another query must be rejected")
	}
	byLineage, err := store.RecordsByLineage(ctx, repo, fooLineage, 1)
	if err != nil || len(byLineage) != 5 {
		t.Fatalf("foo lineage at 1: %d %v", len(byLineage), err)
	}
	byLineage, err = store.RecordsByLineage(ctx, repo, fooLineage, 0)
	if err != nil || len(byLineage) != 2 { // file node and bar v2
		t.Fatalf("foo lineage at live: %d %v", len(byLineage), err)
	}

	// Neighbors.
	nb, err := store.Neighbors(ctx, graph.NeighborQuery{RepositoryID: repo, NodeID: foo.Node.ID, Direction: graph.Outgoing, Generation: 1})
	if err != nil || len(nb.Neighbors) != 1 || nb.Neighbors[0].Node == nil || nb.Neighbors[0].Node.Fact.Node.ID != bar.Node.ID {
		t.Fatalf("foo outgoing at 1: %+v %v", nb, err)
	}
	nb, err = store.Neighbors(ctx, graph.NeighborQuery{RepositoryID: repo, NodeID: foo.Node.ID, Direction: graph.Both, Generation: 1})
	if err != nil || len(nb.Neighbors) != 2 {
		t.Fatalf("foo both at 1: %+v %v", nb, err)
	}
	nb, err = store.Neighbors(ctx, graph.NeighborQuery{RepositoryID: repo, NodeID: foo.Node.ID, Direction: graph.Both})
	if err != nil || len(nb.Neighbors) != 0 {
		t.Fatalf("foo at live has no edges: %+v %v", nb, err)
	}
	nb, err = store.Neighbors(ctx, graph.NeighborQuery{RepositoryID: repo, NodeID: bar.Node.ID, Direction: graph.Incoming, EdgeKinds: []string{graph.EdgeCalls}})
	if err != nil || len(nb.Neighbors) != 1 || nb.Neighbors[0].Node.Fact.Node.ID != baz.Node.ID || nb.Neighbors[0].Edge.Fact.Edge.Kind != graph.EdgeCalls {
		t.Fatalf("bar incoming calls: %+v %v", nb, err)
	}
	nb, err = store.Neighbors(ctx, graph.NeighborQuery{RepositoryID: repo, NodeID: bar.Node.ID, Direction: graph.Incoming, EdgeKinds: []string{graph.EdgeInherits}})
	if err != nil || len(nb.Neighbors) != 0 {
		t.Fatalf("kind filter: %+v %v", nb, err)
	}
	// Point reads return the open version, or the latest closed one so a
	// retired record can be recognised and reopened; unknown IDs are absent.
	nodes, err := store.GetNodes(ctx, repo, []string{foo.Node.ID, bar.Node.ID, baz.Node.ID, graph.ID("class", "nope")}, 0)
	if err != nil || len(nodes) != 3 || nodes[bar.Node.ID].GenFrom != 2 || !nodes[foo.Node.ID].Retired || nodes[foo.Node.ID].GenTo != 2 || !nodes[baz.Node.ID].OpenAt(2) {
		t.Fatalf("get nodes at live: %d %+v %v", len(nodes), nodes[foo.Node.ID], err)
	}
	nodes, err = store.GetNodes(ctx, repo, []string{foo.Node.ID, bar.Node.ID}, 1)
	if err != nil || len(nodes) != 2 || nodes[bar.Node.ID].GenFrom != 1 {
		t.Fatalf("get nodes at 1: %d %v", len(nodes), err)
	}

	// Generation 3: foo reappears; an older accepted run is superseded by the publish.
	older := admit(t, repo, 3, commits[3])
	run3, lease3 := startGeneration(t, repo, 4, 3)
	st = load(t, lease3, 3, reopen(foo, fooLineage))
	if st.Reopened != 1 {
		t.Fatalf("generation 3 stats: %+v", st)
	}
	publish(t, lease3, run3)
	if v := mustNode(t, repo, foo.Node.ID, 0); v.GenFrom != 3 {
		t.Fatalf("reopened foo: %+v", v)
	}
	h = versions(t, repo, foo.Key())
	if len(h) != 2 || h[0].GenTo != 2 || h[1].GenFrom != 3 || h[1].GenTo != 0 {
		t.Fatalf("foo history after reopen: %+v", h)
	}
	superseded, err := store.GetRun(ctx, older.Key)
	if err != nil || superseded.Phase != deployment.Superseded || superseded.Revision != 2 || superseded.FinishedAt == nil {
		t.Fatalf("older run superseded: %+v %v", superseded, err)
	}
	if r, err := store.GetRun(ctx, run3.Key); err != nil || r.Phase != deployment.Succeeded || r.Generation != 3 {
		t.Fatalf("published run 3: %+v %v", r, err)
	}
	if _, err = node(ctx, repo, foo.Node.ID, 4); !errors.Is(err, graph.ErrInvalid) {
		t.Fatalf("generation 4 is not live: %v", err)
	}
}

func TestLoaderRejectsLostLease(t *testing.T) {
	ctx := context.Background()
	repo := "fence-" + t.Name()
	register(t, repo)
	run, lease := startGeneration(t, repo, 1, 1)
	if err := store.ReleaseLease(ctx, lease); err != nil {
		t.Fatal(err)
	}
	l := store.NewLoader(lease, 1, testCommit1, LoaderOptions{BatchRecords: 1})
	err := l.Apply(ctx, []graph.Change{add(testNode("class", "X", nil), "")})
	if err == nil {
		err = l.Flush(ctx)
	}
	wantErr(t, err, deployment.ErrFenceLost, "loader without the lease")
	if _, _, err = store.SweepAboveLive(ctx, lease); !errors.Is(err, deployment.ErrFenceLost) {
		t.Fatalf("sweep without the lease: %v", err)
	}
	_ = run
	// A loader for a generation at or below live is refused at commit time.
	run2, lease2 := startGeneration(t, repo, 2, 1)
	publish(t, lease2, run2)
	lease3, err := store.AcquireLease(ctx, run2.Key, testOwner, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	l = store.NewLoader(lease3, 1, testCommit1, LoaderOptions{BatchRecords: 1})
	if err = l.Apply(ctx, []graph.Change{add(testNode("class", "Y", nil), "")}); err == nil {
		err = l.Flush(ctx)
	}
	wantErr(t, err, deployment.ErrFenceLost, "loader at the live generation")
}

// Regression for the audit's finding 1: identity maps, the cascade and the
// loader are fenced commits. A released, taken-over or generation-stale lease
// cannot write anything.
func TestFencedWritesRequireLease(t *testing.T) {
	ctx := context.Background()
	repo := "fenced-" + t.Name()
	register(t, repo)
	lineage := graph.Lineage(repo, "m", "main", "src/A.java")
	ids := []semantic.FileIdentities{{Lineage: lineage, Path: "src/A.java", ContentSHA256: strings.Repeat("11", 32)}}
	run, lease := startGeneration(t, repo, 1, 1)
	if err := store.ReleaseLease(ctx, lease); err != nil {
		t.Fatal(err)
	}
	wantErr(t, store.PutFileIdentities(ctx, lease, 1, ids), deployment.ErrFenceLost, "identities without the lease")
	if _, err := store.GetFileIdentities(ctx, repo, lineage, 1); !errors.Is(err, deployment.ErrNotFound) {
		t.Fatalf("identity map landed without the lease: %v", err)
	}
	_, err := store.CloseEdgesTouching(ctx, lease, 1, testCommit1, []string{graph.ID("entity", "x")})
	wantErr(t, err, deployment.ErrFenceLost, "cascade without the lease")

	// With the lease, maps at the loading generation are accepted; at or
	// below live they are not.
	lease, err = store.AcquireLease(ctx, run.Key, testOwner, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err = store.PutFileIdentities(ctx, lease, 1, ids); err != nil {
		t.Fatal(err)
	}
	publish(t, lease, run)
	lease2, err := store.AcquireLease(ctx, run.Key, testOwner, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	wantErr(t, store.PutFileIdentities(ctx, lease2, 1, ids), deployment.ErrFenceLost, "identities at the live generation")
	if err = store.PutFileIdentities(ctx, lease2, 2, ids); err != nil {
		t.Fatal(err)
	}

	// An expired lease that another owner took over is dead for every write,
	// even though the old holder still has its token.
	short, err := store.AcquireLease(ctx, run.Key, testOwner, time.Nanosecond)
	if err != nil {
		t.Fatal(err)
	}
	taken, err := store.AcquireLease(ctx, run.Key, "worker-2", time.Minute)
	if err != nil {
		t.Fatalf("take over the expired lease: %v", err)
	}
	wantErr(t, store.PutFileIdentities(ctx, short, 2, ids), deployment.ErrFenceLost, "identities after takeover")
	_, err = store.CloseEdgesTouching(ctx, short, 2, testCommit2, []string{graph.ID("entity", "x")})
	wantErr(t, err, deployment.ErrFenceLost, "cascade after takeover")
	_, _, err = store.SweepAboveLive(ctx, short)
	wantErr(t, err, deployment.ErrFenceLost, "sweep after takeover")
	l := store.NewLoader(short, 2, testCommit2, LoaderOptions{BatchRecords: 1})
	if err = l.Apply(ctx, []graph.Change{add(testNode("class", "Late", nil), "")}); err == nil {
		err = l.Flush(ctx)
	}
	wantErr(t, err, deployment.ErrFenceLost, "loader after takeover")
	if n, _, err := store.CountOpen(ctx, repo, 2); err != nil || n != 0 {
		t.Fatalf("stale loader landed rows: %d %v", n, err)
	}
	if err = store.ReleaseLease(ctx, taken); err != nil {
		t.Fatal(err)
	}
}

// Regression for the audit's finding 2: publication enforces deployment
// order. A stalled older RUNNING run is superseded when a newer deployment
// publishes, and an equal sequence may publish only as an analysis refresh.
func TestPublishEnforcesDeploymentSequence(t *testing.T) {
	ctx := context.Background()
	repo := "order-" + t.Name()
	register(t, repo)
	start := func(run deployment.Run, generation uint64) (deployment.Run, deployment.Lease) {
		t.Helper()
		run.Phase, run.Generation = deployment.Running, generation
		if generation > 1 {
			state, err := store.State(ctx, repo)
			if err != nil {
				t.Fatal(err)
			}
			run.BaselineGeneration, run.BaselineCommit = state.LiveGeneration, state.LiveCommit
		}
		run, err := store.UpdateRun(ctx, run)
		if err != nil {
			t.Fatal(err)
		}
		lease, err := store.AcquireLease(ctx, run.Key, testOwner, time.Minute)
		if err != nil {
			t.Fatal(err)
		}
		return run, lease
	}
	refresh := func(seq uint64, commit, salt string) deployment.Request {
		r := request(repo, seq, commit)
		r.TriggerKind = deployment.TriggerAnalysisRefresh
		r.AnalysisConfigDigest = "sha256:" + strings.Repeat(salt, 32)
		return r
	}
	// Admitted before anything is live: a deployment at sequence 1 that a
	// stalled worker left RUNNING, the deployment at sequence 2, and an
	// analysis refresh of sequence 2.
	older := admit(t, repo, 1, commits[1])
	older.Phase = deployment.Running
	older, err := store.UpdateRun(ctx, older)
	if err != nil {
		t.Fatal(err)
	}
	a := admit(t, repo, 2, commits[2])
	b := admitWith(t, refresh(2, commits[2], "b1"))

	// The refresh publishes generation 1 and supersedes the stalled run.
	b, leaseB := start(b, 1)
	load(t, leaseB, 1, add(testNode("class", "B", nil), ""))
	b = publish(t, leaseB, b)
	if got, err := store.GetRun(ctx, older.Key); err != nil || got.Phase != deployment.Superseded || got.FinishedAt == nil {
		t.Fatalf("stalled RUNNING run at an older sequence must be superseded: %+v %v", got, err)
	}

	// The deployment at the same sequence as the live refresh cannot publish
	// over it, and live does not move.
	a, leaseA := start(a, 2)
	load(t, leaseA, 2, add(testNode("class", "A", nil), ""))
	a.Metrics = &deployment.Metrics{Nodes: 1}
	_, err = store.Publish(ctx, leaseA, a)
	wantErr(t, err, deployment.ErrStaleDeployment, "equal sequence without a refresh")
	if st, err := store.State(ctx, repo); err != nil || st.LiveGeneration != 1 || st.LiveRunID != b.Key.RunID {
		t.Fatalf("live moved: %+v %v", st, err)
	}
	if err = store.ReleaseLease(ctx, leaseA); err != nil {
		t.Fatal(err)
	}

	// Another refresh of the same sequence may publish over the live refresh.
	c := admitWith(t, refresh(2, commits[2], "c1"))
	c, leaseC := start(c, 2)
	if _, _, err = store.SweepAboveLive(ctx, leaseC); err != nil {
		t.Fatal(err)
	}
	load(t, leaseC, 2, add(testNode("class", "C", nil), ""))
	publish(t, leaseC, c)

	// A later deployment publishes and supersedes the stuck run at sequence 2.
	d := admit(t, repo, 3, commits[3])
	d, leaseD := start(d, 3)
	load(t, leaseD, 3, add(testNode("class", "D", nil), ""))
	publish(t, leaseD, d)
	if got, err := store.GetRun(ctx, a.Key); err != nil || got.Phase != deployment.Superseded {
		t.Fatalf("stuck run at a lower sequence must be superseded: %+v %v", got, err)
	}
	if st, err := store.State(ctx, repo); err != nil || st.LiveGeneration != 3 || st.LiveCommit != commits[3] {
		t.Fatalf("final state: %+v %v", st, err)
	}
}

// Regression for the audit's finding 4: the cascade never closes edges into a
// node that is open at the loading generation, such as an entity that a
// renamed file continued under its new lineage.
func TestCascadeSkipsNodesOpenAtGeneration(t *testing.T) {
	ctx := context.Background()
	repo := "cascade-" + t.Name()
	register(t, repo)
	oldLineage := graph.Lineage(repo, "m", "main", "src/Helper.java")
	newLineage := graph.Lineage(repo, "m", "main", "src/Helpers.java")
	callerLineage := graph.Lineage(repo, "m", "main", "src/Caller.java")
	sha := strings.Repeat("0c", 32)
	helper := testNode("class", "Helper", nil)
	helper.Node.Source = anchor(oldLineage, sha)
	caller := testNode("class", "Caller", nil)
	caller.Node.Source = anchor(callerLineage, sha)
	calls := testEdge(graph.EdgeCalls, caller.Node.ID, helper.Node.ID)
	calls.Edge.Source = anchor(callerLineage, sha)
	run1, lease1 := startGeneration(t, repo, 1, 1)
	load(t, lease1, 1, add(helper, oldLineage), add(caller, callerLineage), add(calls, callerLineage))
	publish(t, lease1, run1)

	// Generation 2: Helper.java was renamed; the class keeps its ID and moves
	// to the new lineage. The caller's file is unchanged.
	run2, lease2 := startGeneration(t, repo, 2, 2)
	before := mustNode(t, repo, helper.Node.ID, 0)
	moved := helper
	moved.Node.Source = anchor(newLineage, sha)
	change := update(before, moved)
	change.Lineage = newLineage
	load(t, lease2, 2, change)
	closed, err := store.CloseEdgesTouching(ctx, lease2, 2, testCommit2, []string{helper.Node.ID})
	if err != nil || closed != 0 {
		t.Fatalf("cascade closed %d edges of a continued entity: %v", closed, err)
	}
	publish(t, lease2, run2)
	if e, err := edge(ctx, repo, calls.Edge.ID, 0); err != nil || e.GenFrom != 1 || e.GenTo != 0 {
		t.Fatalf("call edge into the renamed class must survive: %+v %v", e, err)
	}
	if v := mustNode(t, repo, helper.Node.ID, 0); v.GenFrom != 2 || v.Lineage != newLineage {
		t.Fatalf("continued class at live: %+v", v)
	}
	if h := versions(t, repo, helper.Key()); len(h) != 2 || h[0].Retired || h[0].GenTo != 2 {
		t.Fatalf("continued class history must show a replacement, not a retirement: %+v", h)
	}
}

func TestSweepAboveLive(t *testing.T) {
	ctx := context.Background()
	repo := "sweep-" + t.Name()
	register(t, repo)
	lineage := graph.Lineage(repo, "m", "main", "src/A.java")
	a := testNode("class", "A", nil)
	b := testNode("class", "B", nil)
	run1, lease1 := startGeneration(t, repo, 1, 1)
	load(t, lease1, 1, add(a, lineage), add(b, lineage))
	if err := store.PutFileIdentities(ctx, lease1, 1, []semantic.FileIdentities{{Lineage: lineage, Path: "src/A.java", ContentSHA256: strings.Repeat("11", 32)}}); err != nil {
		t.Fatal(err)
	}
	publish(t, lease1, run1)

	// Generation 2 is loaded then abandoned: A retired, B updated, C added.
	run2, lease2 := startGeneration(t, repo, 2, 2)
	aV1, bV1 := mustNode(t, repo, a.Node.ID, 0), mustNode(t, repo, b.Node.ID, 0)
	b2 := b
	b2.Node.QualifiedName = "changed.B"
	c := testNode("class", "C", nil)
	load(t, lease2, 2, retire(aV1), update(bV1, b2), add(c, lineage))
	if err := store.PutFileIdentities(ctx, lease2, 2, []semantic.FileIdentities{{Lineage: lineage, Path: "src/A.java", ContentSHA256: strings.Repeat("22", 32)}}); err != nil {
		t.Fatal(err)
	}
	if n, e, err := store.CountOpen(ctx, repo, 2); err != nil || n != 2 || e != 0 {
		t.Fatalf("count at abandoned generation: %d %d %v", n, e, err)
	}
	run2.Phase, run2.ErrorCode = deployment.Failed, "worker_lost"
	if _, err := store.UpdateRun(ctx, run2); err != nil {
		t.Fatal(err)
	}

	deleted, reopened, err := store.SweepAboveLive(ctx, lease2)
	if err != nil || deleted != 2 || reopened != 2 { // B v2 and C deleted; A v1 and B v1 reopened
		t.Fatalf("sweep: deleted %d reopened %d %v", deleted, reopened, err)
	}
	if n, e, err := store.CountOpen(ctx, repo, 2); err != nil || n != 2 || e != 0 {
		t.Fatalf("after sweep generation 2 equals live: %d %d %v", n, e, err)
	}
	if v := mustNode(t, repo, a.Node.ID, 0); v.GenTo != 0 || v.Retired || v.CommitTo != "" {
		t.Fatalf("A reopened: %+v", v)
	}
	if h := versions(t, repo, b.Key()); len(h) != 1 || h[0].GenTo != 0 || h[0].Fact.Node.QualifiedName == "changed.B" {
		t.Fatalf("B history after sweep: %+v", h)
	}
	if h := versions(t, repo, c.Key()); len(h) != 0 {
		t.Fatalf("C is gone: %+v", h)
	}
	if ids, err := store.GetFileIdentities(ctx, repo, lineage, 2); err != nil || ids.ContentSHA256 != strings.Repeat("11", 32) {
		t.Fatalf("identities above live swept: %+v %v", ids, err)
	}
	deleted, reopened, err = store.SweepAboveLive(ctx, lease2)
	if err != nil || deleted != 0 || reopened != 0 {
		t.Fatalf("second sweep is a no-op: %d %d %v", deleted, reopened, err)
	}
	// The same generation number loads again cleanly and publishes.
	load(t, lease2, 2, add(c, lineage))
	run2, err = store.GetRun(ctx, run2.Key)
	if err != nil {
		t.Fatal(err)
	}
	run2.Phase = deployment.Running
	if run2, err = store.UpdateRun(ctx, run2); err != nil {
		t.Fatal(err)
	}
	publish(t, lease2, run2)
	if n, _, err := store.CountOpen(ctx, repo, 0); err != nil || n != 3 {
		t.Fatalf("after retry: %d %v", n, err)
	}
}

// Regression for the audit's finding 3: what a generation was computed from
// is recorded under the lease before the flip and swept with an abandoned
// generation.
func TestGenerationInputsRoundTripAndSweep(t *testing.T) {
	ctx := context.Background()
	repo := "inputs-" + t.Name()
	register(t, repo)
	inputs := func(context, digest string) deployment.GenerationInputs {
		return deployment.GenerationInputs{SchemaVersion: deployment.SchemaVersion, ContextID: "build:" + strings.Repeat(context, 32), AnalysisConfigDigest: testConfig, SourceSets: map[string]string{"m:main": "sha256:" + strings.Repeat(digest, 32)}}
	}
	if _, err := store.GetGenerationInputs(ctx, repo, 1); !errors.Is(err, deployment.ErrNotFound) {
		t.Fatalf("unknown generation: %v", err)
	}
	run1, lease1 := startGeneration(t, repo, 1, 1)
	wantErr(t, store.PutGenerationInputs(ctx, lease1, 1, deployment.GenerationInputs{}), deployment.ErrInvalidRequest, "invalid inputs")
	if err := store.PutGenerationInputs(ctx, lease1, 1, inputs("a", "11")); err != nil {
		t.Fatal(err)
	}
	load(t, lease1, 1, add(testNode("class", "A", nil), ""))
	publish(t, lease1, run1)
	got, err := store.GetGenerationInputs(ctx, repo, 1)
	if err != nil || got.ContextID != "build:"+strings.Repeat("a", 32) || got.SourceSets["m:main"] != "sha256:"+strings.Repeat("11", 32) || got.AnalysisConfigDigest != testConfig {
		t.Fatalf("inputs at 1: %+v %v", got, err)
	}
	// Generation 2 records its inputs and is abandoned; the sweep removes them.
	run2, lease2 := startGeneration(t, repo, 2, 2)
	wantErr(t, store.PutGenerationInputs(ctx, lease2, 1, inputs("b", "22")), deployment.ErrFenceLost, "inputs at the live generation")
	if err = store.PutGenerationInputs(ctx, lease2, 2, inputs("b", "22")); err != nil {
		t.Fatal(err)
	}
	if got, err = store.GetGenerationInputs(ctx, repo, 2); err != nil || got.SourceSets["m:main"] != "sha256:"+strings.Repeat("22", 32) {
		t.Fatalf("inputs at 2 before sweep: %+v %v", got, err)
	}
	if _, _, err = store.SweepAboveLive(ctx, lease2); err != nil {
		t.Fatal(err)
	}
	if _, err = store.GetGenerationInputs(ctx, repo, 2); !errors.Is(err, deployment.ErrNotFound) {
		t.Fatalf("inputs above live must be swept: %v", err)
	}
	if got, err = store.GetGenerationInputs(ctx, repo, 1); err != nil || got.ContextID != "build:"+strings.Repeat("a", 32) {
		t.Fatalf("inputs at live survive the sweep: %+v %v", got, err)
	}
	if err = store.ReleaseLease(ctx, lease2); err != nil {
		t.Fatal(err)
	}
	wantErr(t, store.PutGenerationInputs(ctx, lease2, 2, inputs("c", "33")), deployment.ErrFenceLost, "inputs without the lease")
	_ = run2
}

// The loader writes each document-bearing node's CGSearchDocuments row, which
// the readers' lexical search and exact-name lookups use; nodes without a
// document get none. A row an unpublished generation rewrote no longer
// matches the live record's search hash, so readers drop it, and sweeping the
// abandoned generation restores the live document and removes the rows of
// nodes it added.
func TestLoaderWritesSearchDocuments(t *testing.T) {
	ctx := context.Background()
	repo := "documents-" + t.Name()
	register(t, repo)
	sha := strings.Repeat("0d", 32)
	path := "core/src/main/java/flows/LlmFlow.java"
	lineage := graph.Lineage(repo, "core", "main", path)
	method := func(name, qualified, text string) graph.Fact {
		f := testNode("method", name, map[string]graph.PropertyValue{"file_path": graph.StringValue(path), "source_text": graph.StringValue(text), "signature": graph.StringValue(strings.SplitN(text, "{", 2)[0])})
		f.Node.QualifiedName = qualified
		f.Node.Source = anchor(lineage, sha)
		return f
	}
	run := method("runAsyncImpl", "runAsyncImpl(com.acme.agents.InvocationContext)", "protected Flowable<Event> runAsyncImpl(InvocationContext ctx) { return steps(ctx); }")
	list := testNode(graph.NodeExternalSymbol, "java.util.List", nil)
	run1, lease1 := startGeneration(t, repo, 1, 1)
	load(t, lease1, 1, add(run, lineage), add(list, ""))
	publish(t, lease1, run1)

	doc, ok := codesearch.FromNode(*run.Node)
	if !ok {
		t.Fatal("a method is a document")
	}
	want := searchDocumentRow{Repository: repo, NodeID: run.Node.ID, Generation: 1, Kind: "method", Name: "runAsyncImpl", QualifiedName: run.Node.QualifiedName,
		FilePath: path, Document: doc, Identifiers: codesearch.IdentifierTerms(run.Node.Name, run.Node.QualifiedName)}
	if got, ok := searchDocument(t, repo, run.Node.ID); !ok || got != want {
		t.Fatalf("search document:\n got %+v\nwant %+v", got, want)
	}
	if hash := liveSearchHash(t, repo, run.Node.ID); hash != doc.Hash {
		t.Fatalf("record search hash %q, document %q", hash, doc.Hash)
	}
	if got, ok := searchDocument(t, repo, list.Node.ID); ok {
		t.Fatalf("an external symbol has no document: %+v", got)
	}

	// Generation 2 rewrites the document and adds a node, then is abandoned.
	run2, lease2 := startGeneration(t, repo, 2, 2)
	before := mustNode(t, repo, run.Node.ID, 0)
	changed := method("runAsyncImpl", run.Node.QualifiedName, "protected Flowable<Event> runAsyncImpl(InvocationContext ctx) { return fastPath(ctx); }")
	added := method("fastPath", "fastPath(com.acme.agents.InvocationContext)", "Flowable<Event> fastPath(InvocationContext ctx) { return Flowable.empty(); }")
	load(t, lease2, 2, update(before, changed), add(added, lineage))
	if got, ok := searchDocument(t, repo, run.Node.ID); !ok || got.Generation != 2 || got.Document.Hash == doc.Hash || got.Document.Hash == liveSearchHash(t, repo, run.Node.ID) {
		t.Fatalf("a loading generation's document must not match live: %+v", got)
	}
	run2.Phase, run2.ErrorCode = deployment.Failed, "abandoned"
	if _, err := store.UpdateRun(ctx, run2); err != nil {
		t.Fatal(err)
	}
	if _, _, err := store.SweepAboveLive(ctx, lease2); err != nil {
		t.Fatal(err)
	}
	if got, ok := searchDocument(t, repo, run.Node.ID); !ok || got != want {
		t.Fatalf("sweep must restore the live document:\n got %+v\nwant %+v", got, want)
	}
	if got, ok := searchDocument(t, repo, added.Node.ID); ok {
		t.Fatalf("sweep must remove the abandoned node's document: %+v", got)
	}
	if err := store.ReleaseLease(ctx, lease2); err != nil {
		t.Fatal(err)
	}
}

// searchDocument reads a node's CGSearchDocuments row.
func searchDocument(t *testing.T, repo, id string) (searchDocumentRow, bool) {
	t.Helper()
	row, err := store.client.Single().ReadRow(context.Background(), "CGSearchDocuments", spanner.Key{repo, id},
		[]string{"Generation", "Kind", "Name", "NameLower", "QualifiedName", "FilePath", "DocumentHash", "DocumentVersion", "DocumentText", "Identifiers"})
	if rowNotFound(err) {
		return searchDocumentRow{}, false
	}
	if err != nil {
		t.Fatalf("search document %s: %v", id, err)
	}
	r := searchDocumentRow{Repository: repo, NodeID: id, Document: codesearch.Document{NodeID: id}}
	var generation, version int64
	var lower string
	if err = row.Columns(&generation, &r.Kind, &r.Name, &lower, &r.QualifiedName, &r.FilePath, &r.Document.Hash, &version, &r.Document.Text, &r.Identifiers); err != nil {
		t.Fatal(err)
	}
	if lower != strings.ToLower(r.Name) {
		t.Fatalf("NameLower %q for %q", lower, r.Name)
	}
	r.Generation, r.Document.Version = uint64(generation), int(version)
	return r, true
}

// liveSearchHash is the search hash of a node's record open at live.
func liveSearchHash(t *testing.T, repo, id string) string {
	t.Helper()
	ctx := context.Background()
	state, err := store.State(ctx, repo)
	if err != nil {
		t.Fatal(err)
	}
	it := store.client.Single().Query(ctx, spanner.Statement{
		SQL:    "SELECT SearchHash FROM CGRecords WHERE RepositoryID=@repo AND RecordKind='node' AND RecordID=@id AND " + openPredicate(""),
		Params: map[string]any{"repo": repo, "id": id, "gen": int64(state.LiveGeneration)},
	})
	defer it.Stop()
	row, err := nextRow(it)
	if err != nil || row == nil {
		t.Fatalf("live record %s: %v", id, err)
	}
	var hash spanner.NullString
	if err = row.Columns(&hash); err != nil {
		t.Fatal(err)
	}
	return hash.StringVal
}

// --- file identities, content ------------------------------------------------

func TestFileIdentitiesAsOf(t *testing.T) {
	ctx := context.Background()
	repo := "ids-" + t.Name()
	register(t, repo)
	lineage := graph.Lineage(repo, "m", "main", "src/A.java")
	mk := func(gen uint64) semantic.FileIdentities {
		return semantic.FileIdentities{Lineage: lineage, FileID: ir.FileID(fmt.Sprintf("file-%d", gen)), Path: "src/A.java", ContentSHA256: strings.Repeat(fmt.Sprintf("%02d", gen), 32),
			Declarations: []semantic.DeclarationIdentity{{DeclarationID: "d1", EntityID: graph.ID("class", "A"), Kind: "class", Name: "A", Span: ir.Span{Start: ir.Position{Line: 1, Column: 1}, End: ir.Position{ByteOffset: 5, Line: 1, Column: 6}}}}}
	}
	lease, err := store.AcquireLease(ctx, deployment.RunKey{RepositoryID: repo, RunID: "run-ids"}, testOwner, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.PutFileIdentities(ctx, lease, 1, []semantic.FileIdentities{mk(1)}); err != nil {
		t.Fatal(err)
	}
	if err := store.PutFileIdentities(ctx, lease, 3, []semantic.FileIdentities{mk(3)}); err != nil {
		t.Fatal(err)
	}
	if err := store.PutFileIdentities(ctx, lease, 3, []semantic.FileIdentities{mk(3)}); err != nil {
		t.Fatalf("rewrite is idempotent: %v", err)
	}
	for gen, want := range map[uint64]string{1: "01", 2: "01", 3: "03", 9: "03"} {
		got, err := store.GetFileIdentities(ctx, repo, lineage, gen)
		if err != nil || got.ContentSHA256 != strings.Repeat(want, 32) || len(got.Declarations) != 1 || got.Declarations[0].EntityID != graph.ID("class", "A") {
			t.Fatalf("identities at %d: %+v %v", gen, got, err)
		}
	}
	_, err = store.GetFileIdentities(ctx, repo, lineage, 0)
	wantErr(t, err, deployment.ErrNotFound, "live generation is 0")
	_, err = store.GetFileIdentities(ctx, repo, graph.Lineage(repo, "m", "main", "src/B.java"), 5)
	wantErr(t, err, deployment.ErrNotFound, "unknown lineage")
}

func TestContentPutGetDedupe(t *testing.T) {
	ctx := context.Background()
	repo := "content-" + t.Name()
	data := bytes.Repeat([]byte("package acme;\nclass A {}\n"), 200)
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	existed, err := store.PutSource(ctx, repo, sha, data)
	if err != nil || existed {
		t.Fatalf("first put: %v %v", existed, err)
	}
	existed, err = store.PutSource(ctx, repo, sha, data)
	if err != nil || !existed {
		t.Fatalf("second put dedupes: %v %v", existed, err)
	}
	if got, ok := retained(t, repo, sha); !ok || !bytes.Equal(got, data) {
		t.Fatalf("retained: %d bytes %v", len(got), ok)
	}
	if _, ok := retained(t, repo, strings.Repeat("ff", 32)); ok {
		t.Fatal("unknown content retained")
	}
	_, err = store.PutSource(ctx, repo, strings.Repeat("ff", 32), data)
	wantErr(t, err, deployment.ErrInvalidRequest, "hash mismatch")
	empty := sha256.Sum256(nil)
	if _, err = store.PutSource(ctx, repo, hex.EncodeToString(empty[:]), nil); err != nil {
		t.Fatalf("empty file: %v", err)
	}
	if got, ok := retained(t, repo, hex.EncodeToString(empty[:])); !ok || len(got) != 0 {
		t.Fatalf("empty file retained: %v %v", got, ok)
	}
}

// retained reads a CGContent row as the readers do: gunzipped, and checked
// against its size and the hash that keys it.
func retained(t *testing.T, repo, sha string) ([]byte, bool) {
	t.Helper()
	row, err := store.client.Single().ReadRow(context.Background(), "CGContent", spanner.Key{repo, sha}, []string{"UncompressedBytes", "Payload"})
	if rowNotFound(err) {
		return nil, false
	}
	if err != nil {
		t.Fatalf("content %s: %v", sha, err)
	}
	var size int64
	var z []byte
	if err = row.Columns(&size, &z); err != nil {
		t.Fatal(err)
	}
	data, err := gunzipBytes(z, size)
	if err != nil {
		t.Fatalf("content %s: %v", sha, err)
	}
	if sum := sha256.Sum256(data); int64(len(data)) != size || hex.EncodeToString(sum[:]) != sha {
		t.Fatalf("content %s differs from its hash or size", sha)
	}
	return data, true
}

// --- search ---------------------------------------------------------------

func unit(i, dims int) []float64 {
	v := make([]float64, dims)
	v[i%dims] = 1
	return v
}

// The embedding pass reads the documents whose embedding is missing or stale
// and stores vectors only for documents that still match the live record;
// the readers' vector search uses what it stores.
func TestSearchDocumentsAndEmbeddings(t *testing.T) {
	ctx := context.Background()
	repo := "search-" + t.Name()
	register(t, repo)
	const model, dims = "test-embed", 4
	nodes := []graph.Fact{
		testNode("class", "OrderService", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/order/OrderService.java"), "source_text": graph.StringValue("class OrderService { void placeOrder() {} }")}),
		testNode("method", "OrderService.placeOrder", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/order/OrderService.java"), "source_text": graph.StringValue("void placeOrder() { inventory.reserve(); }")}),
		testNode("method", "InventoryService.reserve", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/inventory/InventoryService.java"), "source_text": graph.StringValue("void reserve() { stock--; }")}),
		testNode(graph.NodeExternalSymbol, "java.util.List", nil),
	}
	run, lease := startGeneration(t, repo, 1, 1)
	var changes []graph.Change
	for _, n := range nodes {
		changes = append(changes, add(n, ""))
	}
	load(t, lease, 1, changes...)
	_, err := store.SearchDocuments(ctx, repo, model, dims, "")
	if err != nil {
		t.Fatal(err)
	}
	publish(t, lease, run)

	page, err := store.SearchDocuments(ctx, repo, model, dims, "")
	if err != nil || page.Generation != 1 || len(page.Documents) != 3 || page.NextCursor != "" {
		t.Fatalf("documents to embed: %+v %v", page, err)
	}
	if pending, err := store.PendingDocuments(ctx, repo, model, dims); err != nil || pending != 3 {
		t.Fatalf("pending documents: %d %v", pending, err)
	}
	var rows []codesearch.Embedding
	for i, d := range page.Documents {
		want, ok := codesearch.FromNode(*nodes[slices.IndexFunc(nodes, func(f graph.Fact) bool { return f.Node.ID == d.NodeID })].Node)
		if !ok || d != want || d.Version != codesearch.DocumentVersion || d.Text == "" {
			t.Fatalf("document: %+v, want %+v", d, want)
		}
		rows = append(rows, codesearch.Embedding{Document: d, Vector: unit(i, dims)})
	}
	// The embedding column and its vector index have the database's fixed
	// length: other dimensions are rejected before Spanner sees them.
	_, err = store.SearchDocuments(ctx, repo, model, 8, "")
	wantErr(t, err, deployment.ErrInvalidRequest, "documents for another dimension")
	err = store.PutEmbeddings(ctx, repo, model, 1, []codesearch.Embedding{{Document: rows[0].Document, Vector: unit(0, 8)}})
	wantErr(t, err, deployment.ErrInvalidRequest, "embeddings of another dimension")
	err = store.PutEmbeddings(ctx, repo, model, 2, rows)
	wantErr(t, err, deployment.ErrStaleDeployment, "embeddings for a generation that is not live")
	stale := rows[0]
	stale.Document.Hash = strings.Repeat("00", 32)
	err = store.PutEmbeddings(ctx, repo, model, 1, []codesearch.Embedding{stale})
	wantErr(t, err, deployment.ErrStaleDeployment, "document hash no longer matches the node")
	if err = store.PutEmbeddings(ctx, repo, model, 1, rows); err != nil {
		t.Fatalf("put embeddings: %v", err)
	}
	page, err = store.SearchDocuments(ctx, repo, model, dims, "")
	if err != nil || len(page.Documents) != 0 {
		t.Fatalf("everything embedded: %+v %v", page, err)
	}
	if pending, err := store.PendingDocuments(ctx, repo, model, dims); err != nil || pending != 0 {
		t.Fatalf("pending after the pass: %d %v", pending, err)
	}
	// Each vector is stored with the document it embeds, for the generation
	// it was computed at.
	stored := map[string]bool{}
	it := store.client.Single().Read(ctx, "CGSearchEmbeddings", spanner.Key{repo, model, int64(dims)}.AsPrefix(),
		[]string{"NodeID", "DocumentHash", "DocumentVersion", "DocumentText", "Embedding", "UpdatedGeneration"})
	defer it.Stop()
	for {
		row, err := nextRow(it)
		if err != nil {
			t.Fatal(err)
		}
		if row == nil {
			break
		}
		var id, hash, text string
		var version, generation int64
		var vector []float32
		if err = row.Columns(&id, &hash, &version, &text, &vector, &generation); err != nil {
			t.Fatal(err)
		}
		i := slices.IndexFunc(rows, func(r codesearch.Embedding) bool { return r.Document.NodeID == id })
		if i < 0 || hash != rows[i].Document.Hash || int(version) != codesearch.DocumentVersion || text != rows[i].Document.Text || generation != 1 || !slices.Equal(vector, float32Vector(rows[i].Vector)) {
			t.Fatalf("stored embedding of %s: %s %d %v %d", id, hash, version, vector, generation)
		}
		stored[id] = true
	}
	if len(stored) != len(rows) {
		t.Fatalf("stored %d embeddings, want %d", len(stored), len(rows))
	}
	// A new text makes the stored vector stale: the next pass embeds it again.
	run2, lease2 := startGeneration(t, repo, 2, 2)
	before := mustNode(t, repo, rows[0].Document.NodeID, 0)
	changed := before.Fact
	n := *changed.Node
	n.Properties = map[string]graph.PropertyValue{"file_path": graph.StringValue(graph.Text(n.Properties, "file_path")), "source_text": graph.StringValue("// rewritten")}
	changed.Node = &n
	load(t, lease2, 2, update(before, changed))
	publish(t, lease2, run2)
	page, err = store.SearchDocuments(ctx, repo, model, dims, "")
	if err != nil || page.Generation != 2 || len(page.Documents) != 1 || page.Documents[0].NodeID != n.ID || page.Documents[0].Hash == rows[0].Document.Hash {
		t.Fatalf("a changed document is stale: %+v %v", page, err)
	}
}

// Fenced commits check the lease identity, not its expiry: a renewal writes
// the expiry cell alone, so it never contends with in-flight batch commits,
// and a takeover, which rewrites the identity cells, is what fences a stale
// holder out. Publish and RenewLease still verify expiry strictly.
func TestFenceIgnoresExpiryUntilTakeover(t *testing.T) {
	ctx := context.Background()
	repo := "fence-expiry-" + t.Name()
	register(t, repo)
	lineage := graph.Lineage(repo, "m", "main", "src/A.java")
	ids := []semantic.FileIdentities{{Lineage: lineage, Path: "src/A.java", ContentSHA256: strings.Repeat("22", 32)}}
	ids2 := []semantic.FileIdentities{{Lineage: lineage, Path: "src/A.java", ContentSHA256: strings.Repeat("33", 32)}}
	run, lease := startGeneration(t, repo, 1, 1)
	if err := store.ReleaseLease(ctx, lease); err != nil {
		t.Fatal(err)
	}
	short, err := store.AcquireLease(ctx, run.Key, testOwner, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	renewed, err := store.RenewLease(ctx, short, time.Second)
	if err != nil || renewed.Fence != short.Fence || renewed.OwnerID != short.OwnerID || !renewed.ExpiresAt.After(short.ExpiresAt.Add(-time.Second)) {
		t.Fatalf("renewal must move only the expiry: %+v %v", renewed, err)
	}
	time.Sleep(1500 * time.Millisecond)
	// Expired, not taken over: the strict check refuses a renewal, the fence
	// still admits an idempotent batch of the unpublished generation.
	_, err = store.RenewLease(ctx, short, time.Second)
	wantErr(t, err, deployment.ErrFenceLost, "renew an expired lease")
	if err = store.PutFileIdentities(ctx, short, 1, ids); err != nil {
		t.Fatalf("fenced write after expiry without takeover: %v", err)
	}
	// Taken over: the identity cells changed, so the old holder is fenced out.
	next, err := store.AcquireLease(ctx, run.Key, "worker-2", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	wantErr(t, store.PutFileIdentities(ctx, short, 1, ids2), deployment.ErrFenceLost, "fenced write after takeover")
	if err = store.PutFileIdentities(ctx, next, 1, ids2); err != nil {
		t.Fatal(err)
	}
	if err = store.ReleaseLease(ctx, next); err != nil {
		t.Fatal(err)
	}
}
