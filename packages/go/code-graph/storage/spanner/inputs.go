package spannerstore

import (
	"context"
	"encoding/json"
	"fmt"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/deployment"
)

var generationInputColumns = []string{"RepositoryID", "Generation", "ContextID", "ConfigDigest", "Payload"}

// PutGenerationInputs records what generation was computed from, under the
// repository lease and before the generation is published. The row is gzip
// JSON written InsertOrUpdate in one fenced commit; an abandoned generation's
// row is removed by SweepAboveLive.
func (s *Store) PutGenerationInputs(ctx context.Context, lease deployment.Lease, generation uint64, in deployment.GenerationInputs) error {
	if err := lease.Validate(); err != nil {
		return err
	}
	if generation == 0 {
		return fmt.Errorf("%w: generation", deployment.ErrInvalidRequest)
	}
	if err := in.Validate(); err != nil {
		return err
	}
	raw, err := json.Marshal(in)
	if err != nil {
		return err
	}
	if len(raw) > maxIdentityBytes {
		return fmt.Errorf("%w: generation inputs", deployment.ErrLimitExceeded)
	}
	z, err := gzipBytes(raw)
	if err != nil {
		return err
	}
	m := spanner.InsertOrUpdate("CGGenerationInputs", generationInputColumns, []any{lease.Fence.Key.RepositoryID, int64(generation), in.ContextID, in.AnalysisConfigDigest, z})
	return s.fencedCommit(ctx, lease, belowLive(generation), []*spanner.Mutation{m})
}

// GetGenerationInputs returns the inputs recorded for exactly generation.
// deployment.ErrNotFound when the generation predates input recording or was
// never published.
func (s *Store) GetGenerationInputs(ctx context.Context, repo string, generation uint64) (deployment.GenerationInputs, error) {
	if !validRepositoryID(repo) || generation == 0 {
		return deployment.GenerationInputs{}, fmt.Errorf("%w: repository and generation", deployment.ErrInvalidRequest)
	}
	row, err := s.client.Single().ReadRow(ctx, "CGGenerationInputs", spanner.Key{repo, int64(generation)}, []string{"Payload"})
	if err != nil {
		return deployment.GenerationInputs{}, mapRead(err, fmt.Errorf("%w: inputs of generation %d", deployment.ErrNotFound, generation))
	}
	var z []byte
	if err = row.Columns(&z); err != nil {
		return deployment.GenerationInputs{}, err
	}
	raw, err := gunzipBytes(z, maxIdentityBytes)
	if err != nil {
		return deployment.GenerationInputs{}, err
	}
	var out deployment.GenerationInputs
	if err = decode(raw, &out, "generation inputs"); err != nil {
		return deployment.GenerationInputs{}, err
	}
	return out, nil
}
