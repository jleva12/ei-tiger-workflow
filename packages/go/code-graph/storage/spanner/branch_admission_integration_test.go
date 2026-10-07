//go:build integration

package spannerstore

import (
	"context"
	"errors"
	"sync"
	"testing"

	"ei-aitiger-codegraph/pkg/deployment"
)

func TestBranchAdmissionLocksAtomicallyAndChecksValidatedCommit(t *testing.T) {
	ctx := context.Background()
	repo := "branch-race"
	register(t, repo)
	results := make([]deployment.AdmissionResult, 2)
	errs := make([]error, 2)
	var wg sync.WaitGroup
	for i, branch := range []string{"main", "release"} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			a := minimalAdmission(repo, "branch-"+branch)
			a.Request.Branch = branch
			empty := ""
			a.CheckedLiveCommit, a.CheckedIngestionCommit = &empty, &empty
			results[i], errs[i] = store.AdmitIngestion(ctx, a)
		}()
	}
	wg.Wait()
	winner := 0
	if errs[0] != nil {
		winner = 1
	}
	if errs[winner] != nil || !errors.Is(errs[1-winner], deployment.ErrConflictingReplay) {
		t.Fatalf("branch race: %v", errs)
	}
	pinned, err := store.GetRepository(ctx, repo)
	if err != nil || pinned.Branch != results[winner].Run.Request.Branch || pinned.LastIngestionCommit != testCommit1 {
		t.Fatalf("pin: %+v %v", pinned, err)
	}
	state, err := store.State(ctx, repo)
	if err != nil || state.Branch != pinned.Branch {
		t.Fatalf("graph label: %+v %v", state, err)
	}
	changed := pinned
	changed.Branch = "another"
	if _, err := store.PutRepository(ctx, changed); !errors.Is(err, deployment.ErrConflictingReplay) {
		t.Fatalf("branch update accepted: %v", err)
	}
	changed = pinned
	changed.LastIngestionCommit = testCommit2
	if _, err := store.PutRepository(ctx, changed); !errors.Is(err, deployment.ErrConflictingReplay) {
		t.Fatalf("frontier changed outside admission: %v", err)
	}
	// The Git check observed no accepted commit, but a concurrent admission
	// won first. Do not consume the now-stale ancestry validation.
	empty := ""
	a := minimalAdmission(repo, "stale-checked-head")
	a.Request.Branch = pinned.Branch
	a.Request.TargetCommitSHA = testCommit2
	a.CheckedIngestionCommit = &empty
	if _, err := store.AdmitIngestion(ctx, a); !errors.Is(err, deployment.ErrStaleDeployment) {
		t.Fatalf("stale ancestry observation: %v", err)
	}
}

func TestAdmissionChecksPublishedCommitAndReturnsRevisionMetadata(t *testing.T) {
	ctx := context.Background()
	repo := "branch-published"
	register(t, repo)
	run, lease := startGeneration(t, repo, 1, 1)
	load(t, lease, 1, add(testNode("class", "Branch", nil), ""))
	publish(t, lease, run)
	empty := ""
	a := minimalAdmission(repo, "stale-live-observation")
	a.Request.TargetCommitSHA = testCommit2
	a.CheckedLiveCommit = &empty
	if _, err := store.AdmitIngestion(ctx, a); !errors.Is(err, deployment.ErrStaleDeployment) {
		t.Fatalf("published during validation: %v", err)
	}
	commit, err := store.GenerationCommit(ctx, repo, 1)
	if err != nil || commit != testCommit1 {
		t.Fatalf("published revision: %q %v", commit, err)
	}
	if _, err := store.GenerationCommit(ctx, repo, 2); err == nil {
		t.Fatal("unpublished generation exposed")
	}
}
