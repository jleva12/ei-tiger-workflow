// Package spannerstore is the durable backend of the code graph. Spanner is
// the system of record for repositories and their live generation, runs, the
// repository lease, the versioned graph, per-file identity maps, retained
// source content and search embeddings. The queue of ingestion jobs is not
// here: the Forge admin API keeps it in its MySQL, where the worker claims
// from it (apps/forge-codegraph-worker/internal/jobqueue).
//
// Every write is a bounded transaction or an idempotent at-least-once apply;
// no transaction spans a batch boundary or holds across CPU-heavy work.
// Readers use single-use or bounded read-only transactions. Values are decoded
// from JSON and checked with their Validate method; nothing else is verified on
// read.
package spannerstore

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/api/iterator"
	"google.golang.org/api/option"
	"google.golang.org/grpc/codes"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

// Limits bound page sizes and payloads. Zero fields take the defaults.
type Limits struct {
	MaxPageSize     int   // rows per page for readers and ListRuns (default 500)
	MaxRecordBytes  int   // largest stored graph.Fact JSON (default 1 MiB)
	MaxCursorBytes  int   // longest accepted cursor token (default 8 KiB)
	MaxContentBytes int64 // largest compressed source row (default 8 MiB)
}

func DefaultLimits() Limits {
	return Limits{MaxPageSize: 500, MaxRecordBytes: 1 << 20, MaxCursorBytes: 8 << 10, MaxContentBytes: 8 << 20}
}

func (l Limits) withDefaults() Limits {
	d := DefaultLimits()
	if l.MaxPageSize <= 0 {
		l.MaxPageSize = d.MaxPageSize
	}
	if l.MaxRecordBytes <= 0 {
		l.MaxRecordBytes = d.MaxRecordBytes
	}
	if l.MaxCursorBytes <= 0 {
		l.MaxCursorBytes = d.MaxCursorBytes
	}
	if l.MaxContentBytes <= 0 {
		l.MaxContentBytes = d.MaxContentBytes
	}
	return l
}

// Config selects the database. Scope is a deployment-wide label folded into
// cursor signatures so cursors never cross environments.
type Config struct {
	Database         string // projects/P/instances/I/databases/D
	Scope            string
	CursorSigningKey []byte // at least 16 bytes
	Limits           Limits
	// VectorLength is the embedding dimension the database was provisioned
	// with: the fixed length of CGSearchEmbeddings.Embedding and its vector
	// index. Opening a database whose column has another length fails.
	VectorLength int
}

var databaseName = regexp.MustCompile(`^projects/[^/\s]+/instances/[^/\s]+/databases/[^/\s]+$`)

type Store struct {
	client       *spanner.Client
	database     string
	vectorLength int
	scope        string
	limits       Limits
	cursors      cursorCodec
}

var _ graph.Reader = (*Store)(nil)

func New(ctx context.Context, c Config, opts ...option.ClientOption) (*Store, error) {
	if !databaseName.MatchString(c.Database) {
		return nil, fmt.Errorf("%w: database must be projects/P/instances/I/databases/D", deployment.ErrInvalidRequest)
	}
	if c.Scope == "" || len(c.Scope) > 256 {
		return nil, fmt.Errorf("%w: scope required", deployment.ErrInvalidRequest)
	}
	if len(c.CursorSigningKey) < 16 {
		return nil, fmt.Errorf("%w: cursor signing key of at least 16 bytes required", deployment.ErrInvalidRequest)
	}
	if !ValidVectorLength(c.VectorLength) {
		return nil, fmt.Errorf("%w: vector length 1..%d required", deployment.ErrInvalidRequest, MaxVectorLength)
	}
	limits := c.Limits.withDefaults()
	client, err := spanner.NewClient(ctx, c.Database, opts...)
	if err != nil {
		return nil, err
	}
	return &Store{
		client:       client,
		database:     c.Database,
		vectorLength: c.VectorLength,
		scope:        c.Scope,
		limits:       limits,
		cursors:      cursorCodec{key: append([]byte(nil), c.CursorSigningKey...), domain: c.Database + "|" + c.Scope, maxBytes: limits.MaxCursorBytes},
	}, nil
}

func (s *Store) Close() { s.client.Close() }

func (s *Store) Limits() Limits { return s.limits }

// VectorLength is the embedding dimension this store reads and writes.
func (s *Store) VectorLength() int { return s.vectorLength }

// Live reports whether the database answers at all: one single-use SELECT 1.
// It is the per-tick health probe; Ping additionally verifies the schema and
// belongs at startup, not on a five-second cadence under load.
func (s *Store) Live(ctx context.Context) error {
	it := s.client.Single().Query(ctx, spanner.Statement{SQL: "SELECT 1"})
	defer it.Stop()
	_, err := it.Next()
	return err
}

// Ping checks that the database answers and that its embedding column has
// the configured vector length, so a store never writes vectors a vector
// index of another dimension would reject, nor searches one it cannot use.
func (s *Store) Ping(ctx context.Context) error {
	it := s.client.Single().Query(ctx, spanner.Statement{SQL: "SELECT SPANNER_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='' AND TABLE_NAME='CGSearchEmbeddings' AND COLUMN_NAME='Embedding'"})
	defer it.Stop()
	row, err := it.Next()
	if err != nil {
		if errors.Is(err, iterator.Done) {
			return fmt.Errorf("%w: database %s has no CGSearchEmbeddings.Embedding column; apply the schema first", deployment.ErrInvalidRequest, s.database)
		}
		return err
	}
	var spannerType string
	if err = row.Column(0, &spannerType); err != nil {
		return err
	}
	want := fmt.Sprintf("ARRAY<FLOAT32>(vector_length=>%d)", s.vectorLength)
	if spannerType != want {
		return fmt.Errorf("%w: database %s embedding column is %s, configured %s", deployment.ErrInvalidRequest, s.database, spannerType, want)
	}
	return nil
}

func validQueue(q string) bool {
	return q != "" && len(q) <= 128 && !strings.ContainsAny(q, " :\t\r\n")
}

// validText mirrors deployment's identifier rule for ids that are not run keys.
func validText(s string, max int) bool {
	return (deployment.RunKey{RepositoryID: "x", RunID: s}).Validate() == nil && len(s) <= max
}

func validRepositoryID(id string) bool {
	return (deployment.RunKey{RepositoryID: id, RunID: "x"}).Validate() == nil
}

// reader is the read surface shared by single-use, read-only and read-write
// transactions.
type reader interface {
	ReadRow(context.Context, string, spanner.Key, []string) (*spanner.Row, error)
	ReadRowUsingIndex(context.Context, string, string, spanner.Key, []string) (*spanner.Row, error)
	Read(context.Context, string, spanner.KeySet, []string) *spanner.RowIterator
	ReadWithOptions(context.Context, string, spanner.KeySet, []string, *spanner.ReadOptions) *spanner.RowIterator
	Query(context.Context, spanner.Statement) *spanner.RowIterator
}

// rowNotFound reports whether err is the client's point-read miss. A NotFound
// status carrying a missing session or transaction is a transaction failure and
// must never be treated as an absent row.
func rowNotFound(err error) bool { return errors.Is(err, spanner.ErrRowNotFound) }

// mapRead converts a point-read miss into the caller's sentinel; other errors
// are returned wrapped so transaction failures stay visible.
func mapRead(err error, notFound error) error {
	if err == nil {
		return nil
	}
	if rowNotFound(err) {
		return notFound
	}
	if spanner.ErrCode(err) == codes.NotFound {
		return fmt.Errorf("spanner read failed (not a missing row): %w", err)
	}
	return err
}

// now returns database time from within the transaction.
func now(ctx context.Context, t reader) (time.Time, error) {
	it := t.Query(ctx, spanner.Statement{SQL: "SELECT CURRENT_TIMESTAMP()"})
	defer it.Stop()
	row, err := it.Next()
	if err != nil {
		return time.Time{}, err
	}
	var out time.Time
	if err = row.Columns(&out); err != nil {
		return time.Time{}, err
	}
	return out, nil
}

func nextRow(it *spanner.RowIterator) (*spanner.Row, error) {
	r, err := it.Next()
	if err == iterator.Done {
		return nil, nil
	}
	return r, err
}

// tx runs a read-write transaction. Aborts are retried by the client; an
// uncertain commit outcome is reported as deployment.ErrUncertainCommit and
// duplicate inserts as deployment.ErrConflictingReplay.
func (s *Store) tx(ctx context.Context, f func(context.Context, *spanner.ReadWriteTransaction) error) error {
	_, err := s.client.ReadWriteTransaction(ctx, f)
	return mapCommit(ctx, err)
}

func mapCommit(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return fmt.Errorf("%w: %w", ctx.Err(), err)
	}
	switch spanner.ErrCode(err) {
	case codes.AlreadyExists:
		return fmt.Errorf("%w: %w", deployment.ErrConflictingReplay, err)
	case codes.DeadlineExceeded, codes.Unavailable:
		return fmt.Errorf("%w: %w", deployment.ErrUncertainCommit, err)
	}
	return err
}

// apply commits mutations outside a transaction with at-least-once semantics.
// Every caller passes idempotent mutations only.
func (s *Store) apply(ctx context.Context, ms []*spanner.Mutation) error {
	if len(ms) == 0 {
		return nil
	}
	_, err := s.client.Apply(ctx, ms, spanner.ApplyAtLeastOnce())
	return mapCommit(ctx, err)
}

func payload(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}

func decode(b []byte, v interface{ Validate() error }, what string) error {
	if err := json.Unmarshal(b, v); err != nil {
		return fmt.Errorf("%w: %s payload: %v", deployment.ErrIntegrity, what, err)
	}
	if err := v.Validate(); err != nil {
		return fmt.Errorf("%w: %s payload: %v", deployment.ErrIntegrity, what, err)
	}
	return nil
}

// repoRow is the CGRepositories row: live gate plus the repository lease.
type repoRow struct {
	ID             string
	LiveGeneration int64
	LiveCommit     string
	LiveRunID      string
	LeaseOwner     string
	LeaseRunID     string
	LeaseToken     int64
	LeaseExpiresAt spanner.NullTime
	Revision       int64
	Payload        []byte
}

var repoColumns = []string{"RepositoryID", "LiveGeneration", "LiveCommit", "LiveRunID", "LeaseOwner", "LeaseRunID", "LeaseToken", "LeaseExpiresAt", "Revision", "Payload"}

// fenceColumns are the cells a fenced commit reads: the live gate and the
// lease identity, never LeaseExpiresAt, Revision or Payload. Spanner locks per
// cell, so a renewal that writes only the expiry never waits on in-flight
// fenced commits and they never wait on it; only an acquisition, which
// rewrites the identity cells, conflicts with them, as it must.
var fenceColumns = []string{"LiveGeneration", "LiveCommit", "LiveRunID", "LeaseOwner", "LeaseRunID", "LeaseToken"}

func (r repoRow) values() []any {
	return []any{r.ID, r.LiveGeneration, r.LiveCommit, r.LiveRunID, r.LeaseOwner, r.LeaseRunID, r.LeaseToken, r.LeaseExpiresAt, r.Revision, r.Payload}
}

func readRepo(ctx context.Context, t reader, id string) (repoRow, error) {
	var r repoRow
	row, err := t.ReadRow(ctx, "CGRepositories", spanner.Key{id}, repoColumns)
	if err != nil {
		return r, mapRead(err, deployment.ErrNotFound)
	}
	if err = row.Columns(&r.ID, &r.LiveGeneration, &r.LiveCommit, &r.LiveRunID, &r.LeaseOwner, &r.LeaseRunID, &r.LeaseToken, &r.LeaseExpiresAt, &r.Revision, &r.Payload); err != nil {
		return r, err
	}
	if r.LiveGeneration < 0 || r.LeaseToken < 0 || r.Revision < 0 {
		return r, fmt.Errorf("%w: repository counters", deployment.ErrIntegrity)
	}
	return r, nil
}

// readFence reads the fence cells of the repository row; the other fields of
// the result stay zero.
func readFence(ctx context.Context, t reader, id string) (repoRow, error) {
	r := repoRow{ID: id}
	row, err := t.ReadRow(ctx, "CGRepositories", spanner.Key{id}, fenceColumns)
	if err != nil {
		return r, mapRead(err, deployment.ErrNotFound)
	}
	if err = row.Columns(&r.LiveGeneration, &r.LiveCommit, &r.LiveRunID, &r.LeaseOwner, &r.LeaseRunID, &r.LeaseToken); err != nil {
		return r, err
	}
	if r.LiveGeneration < 0 || r.LeaseToken < 0 {
		return r, fmt.Errorf("%w: repository counters", deployment.ErrIntegrity)
	}
	return r, nil
}

func (r repoRow) state() (graph.RepositoryState, error) {
	repo, err := r.repository()
	if err != nil {
		return graph.RepositoryState{}, err
	}
	return graph.RepositoryState{RepositoryID: r.ID, Branch: repo.Branch, LiveGeneration: uint64(r.LiveGeneration), LiveCommit: r.LiveCommit, LiveRunID: r.LiveRunID}, nil
}

// fenceHolds reports whether the row's lease identity (owner, run, token) is
// l's. Expiry is not consulted.
func (r repoRow) fenceHolds(l deployment.Lease) bool {
	return r.LeaseOwner == l.OwnerID && r.LeaseRunID == l.Fence.Key.RunID && r.LeaseToken == int64(l.Fence.Token)
}

// leaseHeld reports whether the row's lease is the given one and unexpired at t.
func (r repoRow) leaseHeld(l deployment.Lease, t time.Time) bool {
	return r.fenceHolds(l) && r.LeaseExpiresAt.Valid && r.LeaseExpiresAt.Time.After(t)
}

// checkFence verifies inside a transaction that l is still the repository's
// lease by identity alone, reading only fenceColumns. Every acquisition,
// including a takeover of an expired lease, rewrites those cells with a higher
// token, so a holder whose lease was taken over cannot commit: its read either
// sees the new token or is aborted by the acquisition. Between expiry and a
// takeover no competing writer exists, the writes behind the fence are
// idempotent records of an unpublished generation, and the holder stops at its
// next leaseSnapshot or failed renewal; so expiry is deliberately not checked
// here, which keeps LeaseExpiresAt out of the locks these hot transactions
// take. Publish and RenewLease use checkLease, the strict form.
func checkFence(ctx context.Context, t reader, l deployment.Lease) (repoRow, error) {
	if err := l.Validate(); err != nil {
		return repoRow{}, err
	}
	r, err := readFence(ctx, t, l.Fence.Key.RepositoryID)
	if err != nil {
		return r, err
	}
	if !r.fenceHolds(l) {
		return r, fmt.Errorf("%w: repository %s lease token %d", deployment.ErrFenceLost, l.Fence.Key.RepositoryID, l.Fence.Token)
	}
	return r, nil
}

// checkLease verifies the lease against database time inside a transaction:
// identity and expiry, reading the whole row. Use it where one transaction
// decides something final (Publish, RenewLease); fenced batch commits use
// checkFence.
func checkLease(ctx context.Context, t reader, l deployment.Lease) (repoRow, time.Time, error) {
	if err := l.Validate(); err != nil {
		return repoRow{}, time.Time{}, err
	}
	r, err := readRepo(ctx, t, l.Fence.Key.RepositoryID)
	if err != nil {
		return r, time.Time{}, err
	}
	n, err := now(ctx, t)
	if err != nil {
		return r, n, err
	}
	if !r.leaseHeld(l, n) {
		return r, n, fmt.Errorf("%w: repository %s lease token %d", deployment.ErrFenceLost, l.Fence.Key.RepositoryID, l.Fence.Token)
	}
	return r, n, nil
}

// leaseSnapshot is the cheap out-of-transaction lease check used between
// idempotent batch commits: one single-use query returning the lease columns
// and database time together.
func (s *Store) leaseSnapshot(ctx context.Context, l deployment.Lease) (repoRow, error) {
	if err := l.Validate(); err != nil {
		return repoRow{}, err
	}
	it := s.client.Single().Query(ctx, spanner.Statement{
		SQL:    "SELECT LiveGeneration, LiveCommit, LiveRunID, LeaseOwner, LeaseRunID, LeaseToken, LeaseExpiresAt, CURRENT_TIMESTAMP() FROM CGRepositories WHERE RepositoryID=@id",
		Params: map[string]any{"id": l.Fence.Key.RepositoryID},
	})
	defer it.Stop()
	row, err := nextRow(it)
	if err != nil {
		return repoRow{}, err
	}
	if row == nil {
		return repoRow{}, fmt.Errorf("%w: repository %s", deployment.ErrNotFound, l.Fence.Key.RepositoryID)
	}
	r := repoRow{ID: l.Fence.Key.RepositoryID}
	var n time.Time
	if err = row.Columns(&r.LiveGeneration, &r.LiveCommit, &r.LiveRunID, &r.LeaseOwner, &r.LeaseRunID, &r.LeaseToken, &r.LeaseExpiresAt, &n); err != nil {
		return r, err
	}
	if !r.leaseHeld(l, n) {
		return r, fmt.Errorf("%w: repository %s lease token %d", deployment.ErrFenceLost, l.Fence.Key.RepositoryID, l.Fence.Token)
	}
	return r, nil
}
