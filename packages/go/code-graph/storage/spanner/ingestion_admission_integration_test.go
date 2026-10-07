//go:build integration

package spannerstore

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sync"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/deployment"
)

func minimalAdmission(repo, submission string) deployment.Admission {
	q := request(repo, 1, testCommit1)
	q.DeploymentID = ""
	q.DeploymentSequence = 0
	q.DeployedAt = time.Time{}
	return deployment.Admission{SubmissionID: submission, Request: q}
}

func TestAutomaticAdmissionConcurrencyAndReplay(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	repo := "automatic-" + t.Name()
	register(t, repo)
	// Sequences advance past accepted runs, not just the published generation.
	admit(t, repo, 40, testCommit1)
	const count = 4
	results := make([]deployment.AdmissionResult, count)
	errs := make([]error, count)
	var wg sync.WaitGroup
	for i := range count {
		wg.Add(1)
		go func() {
			defer wg.Done()
			results[i], errs[i] = store.AdmitIngestion(ctx, minimalAdmission(repo, fmt.Sprintf("automatic-%d", i)))
		}()
	}
	wg.Wait()
	seen := map[uint64]bool{}
	for i, result := range results {
		if errs[i] != nil || result.Reused {
			t.Fatalf("concurrent admission %d: %+v %v", i, result, errs[i])
		}
		if err := result.Run.Validate(); err != nil {
			t.Fatalf("generated run invalid: %v", err)
		}
		seq := result.Run.Request.DeploymentSequence
		if seen[seq] || seq < 41 || seq > 40+count {
			t.Fatalf("sequences must be unique and consecutive: %d, seen %v", seq, seen)
		}
		seen[seq] = true
		if result.Run.Phase != deployment.Accepted {
			t.Fatalf("admitted run: %+v", result.Run)
		}
	}
	// Concurrent retries of one submission all reuse the same original run.
	for i := range count {
		wg.Add(1)
		go func() {
			defer wg.Done()
			results[i], errs[i] = store.AdmitIngestion(ctx, minimalAdmission(repo, "same-attempt"))
		}()
	}
	wg.Wait()
	created := 0
	for i, result := range results {
		if errs[i] != nil || result.Run.Key != results[0].Run.Key || !result.Run.Request.SameDeployment(results[0].Run.Request) || result.Run.Request.DeploymentSequence != 45 {
			t.Fatalf("concurrent replay %d: %+v %v", i, result, errs[i])
		}
		if !result.Reused {
			created++
		}
	}
	if created != 1 {
		t.Fatalf("created %d runs for the same attempt", created)
	}
	for _, field := range []string{"commit", "config", "deployment", "sequence", "timestamp", "repository"} {
		a := minimalAdmission(repo, "same-attempt")
		switch field {
		case "commit":
			a.Request.TargetCommitSHA = testCommit2
		case "config":
			a.Request.AnalysisConfigDigest = configFor(t)
		case "deployment":
			a.Request.DeploymentID = "different"
		case "sequence":
			a.Request.DeploymentSequence = 99
		case "timestamp":
			a.Request.DeployedAt = time.Unix(1, 0)
		case "repository":
			register(t, "another-automatic-repo")
			a.Request.RepositoryID = "another-automatic-repo"
		}
		if _, err := store.AdmitIngestion(ctx, a); !errors.Is(err, deployment.ErrConflictingReplay) {
			t.Fatalf("changed %s: %v", field, err)
		}
	}
}

func TestAutomaticAdmissionPublishedSequenceAndLimit(t *testing.T) {
	ctx := context.Background()
	repo := "automatic-published"
	register(t, repo)
	run, lease := startGeneration(t, repo, 2, 1)
	load(t, lease, 1, add(testNode("class", "Automatic", nil), ""))
	publish(t, lease, run)
	result, err := store.AdmitIngestion(ctx, minimalAdmission(repo, ""))
	if err != nil || !result.Reused || result.Run.Key != run.Key {
		t.Fatalf("current commit is already ingested: %+v %v", result, err)
	}
	next := minimalAdmission(repo, "")
	next.Request.TargetCommitSHA = testCommit2
	result, err = store.AdmitIngestion(ctx, next)
	if err != nil || result.Run.Request.DeploymentSequence != 3 {
		t.Fatalf("advance past publication: %+v %v", result, err)
	}
	// Supplied metadata still uses the existing stale deployment protection.
	a := minimalAdmission(repo, "")
	a.Request.DeploymentSequence = 1
	if _, err := store.AdmitIngestion(ctx, a); !errors.Is(err, deployment.ErrStaleDeployment) {
		t.Fatalf("explicit stale sequence: %v", err)
	}
	q := request(repo, 1, testCommit1)
	q.DeploymentSequence = math.MaxInt64
	if _, err := store.Admit(ctx, deployment.Admission{Request: q}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.AdmitIngestion(ctx, next); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("sequence overflow: %v", err)
	}
}
