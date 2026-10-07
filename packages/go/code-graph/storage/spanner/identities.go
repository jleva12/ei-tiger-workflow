package spannerstore

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"io"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/semantic"
)

func gzipBytes(b []byte) ([]byte, error) {
	var buf bytes.Buffer
	w := gzip.NewWriter(&buf)
	if _, err := w.Write(b); err != nil {
		return nil, err
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func gunzipBytes(b []byte, max int64) ([]byte, error) {
	r, err := gzip.NewReader(bytes.NewReader(b))
	if err != nil {
		return nil, fmt.Errorf("%w: gzip header: %v", deployment.ErrIntegrity, err)
	}
	out, err := io.ReadAll(io.LimitReader(r, max+1))
	if err != nil {
		return nil, fmt.Errorf("%w: gzip body: %v", deployment.ErrIntegrity, err)
	}
	if int64(len(out)) > max {
		return nil, fmt.Errorf("%w: decompressed payload exceeds %d bytes", deployment.ErrLimitExceeded, max)
	}
	return out, nil
}

const (
	identityRowsPerCommit  = 200
	identityBytesPerCommit = 4 << 20
	maxIdentityBytes       = 64 << 20
)

// PutFileIdentities stores the identity map of each file lineage at
// generation under the repository lease. Rows are gzip JSON, written
// InsertOrUpdate in bounded batches; every batch is a fenced commit, so a
// worker that lost the lease cannot leave a map behind for the next run to
// continue identities from.
func (s *Store) PutFileIdentities(ctx context.Context, lease deployment.Lease, generation uint64, maps []semantic.FileIdentities) error {
	if err := lease.Validate(); err != nil {
		return err
	}
	repo := lease.Fence.Key.RepositoryID
	if generation == 0 {
		return fmt.Errorf("%w: generation", deployment.ErrInvalidRequest)
	}
	var ms []*spanner.Mutation
	size := 0
	flush := func() error {
		err := s.fencedCommit(ctx, lease, belowLive(generation), ms)
		ms, size = nil, 0
		return err
	}
	for _, m := range maps {
		if !graph.ValidID(m.Lineage) || len(m.ContentSHA256) != 64 {
			return fmt.Errorf("%w: file identities lineage %q", deployment.ErrInvalidRequest, m.Lineage)
		}
		raw, err := json.Marshal(m)
		if err != nil {
			return err
		}
		if len(raw) > maxIdentityBytes {
			return fmt.Errorf("%w: identities for %s", deployment.ErrLimitExceeded, m.Lineage)
		}
		z, err := gzipBytes(raw)
		if err != nil {
			return err
		}
		ms = append(ms, spanner.InsertOrUpdate("CGFileIdentities", []string{"RepositoryID", "Lineage", "Generation", "Path", "ContentSHA256", "Payload"}, []any{repo, m.Lineage, int64(generation), m.Path, m.ContentSHA256, z}))
		size += len(z)
		if len(ms) >= identityRowsPerCommit || size >= identityBytesPerCommit {
			if err = flush(); err != nil {
				return err
			}
		}
	}
	return flush()
}

// GetFileIdentities returns the latest identity map of a lineage at or below
// generation (zero means live). deployment.ErrNotFound when none exists.
func (s *Store) GetFileIdentities(ctx context.Context, repo, lineage string, generation uint64) (semantic.FileIdentities, error) {
	if !validRepositoryID(repo) || !graph.ValidID(lineage) {
		return semantic.FileIdentities{}, fmt.Errorf("%w: repository and lineage", deployment.ErrInvalidRequest)
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen := generation
	if gen == 0 {
		r, err := readRepo(ctx, t, repo)
		if err != nil {
			return semantic.FileIdentities{}, err
		}
		gen = uint64(r.LiveGeneration)
	}
	it := t.Query(ctx, spanner.Statement{
		SQL:    "SELECT Payload FROM CGFileIdentities WHERE RepositoryID=@repo AND Lineage=@lineage AND Generation<=@gen ORDER BY Generation DESC LIMIT 1",
		Params: map[string]any{"repo": repo, "lineage": lineage, "gen": int64(gen)},
	})
	defer it.Stop()
	row, err := nextRow(it)
	if err != nil {
		return semantic.FileIdentities{}, err
	}
	if row == nil {
		return semantic.FileIdentities{}, fmt.Errorf("%w: identities for %s at generation %d", deployment.ErrNotFound, lineage, gen)
	}
	var z []byte
	if err = row.Columns(&z); err != nil {
		return semantic.FileIdentities{}, err
	}
	raw, err := gunzipBytes(z, maxIdentityBytes)
	if err != nil {
		return semantic.FileIdentities{}, err
	}
	var out semantic.FileIdentities
	if err = json.Unmarshal(raw, &out); err != nil {
		return semantic.FileIdentities{}, fmt.Errorf("%w: identities payload: %v", deployment.ErrIntegrity, err)
	}
	if out.Lineage != lineage {
		return semantic.FileIdentities{}, fmt.Errorf("%w: identities payload lineage", deployment.ErrIntegrity)
	}
	return out, nil
}

// LineagesByPath lists every file lineage that had an identity map with the
// given repository-relative path at or before generation (zero means live).
// A lineage's path never changes, so this finds the compilation variants of a
// path that no longer exists in the checkout, such as a deleted or renamed file.
func (s *Store) LineagesByPath(ctx context.Context, repo, path string, generation uint64) ([]string, error) {
	if !validRepositoryID(repo) || path == "" {
		return nil, fmt.Errorf("%w: repository and path", deployment.ErrInvalidRequest)
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen := generation
	if gen == 0 {
		r, err := readRepo(ctx, t, repo)
		if err != nil {
			return nil, err
		}
		gen = uint64(r.LiveGeneration)
	}
	it := t.Query(ctx, spanner.Statement{
		SQL:    "SELECT DISTINCT Lineage FROM CGFileIdentities@{FORCE_INDEX=CGFileIdentitiesByPath} WHERE RepositoryID=@repo AND Path=@path AND Generation<=@gen",
		Params: map[string]any{"repo": repo, "path": path, "gen": int64(gen)},
	})
	defer it.Stop()
	var out []string
	for {
		row, err := nextRow(it)
		if err != nil {
			return nil, err
		}
		if row == nil {
			return out, nil
		}
		var lineage string
		if err = row.Columns(&lineage); err != nil {
			return nil, err
		}
		out = append(out, lineage)
	}
}
