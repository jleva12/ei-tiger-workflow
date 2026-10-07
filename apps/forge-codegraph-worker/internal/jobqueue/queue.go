// Package jobqueue is the worker's side of the queue of ingestion jobs the
// Forge admin API keeps in its MySQL (table code_ingestion_jobs, created by
// the admin API's Alembic migration 0014code_repositories): it claims the
// oldest eligible job of its queue, holds it with a lease it renews, and
// writes the job's run and outcome back.
//
// A claim locks one row with FOR UPDATE SKIP LOCKED, so workers never wait
// on each other, and increments its claim token; every later write is fenced
// on that token and on the job still running, so a worker that lost the job
// (its lease lapsed and another claimed it, or the repository was deleted)
// changes nothing: the write reports deployment.ErrFenceLost. While a job
// runs its eligible_at follows its lease, so the job of a worker that died
// becomes claimable when the lease lapses. Times are the database's UTC
// clock. Columns the admin API's audit trail keeps (updated_at, updated_by)
// are set on every write, since SQLAlchemy does not see these.
package jobqueue

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"strings"
	"time"

	"ei-aitiger-codegraph/pkg/deployment"
	"github.com/go-sql-driver/mysql"
)

// Table is the queue's table in the admin API's database.
const Table = "code_ingestion_jobs"

// Actor is who the admin API's audit columns say changed a row.
const Actor = "service:codegraph-worker"

// Statuses of a job, as the admin API names them.
const (
	Queued     = "QUEUED"
	Running    = "RUNNING"
	Succeeded  = "SUCCEEDED"
	Superseded = "SUPERSEDED"
	Failed     = "FAILED"
)

// ErrNoJob means no job of the queue is eligible now.
var ErrNoJob = errors.New("jobqueue: no eligible job")

// Job is a claimed job: what the run needs, and the claim that fences the
// worker's writes.
type Job struct {
	ID             string
	RepositoryID   string
	OrganizationID string
	URL            string
	Branch         string
	// CommitSHA is the commit to ingest; empty for the branch's head until
	// the worker resolves and records it.
	CommitSHA   string
	RequestedBy string
	// Run is the run the job was admitted as; zero until it is.
	Run deployment.RunKey
	// Token fences this claim's writes.
	Token uint64
	// Attempts counts the claims charged to the job, this one included.
	Attempts uint64
	// SettleOnly marks a job whose worker died after its last allowed
	// attempt: the claimer only settles it, from its run's state, and runs
	// nothing.
	SettleOnly bool
}

// Queue claims and settles one queue's jobs.
type Queue struct {
	db    *sql.DB
	queue string
}

// Open connects to the admin API's MySQL. dsn is a go-sql-driver DSN
// (user:password@tcp(host:port)/database); the settings the queue relies on
// are set here whatever it says: times parsed as UTC, the session on UTC,
// updates reporting the rows they matched (a fenced write that matched no
// row lost its claim), and lock waits bounded well under a lease.
func Open(dsn string) (*sql.DB, error) {
	c, err := mysql.ParseDSN(dsn)
	if err != nil {
		return nil, fmt.Errorf("jobqueue: CODEGRAPH_JOBS_MYSQL_DSN: %w", err)
	}
	c.ParseTime = true
	c.Loc = time.UTC
	c.ClientFoundRows = true
	if c.Params == nil {
		c.Params = map[string]string{}
	}
	c.Params["time_zone"] = "'+00:00'"
	c.Params["innodb_lock_wait_timeout"] = "5"
	connector, err := mysql.NewConnector(c)
	if err != nil {
		return nil, fmt.Errorf("jobqueue: %w", err)
	}
	db := sql.OpenDB(connector)
	db.SetMaxOpenConns(16)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(5 * time.Minute)
	return db, nil
}

// New serves queue from db.
func New(db *sql.DB, queue string) *Queue {
	return &Queue{db: db, queue: queue}
}

// Ping checks the database answers.
func (q *Queue) Ping(ctx context.Context) error {
	return classify(q.db.PingContext(ctx))
}

// Check confirms the queue's table exists: the admin API's migrations
// create it (make admin-migrate).
func (q *Queue) Check(ctx context.Context) error {
	var name string
	err := q.db.QueryRowContext(ctx, "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = ?", Table).Scan(&name)
	if errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("jobqueue: the admin API's database has no %s table; apply its migrations (make admin-migrate)", Table)
	}
	return classify(err)
}

// Claim takes the oldest job of the queue that is eligible now and whose
// repository no other worker is ingesting, for owner, until lease elapses.
// A queued job, or a running one whose lease lapsed, is charged an
// attempt; one that has had maxAttempts already is handed over to be
// settled only (Job.SettleOnly), uncharged. ErrNoJob when there is none.
func (q *Queue) Claim(ctx context.Context, owner string, lease time.Duration, maxAttempts uint64) (Job, error) {
	var job Job
	err := retryInline(ctx, func() error {
		var err error
		job, err = q.claim(ctx, owner, lease, maxAttempts)
		return err
	})
	return job, err
}

func (q *Queue) claim(ctx context.Context, owner string, lease time.Duration, maxAttempts uint64) (Job, error) {
	tx, err := q.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return Job{}, classify(err)
	}
	defer tx.Rollback()
	// Repositories another worker holds unexpired are skipped, not waited
	// on. They're read first, without locks: in the locking read below, a
	// subquery over the same table locks rows it only looks at, and other
	// claimers would skip jobs no one claimed.
	busy, err := runningURLs(ctx, tx)
	if err != nil {
		return Job{}, err
	}
	skip, args := "", []any{q.queue}
	if len(busy) > 0 {
		skip = " AND url NOT IN (?" + strings.Repeat(", ?", len(busy)-1) + ")"
		for _, url := range busy {
			args = append(args, url)
		}
	}
	// The claim index (queue, eligible_at), with id appended by InnoDB, reads
	// the queue's eligible rows in order and locks only the one returned;
	// ended jobs have no eligible_at.
	row := tx.QueryRowContext(ctx, `SELECT id, repository_id, organization_id, url, branch,
		COALESCE(commit_sha, ''), requested_by, status, attempts, claim_token,
		COALESCE(codegraph_repository_id, ''), COALESCE(run_id, '')
	FROM `+Table+`
	WHERE queue = ? AND eligible_at <= UTC_TIMESTAMP(6)`+skip+`
	ORDER BY eligible_at, id
	LIMIT 1
	FOR UPDATE SKIP LOCKED`, args...)
	var job Job
	var status string
	err = row.Scan(&job.ID, &job.RepositoryID, &job.OrganizationID, &job.URL, &job.Branch, &job.CommitSHA, &job.RequestedBy,
		&status, &job.Attempts, &job.Token, &job.Run.RepositoryID, &job.Run.RunID)
	if errors.Is(err, sql.ErrNoRows) {
		return Job{}, ErrNoJob
	}
	if err != nil {
		return Job{}, classify(err)
	}
	if status != Queued && status != Running {
		// An ended job keeps no eligible_at; one that does was written by
		// something else. Leave it.
		return Job{}, ErrNoJob
	}
	// A job whose attempts are spent is only settled: its worker died after
	// the last one (it's still RUNNING), or stopped right after claiming it
	// to settle and gave it back.
	charge := uint64(1)
	if maxAttempts > 0 && job.Attempts >= maxAttempts {
		job.SettleOnly, charge = true, 0
	}
	_, err = tx.ExecContext(ctx, `UPDATE `+Table+` SET status = 'RUNNING', claim_token = claim_token + 1,
		attempts = attempts + ?, lease_owner = ?,
		lease_until = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL ? MICROSECOND),
		eligible_at = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL ? MICROSECOND),
		started_at = COALESCE(started_at, UTC_TIMESTAMP(6)),
		updated_at = UTC_TIMESTAMP(6), updated_by = ?
	WHERE id = ?`, charge, owner, lease.Microseconds(), lease.Microseconds(), Actor, job.ID)
	if err != nil {
		return Job{}, classify(err)
	}
	if err = tx.Commit(); err != nil {
		return Job{}, classify(err)
	}
	job.Token++
	job.Attempts += charge
	return job, nil
}

// runningURLs are the repositories a worker is ingesting under an unexpired
// lease, in any queue.
func runningURLs(ctx context.Context, tx *sql.Tx) ([]string, error) {
	rows, err := tx.QueryContext(ctx, "SELECT DISTINCT url FROM "+Table+" WHERE status = 'RUNNING' AND lease_until > UTC_TIMESTAMP(6)")
	if err != nil {
		return nil, classify(err)
	}
	defer rows.Close()
	var urls []string
	for rows.Next() {
		var url string
		if err = rows.Scan(&url); err != nil {
			return nil, classify(err)
		}
		urls = append(urls, url)
	}
	return urls, classify(rows.Err())
}

// Renew extends the job's lease to lease from now.
func (q *Queue) Renew(ctx context.Context, job Job, lease time.Duration) error {
	return q.fenced(ctx, job, `lease_until = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL ? MICROSECOND),
		eligible_at = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL ? MICROSECOND)`, "", lease.Microseconds(), lease.Microseconds())
}

// SetCommit records the commit the job ingests, resolved from its branch,
// before the worker admits a run for it, so a retry admits the same commit.
// A job that already names another commit loses the fence.
func (q *Queue) SetCommit(ctx context.Context, job Job, sha string) error {
	return q.fenced(ctx, job, "commit_sha = ?", " AND (commit_sha IS NULL OR commit_sha = ?)", sha, sha)
}

// AttachRun records the run the job was admitted as.
func (q *Queue) AttachRun(ctx context.Context, job Job, run deployment.RunKey) error {
	return q.fenced(ctx, job, "codegraph_repository_id = ?, run_id = ?", "", run.RepositoryID, run.RunID)
}

// ClearRun forgets the job's run, which the graph no longer has, so the job
// is admitted again.
func (q *Queue) ClearRun(ctx context.Context, job Job) error {
	return q.fenced(ctx, job, "codegraph_repository_id = NULL, run_id = NULL", "")
}

// Outcome is how a job ended well: the run that settled it (which may be
// another job's, when a newer run published the same commit), the
// generation it published and its counts.
type Outcome struct {
	Status     string // Succeeded or Superseded
	Run        deployment.RunKey
	Generation uint64
	Metrics    any
}

// Succeed ends the job with its run's outcome.
func (q *Queue) Succeed(ctx context.Context, job Job, out Outcome) error {
	if out.Status != Succeeded && out.Status != Superseded {
		return fmt.Errorf("jobqueue: %q is not a successful end", out.Status)
	}
	metrics, err := json.Marshal(out.Metrics)
	if err != nil {
		return err
	}
	var generation sql.NullInt64
	if out.Generation > 0 {
		generation = sql.NullInt64{Int64: int64(out.Generation), Valid: true}
	}
	return q.fenced(ctx, job, `status = ?, eligible_at = NULL, lease_owner = '', lease_until = NULL,
		codegraph_repository_id = ?, run_id = ?, generation = ?, metrics = ?,
		error_code = '', error_message = '', finished_at = UTC_TIMESTAMP(6)`, "",
		out.Status, out.Run.RepositoryID, out.Run.RunID, generation, string(metrics))
}

// Requeue returns the job to its queue, eligible after delay, with why. An
// attempt that should not count against the job (the worker is stopping,
// or the repository is busy) is given back when charge is false.
func (q *Queue) Requeue(ctx context.Context, job Job, delay time.Duration, charge bool, code, message string) error {
	// A claim made only to settle charged nothing, so there's nothing to give back.
	refund := 0
	if !charge && job.Attempts > 0 && !job.SettleOnly {
		refund = 1
	}
	return q.fenced(ctx, job, `status = 'QUEUED', eligible_at = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL ? MICROSECOND),
		lease_owner = '', lease_until = NULL, attempts = attempts - ?, error_code = ?, error_message = ?`, "",
		delay.Microseconds(), refund, clip(code, 128), clip(message, 2048))
}

// Fail ends the job as failed, with why.
func (q *Queue) Fail(ctx context.Context, job Job, code, message string) error {
	return q.fenced(ctx, job, `status = 'FAILED', eligible_at = NULL, lease_owner = '', lease_until = NULL,
		error_code = ?, error_message = ?, finished_at = UTC_TIMESTAMP(6)`, "", clip(code, 128), clip(message, 2048))
}

// fenced runs one UPDATE of the job's row that applies only while this
// claim holds it. set is the SET list; extra adds to the WHERE clause; args
// are set's arguments, then extra's.
func (q *Queue) fenced(ctx context.Context, job Job, set, extra string, args ...any) error {
	stmt := `UPDATE ` + Table + ` SET ` + set + `, updated_at = UTC_TIMESTAMP(6), updated_by = ?
		WHERE id = ? AND claim_token = ? AND status = 'RUNNING'` + extra
	setArgs, extraArgs := args, []any(nil)
	if n := strings.Count(extra, "?"); n > 0 {
		setArgs, extraArgs = args[:len(args)-n], args[len(args)-n:]
	}
	all := append(append(append([]any{}, setArgs...), Actor, job.ID, job.Token), extraArgs...)
	return retryInline(ctx, func() error {
		res, err := q.db.ExecContext(ctx, stmt, all...)
		if err != nil {
			return classify(err)
		}
		n, err := res.RowsAffected()
		if err != nil {
			return classify(err)
		}
		if n == 0 {
			return fmt.Errorf("%w: job %s is no longer this worker's (claim %d)", deployment.ErrFenceLost, job.ID, job.Token)
		}
		return nil
	})
}

// retryInline retries an operation a few times, briefly, when MySQL
// answered with something that passes (a deadlock, a lock wait, a dropped
// connection), so a claim or a settlement doesn't wait out a lease for it.
func retryInline(ctx context.Context, op func() error) error {
	var err error
	for attempt := range 4 {
		if err = op(); err == nil || !isRetryable(err) {
			return err
		}
		wait := time.Duration(50*(1<<attempt))*time.Millisecond + time.Duration(rand.IntN(50))*time.Millisecond
		select {
		case <-ctx.Done():
			return errors.Join(err, ctx.Err())
		case <-time.After(wait):
		}
	}
	return err
}

// transientError marks a MySQL failure that passes; the worker's retry
// policy (internal/retry) honours Retryable.
type transientError struct{ err error }

func (e *transientError) Error() string   { return e.err.Error() }
func (e *transientError) Unwrap() error   { return e.err }
func (e *transientError) Retryable() bool { return true }

func isRetryable(err error) bool {
	var t *transientError
	return errors.As(err, &t)
}

// MySQL errors that pass: deadlock, lock wait timeout, too many
// connections, server shutting down, read-only during a failover.
var transientCodes = map[uint16]bool{1213: true, 1205: true, 1040: true, 1053: true, 1290: true, 1836: true}

func classify(err error) error {
	if err == nil {
		return nil
	}
	var m *mysql.MySQLError
	if errors.As(err, &m) && transientCodes[m.Number] {
		return &transientError{err}
	}
	if errors.Is(err, mysql.ErrInvalidConn) || errors.Is(err, sql.ErrConnDone) || errors.Is(err, driver.ErrBadConn) {
		return &transientError{err}
	}
	return err
}

func clip(s string, max int) string {
	if len(s) <= max {
		return s
	}
	cut := max
	for cut > 0 && cut < len(s) && s[cut]&0xC0 == 0x80 {
		cut--
	}
	return s[:cut]
}
