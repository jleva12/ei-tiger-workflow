package spannerstore

import (
	"context"
	"errors"
	"fmt"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

// RepositoryStats totals a repository's live graph for an ingestion audit.
// Kind maps count open records by their node or edge kind; Nodes and Edges
// are their sums. Searchable counts the open nodes that carry a search
// document. Before the first publication every count is zero.
type RepositoryStats struct {
	RepositoryID string            `json:"repository_id"`
	Branch       string            `json:"branch"`
	Generation   uint64            `json:"generation"`
	CommitSHA    string            `json:"commit_sha"`
	RunID        string            `json:"run_id"`
	Nodes        uint64            `json:"nodes"`
	Edges        uint64            `json:"edges"`
	NodeKinds    map[string]uint64 `json:"node_kinds"`
	EdgeKinds    map[string]uint64 `json:"edge_kinds"`
	Searchable   uint64            `json:"searchable"`
	Embeddings   EmbeddingStats    `json:"embeddings"`
}

// EmbeddingStats counts the stored vectors of one embedding model and
// dimension. Current is the searchable open nodes whose vector matches their
// document; Stored is every vector of the repository for the model, stale
// ones included. An empty Model means embeddings are off and nothing was
// counted.
type EmbeddingStats struct {
	Model      string `json:"model"`
	Dimensions int    `json:"dimensions"`
	Current    uint64 `json:"current"`
	Stored     uint64 `json:"stored"`
}

// Stats totals the repository's live generation, and its embeddings for
// (model, dimensions) unless model is empty, from one consistent snapshot.
func (s *Store) Stats(ctx context.Context, repo, model string, dimensions int) (RepositoryStats, error) {
	if !validRepositoryID(repo) {
		return RepositoryStats{}, fmt.Errorf("%w: repository id", graph.ErrInvalid)
	}
	if model != "" && (len(model) > 256 || dimensions < 1) {
		return RepositoryStats{}, fmt.Errorf("%w: embedding model and dimensions", graph.ErrInvalid)
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	r, err := readRepo(ctx, t, repo)
	if err != nil {
		if errors.Is(err, deployment.ErrNotFound) {
			return RepositoryStats{}, fmt.Errorf("%w: repository %s", graph.ErrNotFound, repo)
		}
		return RepositoryStats{}, err
	}
	state, err := r.state()
	if err != nil {
		return RepositoryStats{}, err
	}
	out := RepositoryStats{
		RepositoryID: repo, Branch: state.Branch, Generation: state.LiveGeneration, CommitSHA: state.LiveCommit, RunID: state.LiveRunID,
		NodeKinds: map[string]uint64{}, EdgeKinds: map[string]uint64{},
	}
	if model != "" {
		out.Embeddings = EmbeddingStats{Model: model, Dimensions: dimensions}
	}
	if out.Generation == 0 {
		return out, nil
	}
	if err = countKinds(ctx, t, repo, out.Generation, &out); err != nil {
		return RepositoryStats{}, err
	}
	// Search hashes live on the base table, so searchable nodes are read from
	// its (RepositoryID, RecordKind) key prefix, joined to their vectors when
	// embeddings are on; a model's stored vectors are a key prefix of theirs.
	var searchable, current, stored int64
	if model == "" {
		err = queryCounts(ctx, t, spanner.Statement{
			SQL:    "SELECT COUNT(*) FROM CGRecords WHERE RepositoryID=@repo AND RecordKind='node' AND SearchHash IS NOT NULL AND " + openPredicate(""),
			Params: map[string]any{"repo": repo, "gen": int64(out.Generation)},
		}, &searchable)
	} else {
		err = queryCounts(ctx, t, spanner.Statement{
			SQL: `SELECT COUNT(*), COUNTIF(e.DocumentHash=r.SearchHash AND e.DocumentVersion=@version) FROM CGRecords r
 LEFT JOIN CGSearchEmbeddings e ON e.RepositoryID=r.RepositoryID AND e.Model=@model AND e.Dimensions=@dims AND e.NodeID=r.RecordID
 WHERE r.RepositoryID=@repo AND r.RecordKind='node' AND r.SearchHash IS NOT NULL AND ` + openPredicate("r"),
			Params: map[string]any{"repo": repo, "gen": int64(out.Generation), "model": model, "dims": int64(dimensions), "version": int64(codesearch.DocumentVersion)},
		}, &searchable, &current)
		if err == nil {
			err = queryCounts(ctx, t, spanner.Statement{
				SQL:    "SELECT COUNT(*) FROM CGSearchEmbeddings WHERE RepositoryID=@repo AND Model=@model AND Dimensions=@dims",
				Params: map[string]any{"repo": repo, "model": model, "dims": int64(dimensions)},
			}, &stored)
		}
	}
	if err != nil {
		return RepositoryStats{}, err
	}
	out.Searchable, out.Embeddings.Current, out.Embeddings.Stored = uint64(searchable), uint64(current), uint64(stored)
	return out, nil
}

// countKinds adds up the records open at generation by record kind and kind,
// from the CGRecordsByKind index alone.
func countKinds(ctx context.Context, t reader, repo string, generation uint64, out *RepositoryStats) error {
	it := t.Query(ctx, spanner.Statement{
		SQL:    "SELECT RecordKind, Kind, COUNT(*) FROM CGRecords@{FORCE_INDEX=CGRecordsByKind} WHERE RepositoryID=@repo AND " + openPredicate("") + " GROUP BY RecordKind, Kind",
		Params: map[string]any{"repo": repo, "gen": int64(generation)},
	})
	defer it.Stop()
	for {
		row, err := nextRow(it)
		if err != nil || row == nil {
			return err
		}
		var recordKind, kind string
		var n int64
		if err = row.Columns(&recordKind, &kind, &n); err != nil {
			return err
		}
		switch graph.RecordKind(recordKind) {
		case graph.RecordNode:
			out.NodeKinds[kind] = uint64(n)
			out.Nodes += uint64(n)
		case graph.RecordEdge:
			out.EdgeKinds[kind] = uint64(n)
			out.Edges += uint64(n)
		}
	}
}

// queryCounts reads the one row of an aggregate query into dst.
func queryCounts(ctx context.Context, t reader, stmt spanner.Statement, dst ...any) error {
	it := t.Query(ctx, stmt)
	defer it.Stop()
	row, err := it.Next()
	if err != nil {
		return err
	}
	return row.Columns(dst...)
}
