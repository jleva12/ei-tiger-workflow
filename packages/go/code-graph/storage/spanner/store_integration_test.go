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
	v, err := store.GetNode(context.Background(), repo, id, generation)
	if err != nil {
		t.Fatalf("get node %s at %d: %v", id, generation, err)
	}
	return v
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
	runs, next, err := store.ListRuns(ctx, repo, 10, "")
	if err != nil || len(runs) != 2 || next != "" {
		t.Fatalf("list runs: %d %q %v", len(runs), next, err)
	}
	page1, next, err := store.ListRuns(ctx, repo, 1, "")
	if err != nil || len(page1) != 1 || next == "" {
		t.Fatalf("first page: %d %q %v", len(page1), next, err)
	}
	page2, next2, err := store.ListRuns(ctx, repo, 1, next)
	if err != nil || len(page2) != 1 || next2 != "" || page2[0].Key == page1[0].Key {
		t.Fatalf("second page: %d %q %v", len(page2), next2, err)
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
	if _, err := store.GetNode(ctx, repo, foo.Node.ID, 0); !errors.Is(err, graph.ErrNotFound) {
		t.Fatalf("unpublished generation is invisible at live: %v", err)
	}
	if _, err := store.GetNode(ctx, repo, foo.Node.ID, 1); !errors.Is(err, graph.ErrInvalid) {
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
	if h, err := store.History(ctx, repo, foo.Key()); err != nil || len(h) != 1 || h[0].GenTo != 0 || h[0].Retired {
		t.Fatalf("history masks closure above live: %+v %v", h, err)
	}
	wrongGen := run2
	wrongGen.Generation = 3
	_, err = store.Publish(ctx, lease2, wrongGen)
	wantErr(t, err, deployment.ErrStaleDeployment, "publish must target live+1")
	publish(t, lease2, run2)

	// The change set of generation 2: bar updated, foo retired; the cascade
	// retired the two contains edges. Generation 1 added every node.
	changes, err := store.Changes(ctx, repo, 0, graph.RecordNode, 10, "")
	if err != nil || changes.Generation != 2 || changes.Commit != testCommit2 || changes.Added != 0 || changes.Updated != 1 || changes.Retired != 1 || len(changes.Changes) != 2 {
		t.Fatalf("node changes at 2: %+v %v", changes, err)
	}
	ops := map[string]ChangeOp{}
	for _, c := range changes.Changes {
		ops[c.ID] = c.Op
		if c.Op == ChangeUpdated && (c.Before == nil || c.Before.GenTo != 2 || c.Version.GenFrom != 2) {
			t.Fatalf("updated change carries both versions: %+v", c)
		}
	}
	if ops[bar.Node.ID] != ChangeUpdated || ops[foo.Node.ID] != ChangeRetired {
		t.Fatalf("ops: %v", ops)
	}
	if changes, err = store.Changes(ctx, repo, 2, graph.RecordEdge, 10, ""); err != nil || changes.Retired != 2 || changes.Updated != 0 || changes.Added != 0 || len(changes.Changes) != 2 {
		t.Fatalf("edge changes at 2: %+v %v", changes, err)
	}
	first, err := store.Changes(ctx, repo, 1, graph.RecordNode, 3, "")
	if err != nil || first.Added != 4 || len(first.Changes) != 3 || first.NextCursor == "" {
		t.Fatalf("generation 1 page: %+v %v", first, err)
	}
	if rest, err := store.Changes(ctx, repo, 1, graph.RecordNode, 3, first.NextCursor); err != nil || len(rest.Changes) != 1 || rest.NextCursor != "" || rest.Changes[0].ID == first.Changes[2].ID {
		t.Fatalf("generation 1 second page: %+v %v", rest, err)
	}
	if _, err = store.Changes(ctx, repo, 3, graph.RecordNode, 10, ""); !errors.Is(err, graph.ErrInvalid) {
		t.Fatalf("generation above live: %v", err)
	}

	if v := mustNode(t, repo, bar.Node.ID, 0); v.GenFrom != 2 || graph.Text(v.Fact.Node.Properties, "source_text") != "void bar() { log(); }" {
		t.Fatalf("bar at live: %+v", v)
	}
	if v := mustNode(t, repo, bar.Node.ID, 1); v.GenFrom != 1 || v.GenTo != 2 || v.CommitTo != testCommit2 || v.Retired {
		t.Fatalf("bar at 1: %+v", v)
	}
	if _, err = store.GetNode(ctx, repo, foo.Node.ID, 0); !errors.Is(err, graph.ErrNotFound) {
		t.Fatalf("retired foo at live: %v", err)
	}
	if v := mustNode(t, repo, foo.Node.ID, 1); v.GenFrom != 1 || v.GenTo != 2 || !v.Retired || v.CommitTo != testCommit2 {
		t.Fatalf("foo at 1 shows its published closure: %+v", v)
	}
	if _, err = store.GetEdge(ctx, repo, contains.Edge.ID, 0); !errors.Is(err, graph.ErrNotFound) {
		t.Fatalf("cascaded edge at live: %v", err)
	}
	if e, err := store.GetEdge(ctx, repo, contains.Edge.ID, 1); err != nil || e.GenFrom != 1 {
		t.Fatalf("cascaded edge at 1: %+v %v", e, err)
	}
	if e, err := store.GetEdge(ctx, repo, calls.Edge.ID, 0); err != nil || e.GenFrom != 1 || e.GenTo != 0 {
		t.Fatalf("untouched edge survives: %+v %v", e, err)
	}
	h, err := store.History(ctx, repo, foo.Key())
	if err != nil || len(h) != 1 || h[0].GenTo != 2 || !h[0].Retired || h[0].CommitTo != testCommit2 {
		t.Fatalf("foo history: %+v %v", h, err)
	}
	h, err = store.History(ctx, repo, bar.Key())
	if err != nil || len(h) != 2 || h[0].GenFrom != 1 || h[0].GenTo != 2 || h[0].Retired || h[1].GenFrom != 2 || h[1].GenTo != 0 {
		t.Fatalf("bar history: %+v %v", h, err)
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
	edges, err := store.ListEdges(ctx, graph.ListQuery{RepositoryID: repo})
	if err != nil || len(edges.Edges) != 1 || edges.Edges[0].Fact.Edge.ID != calls.Edge.ID {
		t.Fatalf("edges at live: %+v %v", edges, err)
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
	h, err = store.History(ctx, repo, foo.Key())
	if err != nil || len(h) != 2 || h[0].GenTo != 2 || h[1].GenFrom != 3 || h[1].GenTo != 0 {
		t.Fatalf("foo history after reopen: %+v %v", h, err)
	}
	superseded, err := store.GetRun(ctx, older.Key)
	if err != nil || superseded.Phase != deployment.Superseded || superseded.Revision != 2 || superseded.FinishedAt == nil {
		t.Fatalf("older run superseded: %+v %v", superseded, err)
	}
	if r, err := store.GetRun(ctx, run3.Key); err != nil || r.Phase != deployment.Succeeded || r.Generation != 3 {
		t.Fatalf("published run 3: %+v %v", r, err)
	}
	if _, err = store.GetNode(ctx, repo, foo.Node.ID, 4); !errors.Is(err, graph.ErrInvalid) {
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
	if e, err := store.GetEdge(ctx, repo, calls.Edge.ID, 0); err != nil || e.GenFrom != 1 || e.GenTo != 0 {
		t.Fatalf("call edge into the renamed class must survive: %+v %v", e, err)
	}
	if v := mustNode(t, repo, helper.Node.ID, 0); v.GenFrom != 2 || v.Lineage != newLineage {
		t.Fatalf("continued class at live: %+v", v)
	}
	if h, err := store.History(ctx, repo, helper.Key()); err != nil || len(h) != 2 || h[0].Retired || h[0].GenTo != 2 {
		t.Fatalf("continued class history must show a replacement, not a retirement: %+v %v", h, err)
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
	if h, err := store.History(ctx, repo, b.Key()); err != nil || len(h) != 1 || h[0].GenTo != 0 || h[0].Fact.Node.QualifiedName == "changed.B" {
		t.Fatalf("B history after sweep: %+v %v", h, err)
	}
	if h, err := store.History(ctx, repo, c.Key()); err != nil || len(h) != 0 {
		t.Fatalf("C is gone: %+v %v", h, err)
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

// Regression for the audit's finding 5: the lexical branch runs on the search
// index and needs no embeddings; identifier fragments and substrings match;
// exact names win; connectivity and path proximity rerank ties; a document
// written by an unpublished or abandoned generation never speaks for live.
func TestLexicalSearchAndRerank(t *testing.T) {
	ctx := context.Background()
	repo := "lexical-" + t.Name()
	register(t, repo)
	sha := strings.Repeat("0d", 32)
	agentPath, flowPath, testPath := "core/src/main/java/agents/LlmAgent.java", "core/src/main/java/flows/LlmFlow.java", "core/src/test/java/agents/LlmAgentTest.java"
	agentLineage, flowLineage, testLineage := graph.Lineage(repo, "core", "main", agentPath), graph.Lineage(repo, "core", "main", flowPath), graph.Lineage(repo, "core", "test", testPath)
	node := func(kind, name, qualified, path, text, lineage string) graph.Fact {
		f := testNode(kind, name, map[string]graph.PropertyValue{"file_path": graph.StringValue(path), "source_text": graph.StringValue(text), "signature": graph.StringValue(strings.SplitN(text, "{", 2)[0])})
		f.Node.QualifiedName = qualified
		f.Node.Source = anchor(lineage, sha)
		return f
	}
	agent := node("class", "LlmAgent", "com.acme.agents.LlmAgent", agentPath, "public class LlmAgent extends BaseAgent { }", agentLineage)
	runImpl := node("method", "runAsyncImpl", "runAsyncImpl(com.acme.agents.InvocationContext)", agentPath, "protected Flowable<Event> runAsyncImpl(InvocationContext ctx) { return llmFlow.run(ctx); }", agentLineage)
	flowRun := node("method", "run", "run(com.acme.agents.InvocationContext)", flowPath, "public Flowable<Event> run(InvocationContext ctx) { return steps(ctx); }", flowLineage)
	helper := node("method", "runHelper", "runHelper()", testPath, "void runHelper() { run(); run(); run(); run(); run(); }", testLineage)
	call1 := testEdge(graph.EdgeCalls, runImpl.Node.ID, flowRun.Node.ID)
	call1.Edge.Source = anchor(agentLineage, sha)
	call2 := testEdge(graph.EdgeCalls, helper.Node.ID, flowRun.Node.ID)
	call2.Edge.Source = anchor(testLineage, sha)
	run1, lease1 := startGeneration(t, repo, 1, 1)
	load(t, lease1, 1, add(agent, agentLineage), add(runImpl, agentLineage), add(flowRun, flowLineage), add(helper, testLineage), add(call1, agentLineage), add(call2, testLineage))
	publish(t, lease1, run1)
	search := func(q SearchRequest) []SearchHit {
		t.Helper()
		q.RepositoryIDs = []string{repo}
		hits, err := store.HybridSearch(ctx, q)
		if err != nil {
			t.Fatalf("search %+v: %v", q, err)
		}
		return hits
	}
	first := func(hits []SearchHit) string {
		if len(hits) == 0 {
			return ""
		}
		return hits[0].Node.Fact.Node.Name
	}

	// An identifier fragment reaches the camelCase name through the identifier tokens.
	hits := search(SearchRequest{Text: "async"})
	if first(hits) != "runAsyncImpl" || hits[0].Lexical <= 0 || hits[0].Vector != 0 || hits[0].ExactMatch || len(hits[0].Matched) == 0 || hits[0].Snippet == "" || hits[0].Signature == "" {
		t.Fatalf("identifier fragment: %+v", hits)
	}
	// A partial identifier reaches it through the substring tokens.
	if hits = search(SearchRequest{Text: "AsyncImp"}); first(hits) != "runAsyncImpl" {
		t.Fatalf("substring: %+v", hits)
	}
	// The exact name wins over a document that repeats the word.
	hits = search(SearchRequest{Text: "run"})
	if first(hits) != "run" || !hits[0].ExactMatch || hits[0].Callers != 2 {
		t.Fatalf("exact name first with its callers: %+v", hits)
	}
	// An exact qualified name is recognised too.
	hits = search(SearchRequest{Text: "com.acme.agents.LlmAgent"})
	if first(hits) != "LlmAgent" || !hits[0].ExactMatch {
		t.Fatalf("exact qualified name: %+v", hits)
	}
	// Level two: near the caller's file and better connected ranks first.
	hits = search(SearchRequest{Text: "run ctx", NearPath: "core/src/main/java/flows/Steps.java"})
	if first(hits) != "run" {
		t.Fatalf("connectivity and proximity rerank: %+v", hits)
	}
	for _, h := range hits {
		switch h.Node.Fact.Node.Name {
		case "run":
			if h.Callers != 2 || h.Callees != 0 {
				t.Fatalf("degrees of run: %+v", h)
			}
		case "runAsyncImpl":
			if h.Callers != 0 || h.Callees != 1 {
				t.Fatalf("degrees of runAsyncImpl: %+v", h)
			}
		}
	}
	// Filters apply to the lexical branch.
	if hits = search(SearchRequest{Text: "run", Kinds: []string{"method"}, PathPrefix: "core/src/test/"}); len(hits) != 1 || first(hits) != "runHelper" {
		t.Fatalf("filters: %+v", hits)
	}
	if hits = search(SearchRequest{Text: "run", Mode: SearchLexical}); len(hits) == 0 {
		t.Fatalf("lexical mode: %+v", hits)
	}
	_, err := store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Text: "run", Mode: SearchSemantic})
	wantErr(t, err, deployment.ErrInvalidRequest, "semantic mode without a vector")
	_, err = store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Text: "run", Mode: "fuzzy"})
	wantErr(t, err, deployment.ErrInvalidRequest, "unknown mode")

	// A document rewritten by an unpublished generation does not speak for
	// the live node, and an abandoned generation's rows are restored.
	run2, lease2 := startGeneration(t, repo, 2, 2)
	before := mustNode(t, repo, flowRun.Node.ID, 0)
	changed := flowRun
	changed.Node.Properties = map[string]graph.PropertyValue{"file_path": graph.StringValue(flowPath), "source_text": graph.StringValue("public Flowable<Event> run(InvocationContext ctx) { return fastPath(ctx); }")}
	load(t, lease2, 2, update(before, changed))
	for _, h := range search(SearchRequest{Text: "run"}) {
		if h.Node.Fact.Node.ID == flowRun.Node.ID {
			t.Fatalf("stale document must not match live: %+v", h)
		}
	}
	run2.Phase, run2.ErrorCode = deployment.Failed, "abandoned"
	if _, err = store.UpdateRun(ctx, run2); err != nil {
		t.Fatal(err)
	}
	if _, _, err = store.SweepAboveLive(ctx, lease2); err != nil {
		t.Fatal(err)
	}
	if hits = search(SearchRequest{Text: "steps"}); first(hits) != "run" {
		t.Fatalf("sweep must restore the live document: %+v", hits)
	}
	if err = store.ReleaseLease(ctx, lease2); err != nil {
		t.Fatal(err)
	}
}

func TestFindNodesAndBackfill(t *testing.T) {
	ctx := context.Background()
	repo := "find-" + t.Name()
	register(t, repo)
	lineage := graph.Lineage(repo, "m", "main", "src/Foo.java")
	sha := strings.Repeat("0e", 32)
	foo := testNode("class", "Foo", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/Foo.java")})
	foo.Node.QualifiedName = "com.acme.Foo"
	foo.Node.Source = anchor(lineage, sha)
	bar := testNode("method", "bar", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/Foo.java"), "source_text": graph.StringValue("void bar() { }")})
	bar.Node.QualifiedName = "bar()"
	bar.Node.Source = anchor(lineage, sha)
	other := testNode("method", "bar", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/Foo.java"), "source_text": graph.StringValue("void bar(int n) { }")})
	other.Node.ID, other.Node.QualifiedName = graph.ID("method", "bar-int"), "bar(int)"
	other.Node.Source = anchor(lineage, sha)
	run1, lease1 := startGeneration(t, repo, 1, 1)
	load(t, lease1, 1, add(foo, lineage), add(bar, lineage), add(other, lineage))
	publish(t, lease1, run1)

	nodes, err := store.FindNodes(ctx, repo, "bar", "", nil, 0, 10)
	if err != nil || len(nodes) != 2 {
		t.Fatalf("by name: %+v %v", nodes, err)
	}
	if nodes, err = store.FindNodes(ctx, repo, "", "bar(int)", nil, 0, 10); err != nil || len(nodes) != 1 || nodes[0].Fact.Node.ID != other.Node.ID {
		t.Fatalf("by qualified name: %+v %v", nodes, err)
	}
	if nodes, err = store.FindNodes(ctx, repo, "", "com.acme.Foo", []string{"class"}, 0, 10); err != nil || len(nodes) != 1 || nodes[0].Fact.Node.ID != foo.Node.ID {
		t.Fatalf("qualified class with kind: %+v %v", nodes, err)
	}
	if nodes, err = store.FindNodes(ctx, repo, "BAR", "", []string{"class"}, 0, 10); err != nil || len(nodes) != 0 {
		t.Fatalf("kind filter and case folding: %+v %v", nodes, err)
	}
	_, err = store.FindNodes(ctx, repo, "bar", "bar()", nil, 0, 10)
	wantErr(t, err, graph.ErrInvalid, "name and qualified name together")

	// A qualified lookup among many same-named declarations must not depend
	// on the wanted one sorting into the candidate window of a name search.
	var crowd []graph.Fact
	for i := 0; i < 3*candidateFactor+2; i++ {
		m := testNode("method", "run", map[string]graph.PropertyValue{"file_path": graph.StringValue("src/Foo.java")})
		m.Node.ID, m.Node.QualifiedName = graph.ID("method", fmt.Sprintf("run-%02d", i)), fmt.Sprintf("com.acme.Runner%02d.run", i)
		m.Node.Source = anchor(lineage, sha)
		crowd = append(crowd, m)
	}
	changes := make([]graph.Change, 0, len(crowd))
	for _, m := range crowd {
		changes = append(changes, add(m, lineage))
	}
	run2, lease2 := startGeneration(t, repo, 2, 2)
	load(t, lease2, 2, changes...)
	publish(t, lease2, run2)
	last := crowd[len(crowd)-1]
	if nodes, err = store.FindNodes(ctx, repo, "", last.Node.QualifiedName, []string{"method"}, 0, 1); err != nil || len(nodes) != 1 || nodes[0].Fact.Node.ID != last.Node.ID {
		t.Fatalf("qualified lookup beyond the name window: %+v %v", nodes, err)
	}

	// A graph loaded before documents existed is backfilled at live.
	if _, err = store.client.Apply(ctx, []*spanner.Mutation{spanner.Delete("CGSearchDocuments", spanner.KeyRange{Start: spanner.Key{repo}, End: spanner.Key{repo}, Kind: spanner.ClosedClosed})}); err != nil {
		t.Fatal(err)
	}
	if nodes, err = store.FindNodes(ctx, repo, "bar", "", nil, 0, 10); err != nil || len(nodes) != 0 {
		t.Fatalf("no documents, no lookup: %+v %v", nodes, err)
	}
	written, err := store.BackfillSearchDocuments(ctx, repo)
	if err != nil || written != uint64(3+len(crowd)) {
		t.Fatalf("backfill: %d %v", written, err)
	}
	if written, err = store.BackfillSearchDocuments(ctx, repo); err != nil || written != 0 {
		t.Fatalf("backfill is idempotent: %d %v", written, err)
	}
	if nodes, err = store.FindNodes(ctx, repo, "bar", "", nil, 0, 10); err != nil || len(nodes) != 2 {
		t.Fatalf("after backfill: %+v %v", nodes, err)
	}
	hits, err := store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Text: "Foo"})
	if err != nil || len(hits) == 0 || hits[0].Node.Fact.Node.ID != foo.Node.ID || !hits[0].ExactMatch {
		t.Fatalf("search after backfill: %+v %v", hits, err)
	}
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
	got, err := store.GetSource(ctx, repo, sha)
	if err != nil || !bytes.Equal(got, data) {
		t.Fatalf("get: %d bytes %v", len(got), err)
	}
	_, err = store.GetSource(ctx, repo, strings.Repeat("ff", 32))
	wantErr(t, err, deployment.ErrNotFound, "unknown content")
	_, err = store.PutSource(ctx, repo, strings.Repeat("ff", 32), data)
	wantErr(t, err, deployment.ErrInvalidRequest, "hash mismatch")
	empty := sha256.Sum256(nil)
	if _, err = store.PutSource(ctx, repo, hex.EncodeToString(empty[:]), nil); err != nil {
		t.Fatalf("empty file: %v", err)
	}
	if got, err = store.GetSource(ctx, repo, hex.EncodeToString(empty[:])); err != nil || len(got) != 0 {
		t.Fatalf("empty get: %v %v", got, err)
	}
}

// --- search ---------------------------------------------------------------

func unit(i, dims int) []float64 {
	v := make([]float64, dims)
	v[i%dims] = 1
	return v
}

func TestSearchDocumentsEmbeddingsHybrid(t *testing.T) {
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
	var rows []codesearch.Embedding
	byID := map[string]int{}
	for i, d := range page.Documents {
		if d.Version != codesearch.DocumentVersion || d.Text == "" {
			t.Fatalf("document: %+v", d)
		}
		rows = append(rows, codesearch.Embedding{Document: d, Vector: unit(i, dims)})
		byID[d.NodeID] = i
	}
	target := page.Documents[1].NodeID
	// The embedding column and its vector index have the database's fixed
	// length: other dimensions are rejected before Spanner sees them.
	_, err = store.SearchDocuments(ctx, repo, model, 8, "")
	wantErr(t, err, deployment.ErrInvalidRequest, "documents for another dimension")
	err = store.PutEmbeddings(ctx, repo, model, 1, []codesearch.Embedding{{Document: rows[0].Document, Vector: unit(0, 8)}})
	wantErr(t, err, deployment.ErrInvalidRequest, "embeddings of another dimension")
	_, err = store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Model: model, Vector: unit(0, 8)})
	wantErr(t, err, deployment.ErrInvalidRequest, "query vector of another dimension")
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
	hits, err := store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Model: model, Vector: unit(1, dims), Limit: 5})
	if err != nil || len(hits) != 3 || hits[0].Node.Fact.Node.ID != target || math.Abs(hits[0].Vector-1) > 1e-9 || hits[0].Lexical != 0 {
		t.Fatalf("vector search: %+v %v", hits, err)
	}
	hits, err = store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Model: model, Text: "inventory reserve stock"})
	if err != nil || len(hits) == 0 || hits[0].Node.Fact.Node.Name != "InventoryService.reserve" || hits[0].Lexical <= 0 || hits[0].Vector != 0 {
		t.Fatalf("lexical search: %+v %v", hits, err)
	}
	hits, err = store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Model: model, Text: "placeOrder", Vector: unit(byID[graph.ID("class", "OrderService")], dims), Limit: 2})
	if err != nil || len(hits) != 2 || hits[0].Score <= hits[1].Score {
		t.Fatalf("hybrid: %+v %v", hits, err)
	}
	for _, h := range hits {
		if h.RepositoryID != repo || h.Node.GenFrom != 1 {
			t.Fatalf("hit: %+v", h)
		}
	}
	hits, err = store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Model: model, Vector: unit(0, dims), Kinds: []string{"method"}})
	if err != nil || len(hits) != 2 {
		t.Fatalf("kind filter: %+v %v", hits, err)
	}
	for _, h := range hits {
		if h.Node.Fact.Node.Kind != "method" {
			t.Fatalf("kind filter leaked: %+v", h)
		}
	}
	hits, err = store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Model: model, Vector: unit(0, dims), PathPrefix: "src/inventory/"})
	if err != nil || len(hits) != 1 || hits[0].Node.Fact.Node.Name != "InventoryService.reserve" {
		t.Fatalf("path filter: %+v %v", hits, err)
	}
	hits, err = store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo, "search-unknown"}, Model: model, Text: "order"})
	wantErr(t, err, graph.ErrNotFound, "unknown repository")
	_, err = store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Model: model})
	wantErr(t, err, deployment.ErrInvalidRequest, "empty query")
	_ = hits
}

func TestNaturalLanguageSearch(t *testing.T) {
	ctx := context.Background()
	repo := "nl-" + t.Name()
	register(t, repo)
	sha := strings.Repeat("0e", 32)
	svcPath, orderPath, invPath, ctlPath := "core/src/main/java/shop/OrderService.java", "core/src/main/java/shop/Order.java", "core/src/main/java/inventory/InventoryService.java", "web/src/main/java/shop/CheckoutController.java"
	lineage := func(module, path string) string { return graph.Lineage(repo, module, "main", path) }
	node := func(kind, name, qualified, path, text, module string) graph.Fact {
		f := testNode(kind, name, map[string]graph.PropertyValue{"file_path": graph.StringValue(path), "source_text": graph.StringValue(text), "signature": graph.StringValue(strings.SplitN(text, "{", 2)[0])})
		f.Node.QualifiedName = qualified
		f.Node.Source = anchor(lineage(module, path), sha)
		return f
	}
	orderService := node("class", "OrderService", "shop.OrderService", svcPath, "public class OrderService { /* places orders and reserves stock */ }", "core")
	placeOrder := node("method", "placeOrder", "placeOrder(shop.Cart)", svcPath, "public Order placeOrder(Cart cart) { inventory.reserve(cart); return orders.save(cart); }", "core")
	orderField := node("field", "order", "order", svcPath, "private Order order;", "core")
	order := node("class", "Order", "shop.Order", orderPath, "public class Order { long id; }", "core")
	reserve := node("method", "reserve", "reserve(shop.Cart)", invPath, "public void reserve(Cart cart) { stock.hold(cart.items()); }", "core")
	checkout := node("method", "checkout", "checkout(shop.Cart)", ctlPath, "public Response checkout(Cart cart) { return orderService.placeOrder(cart); }", "web")
	call1 := testEdge(graph.EdgeCalls, checkout.Node.ID, placeOrder.Node.ID)
	call1.Edge.Source = anchor(lineage("web", ctlPath), sha)
	call2 := testEdge(graph.EdgeCalls, placeOrder.Node.ID, reserve.Node.ID)
	call2.Edge.Source = anchor(lineage("core", svcPath), sha)
	run1, lease1 := startGeneration(t, repo, 1, 1)
	load(t, lease1, 1,
		add(orderService, lineage("core", svcPath)), add(placeOrder, lineage("core", svcPath)), add(orderField, lineage("core", svcPath)),
		add(order, lineage("core", orderPath)), add(reserve, lineage("core", invPath)), add(checkout, lineage("web", ctlPath)),
		add(call1, lineage("web", ctlPath)), add(call2, lineage("core", svcPath)))
	publish(t, lease1, run1)
	search := func(text string) []SearchHit {
		t.Helper()
		hits, err := store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Text: text})
		if err != nil {
			t.Fatalf("search %q: %v", text, err)
		}
		return hits
	}
	find := func(hits []SearchHit, name string) *SearchHit {
		for i := range hits {
			if hits[i].Node.Fact.Node.Name == name {
				return &hits[i]
			}
		}
		return nil
	}

	// A question naming a symbol puts that symbol first as an exact match,
	// and a plain word equal to a method name is a name match behind it.
	hits := search("who calls placeOrder from the checkout flow?")
	if len(hits) == 0 || hits[0].Node.Fact.Node.Name != "placeOrder" || !hits[0].ExactMatch || hits[0].NameMatch {
		t.Fatalf("symbol in a question: %+v", hits)
	}
	if h := find(hits, "checkout"); h == nil || !h.NameMatch || h.ExactMatch {
		t.Fatalf("plain word naming a method: %+v", hits)
	}
	// Question words never match: nothing in the graph is named "who" or "flow".
	for _, h := range hits {
		for _, term := range h.Matched {
			if term == "who" || term == "calls" || term == "from" || term == "the" {
				t.Fatalf("stop word reported as matched: %+v", h)
			}
		}
	}

	// A member reference names both the owner and the member.
	hits = search("explain OrderService.placeOrder() to me")
	if a, b := find(hits, "placeOrder"), find(hits, "OrderService"); a == nil || b == nil || !a.ExactMatch || !b.ExactMatch {
		t.Fatalf("member reference: %+v", hits)
	}

	// A plain word equal to a type name is a name match on the type, never
	// on a field that happens to share it, and never an exact match.
	hits = search("how is an order placed and reserved")
	if h := find(hits, "Order"); h == nil || !h.NameMatch || h.ExactMatch {
		t.Fatalf("plain word naming a class: %+v", hits)
	}
	if h := find(hits, "order"); h != nil && h.NameMatch {
		t.Fatalf("field must not be a name match: %+v", h)
	}
	for _, h := range hits {
		if h.ExactMatch {
			t.Fatalf("no symbol was named: %+v", h)
		}
	}

	// A path mention lifts the hit under it.
	hits = search("where does reserve hold stock under core/src/main/java/inventory")
	if len(hits) == 0 || hits[0].Node.Fact.Node.Name != "reserve" || !hits[0].NameMatch {
		t.Fatalf("path hint: %+v", hits)
	}

	// A single identifier still behaves as before, and a question made only
	// of stop words is still a valid lexical query.
	if hits = search("placeOrder"); len(hits) == 0 || hits[0].Node.Fact.Node.Name != "placeOrder" || !hits[0].ExactMatch {
		t.Fatalf("identifier: %+v", hits)
	}
	if _, err := store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Text: "what is the class"}); err != nil {
		t.Fatalf("stop words only: %v", err)
	}
	// Kinds restrict the name tier too: asking for fields, a word cannot name one.
	hits, err := store.HybridSearch(ctx, SearchRequest{RepositoryIDs: []string{repo}, Text: "how is an order placed", Kinds: []string{"field"}})
	if err != nil {
		t.Fatal(err)
	}
	for _, h := range hits {
		if h.NameMatch || h.Node.Fact.Node.Kind != "field" {
			t.Fatalf("kinds filter on the name tier: %+v", h)
		}
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
