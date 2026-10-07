package spannerstore

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"

	"ei-aitiger-codegraph/authorization"
	"ei-aitiger-codegraph/pkg/deployment"
)

const registryMaxBytes = 256 << 10

func registryPayload(v interface{ Validate() error }) ([]byte, error) {
	if err := v.Validate(); err != nil {
		return nil, err
	}
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	if len(b) > registryMaxBytes {
		return nil, fmt.Errorf("%w: registry payload", deployment.ErrLimitExceeded)
	}
	return b, nil
}

// --- repositories ---------------------------------------------------------

func (s *Store) GetRepository(ctx context.Context, id string) (deployment.Repository, error) {
	if !validRepositoryID(id) {
		return deployment.Repository{}, fmt.Errorf("%w: repository id", deployment.ErrInvalidRequest)
	}
	row, err := readRepo(ctx, s.client.Single(), id)
	if err != nil {
		return deployment.Repository{}, err
	}
	repo, err := row.repository()
	if err != nil {
		return repo, err
	}
	if authorization.HasContext(ctx) {
		if err = authorizeRepository(ctx, s.client.Single(), repo, "read"); err != nil {
			return deployment.Repository{}, err
		}
	}
	return repo, nil
}

func (r repoRow) repository() (deployment.Repository, error) {
	var out deployment.Repository
	if err := decode(r.Payload, &out, "repository"); err != nil {
		return out, err
	}
	if out.RepositoryID != r.ID {
		return out, fmt.Errorf("%w: repository payload id", deployment.ErrIntegrity)
	}
	out.Revision = uint64(r.Revision)
	return out, nil
}

// PutRepository inserts a registration when Revision is zero and otherwise
// replaces it under compare-and-set on Revision. The live gate and lease
// columns are never touched here.
func (s *Store) PutRepository(ctx context.Context, r deployment.Repository) (deployment.Repository, error) {
	if _, err := registryPayload(r); err != nil {
		return deployment.Repository{}, err
	}
	if r.Revision >= math.MaxInt64 {
		return deployment.Repository{}, deployment.ErrLimitExceeded
	}
	if authorization.HasContext(ctx) {
		identity, err := authorization.CurrentIdentity(ctx)
		if err != nil {
			return deployment.Repository{}, err
		}
		if r.TenantID != "" && r.TenantID != identity.TenantID {
			return deployment.Repository{}, authorization.ErrNotFound
		}
		r.TenantID = identity.TenantID
	}
	out := r
	out.Revision = r.Revision + 1
	body := payload(out)
	err := s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		stored, err := readRepo(ctx, t, r.RepositoryID)
		if r.Revision == 0 {
			if err == nil {
				return fmt.Errorf("%w: repository %s already registered", deployment.ErrConflictingReplay, r.RepositoryID)
			}
			if !errors.Is(err, deployment.ErrNotFound) {
				return err
			}
			if authorization.HasContext(ctx) {
				if err = authorization.RequireCapability(ctx, "repository_collection", "register"); err != nil {
					return err
				}
			}
			row := repoRow{ID: r.RepositoryID, Revision: int64(out.Revision), Payload: body}
			return t.BufferWrite([]*spanner.Mutation{spanner.Insert("CGRepositories", repoColumns, row.values())})
		}
		if err != nil {
			return err
		}
		if stored.Revision != int64(r.Revision) {
			return fmt.Errorf("%w: repository revision %d, stored %d", deployment.ErrConflictingReplay, r.Revision, stored.Revision)
		}
		registered, err := stored.repository()
		if err != nil {
			return err
		}
		if registered.TenantID != "" && registered.TenantID != r.TenantID {
			return authorization.ErrInvalid
		}
		if authorization.HasContext(ctx) {
			if err = authorizeRepository(ctx, t, registered, "update"); err != nil {
				return err
			}
		}
		if registered.Branch != "" && registered.Branch != r.Branch {
			return fmt.Errorf("%w: repository is locked to branch %q", deployment.ErrConflictingReplay, registered.Branch)
		}
		if registered.LastIngestionCommit != r.LastIngestionCommit {
			return fmt.Errorf("%w: ingestion commit is managed by admission", deployment.ErrConflictingReplay)
		}
		return t.BufferWrite([]*spanner.Mutation{spanner.Update("CGRepositories", []string{"RepositoryID", "Revision", "Payload"}, []any{r.RepositoryID, int64(out.Revision), body})})
	})
	if err != nil {
		return deployment.Repository{}, err
	}
	return out, nil
}

// --- runs -----------------------------------------------------------------

var runColumns = []string{"RepositoryID", "RunID", "DeploymentID", "DeploymentSequence", "CommitSHA", "ConfigDigest", "IdentityDigest", "Phase", "Generation", "Revision", "AcceptedAt", "UpdatedAt", "Payload"}

func runMutation(r deployment.Run, identity string) (*spanner.Mutation, error) {
	b, err := registryPayload(r)
	if err != nil {
		return nil, err
	}
	q := r.Request
	return spanner.InsertOrUpdate("CGRuns", runColumns, []any{r.Key.RepositoryID, r.Key.RunID, q.DeploymentID, int64(q.DeploymentSequence), q.TargetCommitSHA, q.AnalysisConfigDigest, identity, string(r.Phase), int64(r.Generation), int64(r.Revision), r.AcceptedAt, r.UpdatedAt, b}), nil
}

func decodeRun(b []byte, key deployment.RunKey) (deployment.Run, error) {
	var r deployment.Run
	if err := decode(b, &r, "run"); err != nil {
		return r, err
	}
	if r.Key != key {
		return r, fmt.Errorf("%w: run payload key", deployment.ErrIntegrity)
	}
	return r, nil
}

// readRun returns the run and its stored identity digest.
func readRun(ctx context.Context, t reader, key deployment.RunKey) (deployment.Run, string, error) {
	row, err := t.ReadRow(ctx, "CGRuns", spanner.Key{key.RepositoryID, key.RunID}, []string{"IdentityDigest", "Payload"})
	if err != nil {
		return deployment.Run{}, "", mapRead(err, fmt.Errorf("%w: run %s/%s", deployment.ErrNotFound, key.RepositoryID, key.RunID))
	}
	var identity string
	var b []byte
	if err = row.Columns(&identity, &b); err != nil {
		return deployment.Run{}, "", err
	}
	r, err := decodeRun(b, key)
	return r, identity, err
}

func (s *Store) GetRun(ctx context.Context, key deployment.RunKey) (deployment.Run, error) {
	if err := key.Validate(); err != nil {
		return deployment.Run{}, err
	}
	r, _, err := readRun(ctx, s.client.Single(), key)
	return r, err
}

type runPosition struct {
	Sequence uint64 `json:"seq"`
	RunID    string `json:"run"`
}

// ListRuns pages a repository's runs newest deployment first.
func (s *Store) ListRuns(ctx context.Context, repo string, limit int, cursor string) ([]deployment.Run, string, error) {
	if !validRepositoryID(repo) {
		return nil, "", fmt.Errorf("%w: repository id", deployment.ErrInvalidRequest)
	}
	if limit <= 0 || limit > s.limits.MaxPageSize {
		limit = s.limits.MaxPageSize
	}
	scope := s.cursors.scope("runs", repo)
	position, err := s.cursors.decode(scope, 0, cursor)
	if err != nil {
		return nil, "", err
	}
	stmt := spanner.Statement{
		SQL:    "SELECT RunID, Payload FROM CGRuns WHERE RepositoryID=@repo ORDER BY DeploymentSequence DESC, RunID LIMIT @limit",
		Params: map[string]any{"repo": repo, "limit": int64(limit + 1)},
	}
	if position != "" {
		var p runPosition
		if json.Unmarshal([]byte(position), &p) != nil {
			return nil, "", fmt.Errorf("%w: cursor position", deployment.ErrInvalidRequest)
		}
		stmt.SQL = "SELECT RunID, Payload FROM CGRuns WHERE RepositoryID=@repo AND (DeploymentSequence<@seq OR (DeploymentSequence=@seq AND RunID>@run)) ORDER BY DeploymentSequence DESC, RunID LIMIT @limit"
		stmt.Params["seq"] = int64(p.Sequence)
		stmt.Params["run"] = p.RunID
	}
	it := s.client.Single().Query(ctx, stmt)
	defer it.Stop()
	var out []deployment.Run
	for {
		row, err := nextRow(it)
		if err != nil {
			return nil, "", err
		}
		if row == nil {
			return out, "", nil
		}
		var id string
		var b []byte
		if err = row.Columns(&id, &b); err != nil {
			return nil, "", err
		}
		if len(out) == limit {
			last := out[limit-1]
			return out, s.cursors.encode(scope, 0, string(payload(runPosition{Sequence: last.Request.DeploymentSequence, RunID: last.Key.RunID}))), nil
		}
		r, err := decodeRun(b, deployment.RunKey{RepositoryID: repo, RunID: id})
		if err != nil {
			return nil, "", err
		}
		out = append(out, r)
	}
}

// UpdateRun replaces a run under compare-and-set on Revision and the phase
// graph. Identity fields (key, request, acceptance time) are kept from the
// stored row; Revision and UpdatedAt are assigned here. A run becomes
// SUCCEEDED only through Publish (lexical-only) or the fenced UpdateIndex.
func (s *Store) UpdateRun(ctx context.Context, run deployment.Run) (deployment.Run, error) {
	if err := run.Validate(); err != nil {
		return deployment.Run{}, err
	}
	var out deployment.Run
	err := s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		stored, identity, err := readRun(ctx, t, run.Key)
		if err != nil {
			return err
		}
		if stored.Revision != run.Revision {
			return fmt.Errorf("%w: run revision %d, stored %d", deployment.ErrConflictingReplay, run.Revision, stored.Revision)
		}
		if err = validateRunTransition(stored, run); err != nil {
			return err
		}
		if run.Phase == deployment.Succeeded && stored.Phase != deployment.Succeeded {
			return fmt.Errorf("%w: %s -> %s requires Publish", deployment.ErrInvalidTransition, stored.Phase, run.Phase)
		}
		n, err := now(ctx, t)
		if err != nil {
			return err
		}
		out = run
		out.SchemaVersion, out.Key, out.Request, out.AcceptedAt = stored.SchemaVersion, stored.Key, stored.Request, stored.AcceptedAt
		if stored.Revision >= math.MaxInt64 {
			return deployment.ErrLimitExceeded
		}
		out.Revision = stored.Revision + 1
		out.UpdatedAt = n
		m, err := runMutation(out, identity)
		if err != nil {
			return err
		}
		return t.BufferWrite([]*spanner.Mutation{m})
	})
	if err != nil {
		return deployment.Run{}, err
	}
	return out, nil
}

// --- admission ------------------------------------------------------------

func newRunID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return "run-" + hex.EncodeToString(b[:]), nil
}

// Admit creates the run, its READY job and the caller's submission record in
// one transaction, or returns the run an identical request already produced.
// A request at or below a published sequence is stale, except that an analysis
// refresh may target that sequence itself.
func (s *Store) Admit(ctx context.Context, a deployment.Admission) (deployment.AdmissionResult, error) {
	if err := a.Validate(); err != nil {
		return deployment.AdmissionResult{}, err
	}
	identity, err := a.Request.IdentityDigest()
	if err != nil {
		return deployment.AdmissionResult{}, err
	}
	a.Request.DeployedAt = a.Request.DeployedAt.UTC()
	for attempt := 0; ; attempt++ {
		out, err := s.admit(ctx, a, identity, false)
		// A concurrent identical admission can win the unique identity index at
		// commit; the second pass returns its run as a reuse.
		if err != nil && spanner.ErrCode(err) == codes.AlreadyExists && attempt == 0 {
			continue
		}
		return out, err
	}
}

// AdmitIngestion assigns omitted metadata inside the admission transaction.
// A caller's submission ID is checked before generating anything, so retries
// preserve the original sequence, timestamp, identity and job.
func (s *Store) AdmitIngestion(ctx context.Context, a deployment.Admission) (deployment.AdmissionResult, error) {
	if err := a.ValidateIngestion(); err != nil {
		return deployment.AdmissionResult{}, err
	}
	if a.Request.DeploymentID != "" && a.Request.DeploymentSequence != 0 && !a.Request.DeployedAt.IsZero() {
		return s.Admit(ctx, a)
	}
	for attempt := 0; ; attempt++ {
		out, err := s.admit(ctx, a, "", true)
		if err != nil && spanner.ErrCode(err) == codes.AlreadyExists && attempt == 0 {
			continue
		}
		return out, err
	}
}

func (s *Store) admit(ctx context.Context, a deployment.Admission, expectedIdentity string, automatic bool) (out deployment.AdmissionResult, err error) {
	repo := a.Request.RepositoryID
	runID, err := newRunID()
	if err != nil {
		return out, err
	}
	err = s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		out = deployment.AdmissionResult{}
		q := a.Request // Reset generated values on every transaction retry.
		identity := expectedIdentity
		registered, err := readRepo(ctx, t, repo)
		if err != nil {
			return err
		}
		metadata, err := registered.repository()
		if err != nil {
			return err
		}
		if authorization.HasContext(ctx) {
			if err = authorizeRepository(ctx, t, metadata, "ingest"); err != nil {
				return err
			}
		}
		if metadata.Branch != "" && metadata.Branch != q.Branch {
			return fmt.Errorf("%w: repository is locked to branch %q", deployment.ErrConflictingReplay, metadata.Branch)
		}
		if a.SubmissionID != "" {
			row, err := t.ReadRow(ctx, "CGSubmissions", spanner.Key{a.SubmissionID}, []string{"RepositoryID", "RunID"})
			if err == nil {
				var prior deployment.RunKey
				if err = row.Columns(&prior.RepositoryID, &prior.RunID); err != nil {
					return err
				}
				run, storedIdentity, err := readRun(ctx, t, prior)
				if err != nil {
					return err
				}
				if automatic {
					// Only omitted fields may reuse generated values. Changed
					// repository, commit, configuration or supplied metadata conflicts.
					if q.DeploymentID == "" {
						q.DeploymentID = run.Request.DeploymentID
					}
					if q.DeploymentSequence == 0 {
						q.DeploymentSequence = run.Request.DeploymentSequence
					}
					if q.DeployedAt.IsZero() {
						q.DeployedAt = run.Request.DeployedAt
					}
					identity, err = q.IdentityDigest()
					if err != nil {
						return err
					}
				}
				if storedIdentity != identity {
					return fmt.Errorf("%w: submission %s was a different request", deployment.ErrConflictingReplay, a.SubmissionID)
				}
				out = deployment.AdmissionResult{Run: run, Reused: true}
				return nil
			}
			if !rowNotFound(err) {
				return mapRead(err, err)
			}
		}
		if a.CheckedLiveCommit != nil && registered.LiveCommit != *a.CheckedLiveCommit {
			return fmt.Errorf("%w: the published commit changed during validation; submit again", deployment.ErrStaleDeployment)
		}
		if a.CheckedIngestionCommit != nil && metadata.LastIngestionCommit != *a.CheckedIngestionCommit {
			return fmt.Errorf("%w: another ingestion was accepted during validation; submit again", deployment.ErrStaleDeployment)
		}
		// Pin the first accepted branch in this transaction, not registration:
		// concurrent first submissions cannot establish different branches.
		if metadata.Branch == "" {
			metadata.Branch = q.Branch
		}
		if automatic && q.DeploymentID == "" && q.DeploymentSequence == 0 && q.DeployedAt.IsZero() && registered.LiveRunID != "" && registered.LiveCommit == q.TargetCommitSHA {
			live, _, err := readRun(ctx, t, deployment.RunKey{RepositoryID: repo, RunID: registered.LiveRunID})
			if err != nil {
				return err
			}
			if live.Request.Branch == q.Branch && live.Request.AnalysisConfigDigest == q.AnalysisConfigDigest {
				out = deployment.AdmissionResult{Run: live, Reused: true}
				return writeSubmission(t, a.SubmissionID, live.Key)
			}
		}
		n, err := now(ctx, t)
		if err != nil {
			return err
		}
		if automatic {
			if q.DeploymentID == "" {
				q.DeploymentID = "ingestion-" + runID
			}
			if q.DeployedAt.IsZero() {
				q.DeployedAt = n
			}
			if q.DeploymentSequence == 0 {
				it := t.Query(ctx, spanner.Statement{
					SQL:    "SELECT MAX(DeploymentSequence) FROM CGRuns WHERE RepositoryID=@repo",
					Params: map[string]any{"repo": repo},
				})
				row, readErr := nextRow(it)
				it.Stop()
				if readErr != nil {
					return readErr
				}
				var latest spanner.NullInt64
				if row != nil {
					if err := row.Columns(&latest); err != nil {
						return err
					}
				}
				if latest.Valid && latest.Int64 == math.MaxInt64 {
					return fmt.Errorf("%w: ingestion sequence exhausted", deployment.ErrInvalidRequest)
				}
				q.DeploymentSequence = uint64(latest.Int64) + 1
			}
			identity, err = q.IdentityDigest()
			if err != nil {
				return err
			}
		}
		q.DeployedAt = q.DeployedAt.UTC()
		row, err := t.ReadRowUsingIndex(ctx, "CGRuns", "CGRunsByIdentity", spanner.Key{repo, identity}, []string{"RunID"})
		if err == nil {
			var id string
			if err = row.Columns(&id); err != nil {
				return err
			}
			run, _, err := readRun(ctx, t, deployment.RunKey{RepositoryID: repo, RunID: id})
			if err != nil {
				return err
			}
			out = deployment.AdmissionResult{Run: run, Reused: true}
			return writeSubmission(t, a.SubmissionID, run.Key)
		}
		if !rowNotFound(err) {
			return mapRead(err, err)
		}
		it := t.Query(ctx, spanner.Statement{
			SQL:    "SELECT MAX(DeploymentSequence) FROM CGRuns@{FORCE_INDEX=CGRunsByPhase} WHERE RepositoryID=@repo AND Phase=@phase",
			Params: map[string]any{"repo": repo, "phase": string(deployment.Succeeded)},
		})
		r, err := nextRow(it)
		it.Stop()
		if err != nil {
			return err
		}
		var succeeded spanner.NullInt64
		if r != nil {
			if err = r.Columns(&succeeded); err != nil {
				return err
			}
		}
		// Publication establishes ordering even while required embeddings are
		// running or failed. Never admit an older deployment over that graph.
		if registered.LiveRunID != "" {
			live, _, err := readRun(ctx, t, deployment.RunKey{RepositoryID: repo, RunID: registered.LiveRunID})
			if err != nil {
				return err
			}
			if !succeeded.Valid || int64(live.Request.DeploymentSequence) > succeeded.Int64 {
				succeeded = spanner.NullInt64{Int64: int64(live.Request.DeploymentSequence), Valid: true}
			}
		}
		if succeeded.Valid {
			seq := int64(q.DeploymentSequence)
			if seq < succeeded.Int64 || (seq == succeeded.Int64 && q.TriggerKind != deployment.TriggerAnalysisRefresh) {
				return fmt.Errorf("%w: sequence %d, succeeded %d", deployment.ErrStaleDeployment, seq, succeeded.Int64)
			}
		}
		key := deployment.RunKey{RepositoryID: repo, RunID: runID}
		run := deployment.Run{SchemaVersion: deployment.SchemaVersion, Key: key, Request: q, Phase: deployment.Accepted, Revision: 1, AcceptedAt: n, UpdatedAt: n}
		b, err := registryPayload(run)
		if err != nil {
			return err
		}
		ms := []*spanner.Mutation{
			spanner.Insert("CGRuns", runColumns, []any{key.RepositoryID, key.RunID, q.DeploymentID, int64(q.DeploymentSequence), q.TargetCommitSHA, q.AnalysisConfigDigest, identity, string(run.Phase), int64(0), int64(1), n, n, b}),
		}
		if metadata.Revision >= math.MaxInt64 {
			return deployment.ErrLimitExceeded
		}
		metadata.Revision++
		metadata.LastIngestionCommit = q.TargetCommitSHA
		ms = append(ms, spanner.Update("CGRepositories", []string{"RepositoryID", "Revision", "Payload"}, []any{repo, int64(metadata.Revision), payload(metadata)}))
		if err = t.BufferWrite(ms); err != nil {
			return err
		}
		out = deployment.AdmissionResult{Run: run}
		return writeSubmission(t, a.SubmissionID, key)
	})
	return out, err
}

func writeSubmission(t *spanner.ReadWriteTransaction, submission string, key deployment.RunKey) error {
	if submission == "" {
		return nil
	}
	return t.BufferWrite([]*spanner.Mutation{spanner.InsertOrUpdate("CGSubmissions", []string{"SubmissionID", "RepositoryID", "RunID"}, []any{submission, key.RepositoryID, key.RunID})})
}
