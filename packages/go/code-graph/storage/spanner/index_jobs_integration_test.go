//go:build integration

package spannerstore

import (
	"context"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/deployment"
)

// indexedRun is a run published with its index still running, under lease.
func indexedRun(t *testing.T) (deployment.Run, deployment.Lease, string) {
	t.Helper()
	ctx := context.Background()
	repo := "idx-" + t.Name()
	register(t, repo)
	r := request(repo, 2, testCommit1)
	r.AnalysisConfigDigest = configFor(t)
	run := admitWith(t, r)
	run.Phase, run.Generation = deployment.Running, 1
	var err error
	run, err = store.UpdateRun(ctx, run)
	if err != nil {
		t.Fatal(err)
	}
	lease, err := store.AcquireLease(ctx, run.Key, testOwner, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	run.Index = &deployment.IndexState{Status: deployment.IndexRunning, StartedAt: time.Now().UTC()}
	run, err = store.Publish(ctx, lease, run)
	if err != nil {
		t.Fatal(err)
	}
	return run, lease, r.AnalysisConfigDigest
}

func TestPublishedIndexControlsSuccessAndOrdering(t *testing.T) {
	ctx := context.Background()
	run, lease, digest := indexedRun(t)
	if run.Phase != deployment.Running || run.FinishedAt != nil || run.Terminal() {
		t.Fatalf("premature success: %+v", run)
	}
	copy := run
	copy.Phase = deployment.Succeeded
	_, err := store.UpdateIndex(ctx, lease, copy)
	wantErr(t, err, deployment.ErrInvalidTransition, "success without embeddings")
	now := time.Now().UTC()
	run.Phase = deployment.Failed
	run.Index = &deployment.IndexState{Status: deployment.IndexIncomplete, StartedAt: run.Index.StartedAt, FinishedAt: &now, Error: "provider down"}
	run, err = store.UpdateIndex(ctx, lease, run)
	if err != nil {
		t.Fatal(err)
	}
	stale := request(run.Key.RepositoryID, 1, testCommit2)
	stale.AnalysisConfigDigest = digest
	_, err = store.Admit(ctx, deployment.Admission{Request: stale})
	wantErr(t, err, deployment.ErrStaleDeployment, "failed index does not erase publication ordering")
	// The worker resumes the failed index on its job's next attempt.
	run.Phase = deployment.Running
	run.Index = &deployment.IndexState{Status: deployment.IndexRunning, StartedAt: now}
	run, err = store.UpdateIndex(ctx, lease, run)
	if err != nil {
		t.Fatal(err)
	}
	run.Phase = deployment.Succeeded
	run.Index.Status = deployment.IndexComplete
	run.Index.FinishedAt = &now
	run, err = store.UpdateIndex(ctx, lease, run)
	if err != nil {
		t.Fatal(err)
	}
	if !run.Terminal() {
		t.Fatalf("a complete index ends the run: %+v", run)
	}
	if err = store.ReleaseLease(ctx, lease); err != nil {
		t.Fatal(err)
	}
	newLease, err := store.AcquireLease(ctx, run.Key, "successor", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer store.ReleaseLease(ctx, newLease)
	_, err = store.UpdateIndex(ctx, lease, run)
	wantErr(t, err, deployment.ErrFenceLost, "expired index writer")
}

func TestFailRunLeavesAHeldOrEndedRunAlone(t *testing.T) {
	ctx := context.Background()
	run, lease, _ := indexedRun(t)
	// Another worker holds the run: its job is not given up on.
	if err := store.FailRun(ctx, run.Key, "retry_exhausted", false); err != nil {
		t.Fatal(err)
	}
	held, err := store.GetRun(ctx, run.Key)
	if err != nil || held.Phase != deployment.Running || held.Revision != run.Revision {
		t.Fatalf("held run changed: %+v %v", held, err)
	}
	if err = store.ReleaseLease(ctx, lease); err != nil {
		t.Fatal(err)
	}
	if err = store.FailRun(ctx, run.Key, "retry_exhausted", false); err != nil {
		t.Fatal(err)
	}
	failed, err := store.GetRun(ctx, run.Key)
	if err != nil || failed.Phase != deployment.Failed || failed.ErrorCode != "retry_exhausted" ||
		failed.Index.Status != deployment.IndexIncomplete || failed.FailureRetryable == nil || *failed.FailureRetryable {
		t.Fatalf("given-up run: %+v %v", failed, err)
	}
	// A failure it recorded itself is kept, code and all.
	if err = store.FailRun(ctx, run.Key, "permanent_failure", false); err != nil {
		t.Fatal(err)
	}
	again, err := store.GetRun(ctx, run.Key)
	if err != nil || again.ErrorCode != "retry_exhausted" || again.Revision != failed.Revision {
		t.Fatalf("failed run rewritten: %+v %v", again, err)
	}
}

func TestSubmissionRunFindsTheAdmittedRun(t *testing.T) {
	ctx := context.Background()
	repo := "submission-" + t.Name()
	register(t, repo)
	r := request(repo, 1, testCommit1)
	r.AnalysisConfigDigest = configFor(t)
	_, err := store.SubmissionRun(ctx, "forge-job-"+repo)
	wantErr(t, err, deployment.ErrNotFound, "nothing admitted yet")
	admitted, err := store.Admit(ctx, deployment.Admission{SubmissionID: "forge-job-" + repo, Request: r})
	if err != nil {
		t.Fatal(err)
	}
	key, err := store.SubmissionRun(ctx, "forge-job-"+repo)
	if err != nil || key != admitted.Run.Key {
		t.Fatalf("submission run %+v %v, want %+v", key, err, admitted.Run.Key)
	}
}
