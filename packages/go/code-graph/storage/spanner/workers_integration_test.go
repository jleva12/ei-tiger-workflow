//go:build integration

package spannerstore

import (
	"context"
	"errors"
	"reflect"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/deployment"
)

func TestWorkerRegistry(t *testing.T) {
	ctx := context.Background()
	queue := "registry-test"
	digest := configFor(t)
	started := time.Now().UTC().Truncate(time.Microsecond)
	live := WorkerRegistration{Queue: queue, OwnerID: "host:100", ConfigDigest: digest, Languages: []string{"typescript", "java"}, BuildMode: "maven-resolved", MaxAttempts: 6, Embeddings: true, StartedAt: started}
	if err := store.RegisterWorker(ctx, live); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []WorkerRegistration{
		{Queue: "", OwnerID: "host:1", ConfigDigest: digest, Languages: []string{"java"}, BuildMode: "auto", MaxAttempts: 1, StartedAt: started},
		{Queue: queue, OwnerID: "", ConfigDigest: digest, Languages: []string{"java"}, BuildMode: "auto", MaxAttempts: 1, StartedAt: started},
		{Queue: queue, OwnerID: "host:1", ConfigDigest: "plain", Languages: []string{"java"}, BuildMode: "auto", MaxAttempts: 1, StartedAt: started},
		{Queue: queue, OwnerID: "host:1", ConfigDigest: digest, Languages: []string{"java"}, BuildMode: "auto", MaxAttempts: 0, StartedAt: started},
		{Queue: queue, OwnerID: "host:1", ConfigDigest: digest, Languages: []string{"java"}, BuildMode: "auto", MaxAttempts: 1},
		{Queue: queue, OwnerID: "host:1", ConfigDigest: digest, Languages: nil, BuildMode: "auto", MaxAttempts: 1, StartedAt: started},
		{Queue: queue, OwnerID: "host:1", ConfigDigest: digest, Languages: []string{"java", "java"}, BuildMode: "auto", MaxAttempts: 1, StartedAt: started},
		{Queue: queue, OwnerID: "host:1", ConfigDigest: digest, Languages: []string{""}, BuildMode: "auto", MaxAttempts: 1, StartedAt: started},
	} {
		if err := store.RegisterWorker(ctx, bad); !errors.Is(err, deployment.ErrInvalidRequest) {
			t.Fatalf("invalid registration accepted: %+v %v", bad, err)
		}
	}
	// A worker that stopped an hour ago without cleaning up is not active.
	if _, err := store.client.Apply(ctx, []*spanner.Mutation{spanner.InsertOrUpdate("CGWorkers", workerColumns,
		[]any{queue, "host:200", digest, []string{"java"}, "auto", int64(3), false, started, time.Now().Add(-time.Hour)})}); err != nil {
		t.Fatal(err)
	}
	active, err := store.ActiveWorkers(ctx, queue, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if len(active) != 1 {
		t.Fatalf("active workers: %+v", active)
	}
	got := active[0]
	if got.HeartbeatAt.IsZero() || got.HeartbeatAt.Before(started) {
		t.Fatalf("heartbeat not stamped: %+v", got)
	}
	first := got.HeartbeatAt
	got.HeartbeatAt = time.Time{}
	// Languages are stored unique and sorted.
	want := live
	want.Languages = []string{"java", "typescript"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("registration round trip: got %+v want %+v", got, want)
	}
	// Re-registering refreshes the heartbeat and keeps one row per owner.
	if err = store.RegisterWorker(ctx, live); err != nil {
		t.Fatal(err)
	}
	if active, err = store.ActiveWorkers(ctx, queue, time.Minute); err != nil || len(active) != 1 || !active[0].HeartbeatAt.After(first) {
		t.Fatalf("heartbeat refresh: %+v %v", active, err)
	}
	// A wide window shows the stale worker too.
	if active, err = store.ActiveWorkers(ctx, queue, 2*time.Hour); err != nil || len(active) != 2 || active[0].OwnerID != "host:100" || active[1].OwnerID != "host:200" {
		t.Fatalf("stale worker within a wide window: %+v %v", active, err)
	}
	if _, err = store.ActiveWorkers(ctx, queue, 0); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("zero window accepted: %v", err)
	}
	// Other queues are unaffected.
	if active, err = store.ActiveWorkers(ctx, "another-queue", time.Hour); err != nil || len(active) != 0 {
		t.Fatalf("queue isolation: %+v %v", active, err)
	}
	// Unregistering removes the row and is idempotent.
	for i := 0; i < 2; i++ {
		if err = store.UnregisterWorker(ctx, queue, "host:100"); err != nil {
			t.Fatal(err)
		}
	}
	if err = store.UnregisterWorker(ctx, queue, "host:200"); err != nil {
		t.Fatal(err)
	}
	if active, err = store.ActiveWorkers(ctx, queue, 2*time.Hour); err != nil || len(active) != 0 {
		t.Fatalf("after unregister: %+v %v", active, err)
	}
	if err = store.UnregisterWorker(ctx, "", "host:100"); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("invalid unregister accepted: %v", err)
	}
}
