//go:build integration

package jobqueue

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"os"
	"sync"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/deployment"
)

// Run against the admin API's MySQL, migrated (make admin-migrate):
//
//	CODEGRAPH_TEST_MYSQL_DSN='forge_admin:local-admin@tcp(127.0.0.1:13326)/forge_admin' \
//	  go test -tags=integration ./internal/jobqueue
//
// Each test works in an organization and a queue of its own and removes
// them after.

func world(t *testing.T) (*sql.DB, *Queue, string) {
	t.Helper()
	dsn := os.Getenv("CODEGRAPH_TEST_MYSQL_DSN")
	if dsn == "" {
		t.Skip("CODEGRAPH_TEST_MYSQL_DSN is not set")
	}
	db, err := Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	ctx := context.Background()
	org := "cgtest-" + random(t)
	if _, err = db.ExecContext(ctx, "INSERT INTO organizations (id, name, description) VALUES (?, ?, '')", org, "codegraph queue test "+org); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.ExecContext(context.Background(), "DELETE FROM organizations WHERE id = ?", org) })
	q := New(db, "test-"+org)
	if err = q.Check(ctx); err != nil {
		t.Fatal(err)
	}
	return db, q, org
}

func random(t *testing.T) string {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		t.Fatal(err)
	}
	return hex.EncodeToString(b)
}

// enqueue adds a repository and a queued job for it, as the admin API does.
func enqueue(t *testing.T, db *sql.DB, q *Queue, org, url string) (repository, job string) {
	t.Helper()
	ctx := context.Background()
	repository, job = "r"+random(t), "j"+random(t)
	if _, err := db.ExecContext(ctx, "INSERT INTO code_repositories (id, organization_id, url, owner, name, branch) VALUES (?, ?, ?, 'acme', 'widgets', 'main')", repository, org, url); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO code_ingestion_jobs (id, repository_id, organization_id, url, branch, requested_by, queue, status,
		eligible_at, lease_owner, claim_token, attempts, error_code, error_message)
		VALUES (?, ?, ?, ?, 'main', 'user-1', ?, 'QUEUED', UTC_TIMESTAMP(6), '', 0, 0, '', '')`, job, repository, org, url, q.queue); err != nil {
		t.Fatal(err)
	}
	return repository, job
}

func status(t *testing.T, db *sql.DB, job string) (string, uint64) {
	t.Helper()
	var s string
	var attempts uint64
	if err := db.QueryRow("SELECT status, attempts FROM code_ingestion_jobs WHERE id = ?", job).Scan(&s, &attempts); err != nil {
		t.Fatal(err)
	}
	return s, attempts
}

func TestParallelClaimersTakeDifferentJobs(t *testing.T) {
	db, q, org := world(t)
	ctx := context.Background()
	want := map[string]bool{}
	for i := range 6 {
		_, job := enqueue(t, db, q, org, "https://github.com/acme/widgets-"+string(rune('a'+i)))
		want[job] = true
	}
	var mu sync.Mutex
	got := map[string]int{}
	var wg sync.WaitGroup
	for w := range 6 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			job, err := q.Claim(ctx, "worker-"+string(rune('0'+w)), time.Minute, 3)
			if err != nil {
				t.Error(err)
				return
			}
			mu.Lock()
			got[job.ID]++
			mu.Unlock()
		}()
	}
	wg.Wait()
	if len(got) != 6 {
		t.Fatalf("claims %v", got)
	}
	for id, n := range got {
		if !want[id] || n != 1 {
			t.Fatalf("job %s claimed %d times", id, n)
		}
	}
	if _, err := q.Claim(ctx, "worker-x", time.Minute, 3); !errors.Is(err, ErrNoJob) {
		t.Fatalf("all claimed: %v", err)
	}
}

func TestALapsedLeaseIsReclaimedAndFencesTheOldClaim(t *testing.T) {
	db, q, org := world(t)
	ctx := context.Background()
	_, id := enqueue(t, db, q, org, "https://github.com/acme/lapsed")
	first, err := q.Claim(ctx, "worker-1", time.Second, 3)
	if err != nil || first.ID != id || first.Attempts != 1 || first.SettleOnly {
		t.Fatalf("first claim %+v %v", first, err)
	}
	if _, err = q.Claim(ctx, "worker-2", time.Second, 3); !errors.Is(err, ErrNoJob) {
		t.Fatalf("leased job claimed again: %v", err)
	}
	if err = q.SetCommit(ctx, first, "0123456789abcdef0123456789abcdef01234567"); err != nil {
		t.Fatal(err)
	}
	time.Sleep(1200 * time.Millisecond)
	second, err := q.Claim(ctx, "worker-2", time.Minute, 3)
	if err != nil || second.ID != id || second.Token != first.Token+1 || second.Attempts != 2 ||
		second.CommitSHA != "0123456789abcdef0123456789abcdef01234567" {
		t.Fatalf("reclaim %+v %v", second, err)
	}
	// The first worker's writes no longer apply.
	for name, write := range map[string]error{
		"renew":   q.Renew(ctx, first, time.Minute),
		"attach":  q.AttachRun(ctx, first, deployment.RunKey{RepositoryID: "repo:x", RunID: "run-x"}),
		"fail":    q.Fail(ctx, first, "permanent_failure", "stale"),
		"succeed": q.Succeed(ctx, first, Outcome{Status: Succeeded, Run: deployment.RunKey{RepositoryID: "repo:x", RunID: "run-x"}}),
	} {
		if !errors.Is(write, deployment.ErrFenceLost) {
			t.Errorf("%s with a stale claim: %v", name, write)
		}
	}
	// Another commit can't replace the recorded one.
	if err = q.SetCommit(ctx, second, "fedcba9876543210fedcba9876543210fedcba98"); !errors.Is(err, deployment.ErrFenceLost) {
		t.Fatalf("commit replaced: %v", err)
	}
	run := deployment.RunKey{RepositoryID: "repo:abc", RunID: "run-1"}
	if err = q.AttachRun(ctx, second, run); err != nil {
		t.Fatal(err)
	}
	if err = q.Succeed(ctx, second, Outcome{Status: Succeeded, Run: run, Generation: 3, Metrics: map[string]int{"nodes": 5}}); err != nil {
		t.Fatal(err)
	}
	var eligible sql.NullTime
	var generation sql.NullInt64
	var metrics, updatedBy string
	if err = db.QueryRow("SELECT eligible_at, generation, metrics, updated_by FROM code_ingestion_jobs WHERE id = ?", id).Scan(&eligible, &generation, &metrics, &updatedBy); err != nil {
		t.Fatal(err)
	}
	if eligible.Valid || generation.Int64 != 3 || metrics != `{"nodes":5}` || updatedBy != Actor {
		t.Fatalf("ended job: eligible %v generation %v metrics %s by %s", eligible, generation, metrics, updatedBy)
	}
	if s, _ := status(t, db, id); s != Succeeded {
		t.Fatalf("status %s", s)
	}
}

func TestRequeueChargesOnlyWhenAsked(t *testing.T) {
	db, q, org := world(t)
	ctx := context.Background()
	_, id := enqueue(t, db, q, org, "https://github.com/acme/requeue")
	job, err := q.Claim(ctx, "worker-1", time.Minute, 3)
	if err != nil {
		t.Fatal(err)
	}
	if err = q.Requeue(ctx, job, 0, false, "worker_stopped", ""); err != nil {
		t.Fatal(err)
	}
	if s, attempts := status(t, db, id); s != Queued || attempts != 0 {
		t.Fatalf("refunded requeue: %s %d", s, attempts)
	}
	job, err = q.Claim(ctx, "worker-1", time.Minute, 3)
	if err != nil {
		t.Fatal(err)
	}
	if err = q.Requeue(ctx, job, time.Hour, true, "transient_failure", "GitHub is down"); err != nil {
		t.Fatal(err)
	}
	if s, attempts := status(t, db, id); s != Queued || attempts != 1 {
		t.Fatalf("charged requeue: %s %d", s, attempts)
	}
	if _, err = q.Claim(ctx, "worker-1", time.Minute, 3); !errors.Is(err, ErrNoJob) {
		t.Fatalf("backed-off job claimed: %v", err)
	}
}

func TestAJobWhoseAttemptsAreSpentIsHandedOverToSettle(t *testing.T) {
	db, q, org := world(t)
	ctx := context.Background()
	_, id := enqueue(t, db, q, org, "https://github.com/acme/spent")
	if _, err := q.Claim(ctx, "worker-1", time.Second, 1); err != nil {
		t.Fatal(err)
	}
	time.Sleep(1200 * time.Millisecond)
	job, err := q.Claim(ctx, "worker-2", time.Minute, 1)
	if err != nil || job.ID != id || !job.SettleOnly || job.Attempts != 1 {
		t.Fatalf("settle-only claim %+v %v", job, err)
	}
	if err = q.Fail(ctx, job, "retry_exhausted", "the worker died"); err != nil {
		t.Fatal(err)
	}
	if s, _ := status(t, db, id); s != Failed {
		t.Fatalf("status %s", s)
	}
}

func TestABusyRepositoryIsSkipped(t *testing.T) {
	db, q, org := world(t)
	ctx := context.Background()
	url := "https://github.com/acme/shared"
	_, first := enqueue(t, db, q, org, url)
	other := "cgtest-" + random(t)
	if _, err := db.Exec("INSERT INTO organizations (id, name, description) VALUES (?, ?, '')", other, "codegraph queue test "+other); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Exec("DELETE FROM organizations WHERE id = ?", other) })
	_, second := enqueue(t, db, q, other, url)
	job, err := q.Claim(ctx, "worker-1", time.Minute, 3)
	if err != nil || job.ID != first {
		t.Fatalf("first claim %+v %v", job, err)
	}
	if _, err = q.Claim(ctx, "worker-2", time.Minute, 3); !errors.Is(err, ErrNoJob) {
		t.Fatalf("the same repository claimed twice: %v", err)
	}
	if err = q.Succeed(ctx, job, Outcome{Status: Succeeded, Run: deployment.RunKey{RepositoryID: "repo:s", RunID: "run-s"}}); err != nil {
		t.Fatal(err)
	}
	next, err := q.Claim(ctx, "worker-2", time.Minute, 3)
	if err != nil || next.ID != second {
		t.Fatalf("second claim %+v %v", next, err)
	}
}

func TestDeletingTheRepositoryFencesItsRunningJob(t *testing.T) {
	db, q, org := world(t)
	ctx := context.Background()
	repository, _ := enqueue(t, db, q, org, "https://github.com/acme/deleted")
	job, err := q.Claim(ctx, "worker-1", time.Minute, 3)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec("DELETE FROM code_repositories WHERE id = ?", repository); err != nil {
		t.Fatal(err)
	}
	if err = q.Renew(ctx, job, time.Minute); !errors.Is(err, deployment.ErrFenceLost) {
		t.Fatalf("renewal of a deleted job: %v", err)
	}
}
