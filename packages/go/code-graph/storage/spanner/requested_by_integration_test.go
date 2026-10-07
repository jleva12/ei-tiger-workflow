//go:build integration

package spannerstore

import (
	"context"
	"testing"
	"time"
)

// The worker reads the submitting user back from the run to fetch as them.
func TestAdmissionKeepsRequestedBy(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	repo := "requested-by-" + t.Name()
	register(t, repo)
	a := minimalAdmission(repo, "requested-by-1")
	a.Request.RequestedBy = "forge-user-1"
	result, err := store.AdmitIngestion(ctx, a)
	if err != nil || result.Reused {
		t.Fatalf("admit: %+v %v", result, err)
	}
	run, err := store.GetRun(ctx, result.Run.Key)
	if err != nil {
		t.Fatal(err)
	}
	if run.Request.RequestedBy != "forge-user-1" {
		t.Fatalf("stored requester %q", run.Request.RequestedBy)
	}
	// A retry of the same submission by the same user returns the same run.
	again, err := store.AdmitIngestion(ctx, a)
	if err != nil || !again.Reused || again.Run.Key != result.Run.Key || again.Run.Request.RequestedBy != "forge-user-1" {
		t.Fatalf("replay: %+v %v", again, err)
	}
}
