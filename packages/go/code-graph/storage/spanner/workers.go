package spannerstore

import (
	"context"
	"fmt"
	"slices"
	"sort"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"

	"ei-aitiger-codegraph/pkg/deployment"
)

// WorkerRegistration is one running worker process on a queue: the analysis
// configuration digest it claims jobs for, the languages it binds, and the
// settings an operator surface reports for it. A worker registers at startup, refreshes its
// heartbeat while it runs and deletes its row on a clean shutdown; a row
// whose heartbeat is older than the caller's window belongs to a worker that
// stopped without cleaning up. Admission reads the live workers' digest so a
// process that never links the analysis stack still binds every run to the
// configuration the fleet will claim.
type WorkerRegistration struct {
	Queue        string    `json:"queue"`
	OwnerID      string    `json:"owner_id"`
	ConfigDigest string    `json:"config_digest"`
	Languages    []string  `json:"languages"`
	BuildMode    string    `json:"build_mode"`
	MaxAttempts  int       `json:"max_attempts"`
	Embeddings   bool      `json:"embeddings"`
	StartedAt    time.Time `json:"started_at"`
	HeartbeatAt  time.Time `json:"heartbeat_at"`
}

func (w WorkerRegistration) Validate() error {
	if !validQueue(w.Queue) || !validText(w.OwnerID, 256) || !deployment.ValidDigest(w.ConfigDigest) || len(w.Languages) == 0 || !validText(w.BuildMode, 64) || w.MaxAttempts < 1 || w.StartedAt.IsZero() {
		return fmt.Errorf("%w: worker registration", deployment.ErrInvalidRequest)
	}
	seen := map[string]bool{}
	for _, language := range w.Languages {
		if !validText(language, 64) || seen[language] {
			return fmt.Errorf("%w: worker registration languages", deployment.ErrInvalidRequest)
		}
		seen[language] = true
	}
	return nil
}

// sortedLanguages is the stored form: unique names in lexical order.
func (w WorkerRegistration) sortedLanguages() []string {
	out := slices.Clone(w.Languages)
	sort.Strings(out)
	return out
}

var workerColumns = []string{"Queue", "OwnerID", "ConfigDigest", "Languages", "BuildMode", "MaxAttempts", "Embeddings", "StartedAt", "HeartbeatAt"}

// RegisterWorker records or refreshes a worker's registration; HeartbeatAt is
// the commit time. It is an idempotent blind write, so a worker calls it at
// startup and on every heartbeat.
func (s *Store) RegisterWorker(ctx context.Context, w WorkerRegistration) error {
	if err := w.Validate(); err != nil {
		return err
	}
	return s.apply(ctx, []*spanner.Mutation{spanner.InsertOrUpdate("CGWorkers", workerColumns,
		[]any{w.Queue, w.OwnerID, w.ConfigDigest, w.sortedLanguages(), w.BuildMode, int64(w.MaxAttempts), w.Embeddings, w.StartedAt.UTC(), spanner.CommitTimestamp})})
}

// UnregisterWorker removes a worker's registration. Removing an absent row
// succeeds.
func (s *Store) UnregisterWorker(ctx context.Context, queue, ownerID string) error {
	if !validQueue(queue) || !validText(ownerID, 256) {
		return fmt.Errorf("%w: worker registration", deployment.ErrInvalidRequest)
	}
	return s.apply(ctx, []*spanner.Mutation{spanner.Delete("CGWorkers", spanner.Key{queue, ownerID})})
}

// ActiveWorkers lists the workers on a queue whose heartbeat is younger than
// within, by database time, ordered by owner.
func (s *Store) ActiveWorkers(ctx context.Context, queue string, within time.Duration) ([]WorkerRegistration, error) {
	if !validQueue(queue) || within <= 0 {
		return nil, fmt.Errorf("%w: active workers query", deployment.ErrInvalidRequest)
	}
	workers, err := s.activeWorkers(ctx, queue, within, false)
	if spanner.ErrCode(err) == codes.InvalidArgument {
		// Older installations store one Language. Permit the admin API to
		// read that fleet during a rolling upgrade to the Languages schema.
		return s.activeWorkers(ctx, queue, within, true)
	}
	return workers, err
}

func (s *Store) activeWorkers(ctx context.Context, queue string, within time.Duration, legacy bool) ([]WorkerRegistration, error) {
	languages := "Languages"
	if legacy {
		languages = "[Language] AS Languages"
	}
	it := s.client.Single().Query(ctx, spanner.Statement{
		SQL:    "SELECT Queue, OwnerID, ConfigDigest, " + languages + ", BuildMode, MaxAttempts, Embeddings, StartedAt, HeartbeatAt, CURRENT_TIMESTAMP() FROM CGWorkers WHERE Queue=@queue ORDER BY OwnerID",
		Params: map[string]any{"queue": queue},
	})
	defer it.Stop()
	out := []WorkerRegistration{}
	for {
		row, err := nextRow(it)
		if err != nil {
			return nil, err
		}
		if row == nil {
			return out, nil
		}
		var w WorkerRegistration
		var attempts int64
		var n time.Time
		if err = row.Columns(&w.Queue, &w.OwnerID, &w.ConfigDigest, &w.Languages, &w.BuildMode, &attempts, &w.Embeddings, &w.StartedAt, &w.HeartbeatAt, &n); err != nil {
			return nil, err
		}
		if attempts < 1 {
			return nil, fmt.Errorf("%w: worker registration counters", deployment.ErrIntegrity)
		}
		w.MaxAttempts = int(attempts)
		if w.HeartbeatAt.After(n.Add(-within)) {
			out = append(out, w)
		}
	}
}
