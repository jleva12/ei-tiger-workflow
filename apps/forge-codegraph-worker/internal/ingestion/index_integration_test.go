//go:build integration

package ingestion

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
	"ei-aitiger-codegraph/worker/internal/retry"
)

type resumeProvider struct {
	fail   error
	inputs int
	check  func()
}

func (*resumeProvider) Model() string      { return "resume-test" }
func (*resumeProvider) Dimensions() int    { return 2 }
func (*resumeProvider) MaxInputBytes() int { return 7500 }
func (p *resumeProvider) Embed(ctx context.Context, inputs []string) ([][]float64, error) {
	if p.check != nil {
		p.check()
	}
	if p.fail != nil {
		return nil, p.fail
	}
	p.inputs += len(inputs)
	out := make([][]float64, len(inputs))
	for i := range out {
		out[i] = []float64{1, 0}
	}
	return out, nil
}

func TestRequiredEmbeddingFailureResumesPublishedGeneration(t *testing.T) {
	if os.Getenv("SPANNER_EMULATOR_HOST") == "" {
		t.Skip("requires Spanner emulator")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	db := fmt.Sprintf("projects/codegraph-test/instances/index-resume/databases/r-%d", time.Now().UnixNano())
	if err := spannerstore.ProvisionEmulator(ctx, db, 2); err != nil {
		t.Fatal(err)
	}
	store, err := spannerstore.New(ctx, spannerstore.Config{Database: db, Scope: "test", CursorSigningKey: []byte(strings.Repeat("k", 32)), VectorLength: 2})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	const repo = "resume"
	_, err = store.PutRepository(ctx, deployment.Repository{SchemaVersion: deployment.SchemaVersion, RepositoryID: repo, GitHubURL: "https://github.com/acme/resume", IntegrationID: "test", Branch: "main"})
	if err != nil {
		t.Fatal(err)
	}
	digest := "sha256:" + strings.Repeat("ab", 32)
	admitted, err := store.Admit(ctx, deployment.Admission{Request: deployment.Request{SchemaVersion: deployment.SchemaVersion, RepositoryID: repo, Branch: "main", DeploymentID: "test", DeploymentSequence: 1, TargetCommitSHA: strings.Repeat("a", 40), DeployedAt: time.Now().UTC(), AnalysisConfigDigest: digest, TriggerKind: deployment.TriggerDeployment}})
	if err != nil {
		t.Fatal(err)
	}
	run := admitted.Run
	run.Phase, run.Generation, run.Attempts = deployment.Running, 1, 1
	run, err = store.UpdateRun(ctx, run)
	if err != nil {
		t.Fatal(err)
	}
	lease, err := store.AcquireLease(ctx, run.Key, "first", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	_, busyErr := store.AcquireLease(ctx, run.Key, "busy-worker", time.Minute)
	if !retry.Transient(busyErr) {
		t.Fatalf("repository busy must remain transient: %v", busyErr)
	}
	guard := startLease(ctx, store, lease, time.Minute, 10*time.Second)
	loader := store.NewLoader(lease, 1, run.Request.TargetCommitSHA, spannerstore.LoaderOptions{})
	for i := 0; i < 3; i++ {
		name := fmt.Sprintf("method%d", i)
		fact := graph.Fact{Node: &graph.Node{ID: graph.ID("method", name), Kind: "method", Name: name, Properties: map[string]graph.PropertyValue{"source_text": graph.StringValue("def " + name + "(): pass")}}}
		if err = loader.Apply(ctx, []graph.Change{{Op: graph.OpAdd, Key: fact.Key(), After: &fact}}); err != nil {
			t.Fatal(err)
		}
	}
	if err = loader.Flush(ctx); err != nil {
		t.Fatal(err)
	}
	provider := &resumeProvider{fail: &codesearch.ProviderError{Status: 503}}
	provider.check = func() {
		current, e := store.GetRun(ctx, run.Key)
		if e != nil || current.Phase != deployment.Running || current.FinishedAt != nil {
			t.Errorf("premature success: %+v %v", current, e)
		}
	}
	p := &Pipeline{digest: digest, config: Config{Store: store, Embedder: provider, EmbedConcurrency: 1, OwnerID: "second", LeaseTTL: time.Minute, RenewInterval: 10 * time.Second, Logger: slog.New(slog.DiscardHandler)}}
	clock := newClock()
	metrics := deployment.Metrics{Nodes: 3, Durations: clock.durations}
	failed, err := p.publish(ctx, guard, run, &metrics, deployment.GenerationInputs{SchemaVersion: deployment.SchemaVersion, ContextID: "fixture", AnalysisConfigDigest: digest}, clock, p.config.Logger)
	guard.Stop()
	if e := store.ReleaseLease(ctx, lease); e != nil {
		t.Fatal(e)
	}
	var providerErr *codesearch.ProviderError
	if !errors.As(err, &providerErr) || failed.Phase != deployment.Failed || failed.Index.Status != deployment.IndexIncomplete || failed.FinishedAt == nil {
		t.Fatalf("embedding failure accepted: %+v %v", failed, err)
	}
	stored, err := store.GetRun(ctx, run.Key)
	if err != nil || stored.Phase != deployment.Failed || stored.FailureRetryable == nil || !*stored.FailureRetryable {
		t.Fatalf("failure not durable: %+v %v", stored, err)
	}
	state, err := store.State(ctx, repo)
	if err != nil || state.LiveGeneration != 1 || state.LiveRunID != run.Key.RunID {
		t.Fatalf("publication: %+v %v", state, err)
	}

	// Simulate a durable vector written immediately before an interruption.
	page, err := store.SearchDocuments(ctx, repo, provider.Model(), 2, "")
	if err != nil || len(page.Documents) != 3 {
		t.Fatalf("documents: %+v %v", page, err)
	}
	if err = store.PutEmbeddings(ctx, repo, provider.Model(), 1, []codesearch.Embedding{{Document: page.Documents[0], Vector: []float64{1, 0}}}); err != nil {
		t.Fatal(err)
	}
	// The resumed pass is sized by what is still missing.
	if pending, e := store.PendingDocuments(ctx, repo, provider.Model(), 2); e != nil || pending != 2 {
		t.Fatalf("pending documents: %d %v", pending, e)
	}
	provider.fail = nil
	completed, err := p.Run(ctx, run.Key)
	if err != nil || !completed.Terminal() || completed.Phase != deployment.Succeeded || completed.Index.Status != deployment.IndexComplete {
		t.Fatalf("resume failed: %+v %v", completed, err)
	}
	if completed.Generation != 1 || completed.Attempts != 2 || provider.inputs != 2 || completed.Metrics.Nodes != 3 {
		t.Fatalf("resume rebuilt graph or repeated vectors: %+v inputs %d", completed, provider.inputs)
	}
	page, err = store.SearchDocuments(ctx, repo, provider.Model(), 2, "")
	if err != nil || len(page.Documents) != 0 {
		t.Fatalf("missing embeddings: %+v %v", page, err)
	}
	if pending, e := store.PendingDocuments(ctx, repo, provider.Model(), 2); e != nil || pending != 0 {
		t.Fatalf("pending documents after the pass: %d %v", pending, e)
	}
	if _, err = p.Run(ctx, run.Key); err != nil {
		t.Fatal(err)
	}
	if provider.inputs != 2 {
		t.Fatal("completed run re-embedded")
	}
}
