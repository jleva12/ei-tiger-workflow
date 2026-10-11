package spannerstore

import (
	"context"
	"encoding/json"
	"fmt"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

// The search tables are written here and read elsewhere. The loader writes a
// node's retrieval document to CGSearchDocuments (documents.go); the
// embedding pass pages the documents whose embedding for a model is missing
// or stale and stores their vectors in CGSearchEmbeddings, the column the
// CGSearchEmbeddingsVector index serves. Search itself, lexical and vector,
// lives in the Python package forge_codegraph_query, which hashes documents
// exactly as codesearch.FromNode does and drops rows whose hash no longer
// matches the open record.

const (
	searchPageSize   = 256 // a page fills the embedding requests in flight
	maxEmbeddingRows = 64
)

// validSearch checks an embedding request: the dimension must be the one
// the database was provisioned with, because the embedding column and its
// vector index have a fixed length.
func (s *Store) validSearch(repo, model string, dimensions int) error {
	if !validRepositoryID(repo) || model == "" || len(model) > 256 {
		return fmt.Errorf("%w: repository and model", deployment.ErrInvalidRequest)
	}
	if dimensions != s.vectorLength {
		return fmt.Errorf("%w: %d-dimensional embeddings, database vector length is %d", deployment.ErrInvalidRequest, dimensions, s.vectorLength)
	}
	return nil
}

// float32Vector narrows a vector to the embedding column's element type.
func float32Vector(v []float64) []float32 {
	out := make([]float32, len(v))
	for i, x := range v {
		out[i] = float32(x)
	}
	return out
}

var _ codesearch.IndexStore = (*Store)(nil)

// SearchDocuments pages the nodes open at live that carry a search hash and
// whose embedding for (model, dimensions) is missing or stale.
func (s *Store) SearchDocuments(ctx context.Context, repo, model string, dimensions int, cursor string) (codesearch.DocumentPage, error) {
	if err := s.validSearch(repo, model, dimensions); err != nil {
		return codesearch.DocumentPage{}, err
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	live, err := liveGeneration(ctx, t, repo)
	if err != nil {
		return codesearch.DocumentPage{}, err
	}
	out := codesearch.DocumentPage{Generation: live}
	scope := s.cursors.scope("search-documents", repo, model, dimensions, codesearch.DocumentVersion)
	after, err := s.cursors.decode(scope, live, cursor)
	if err != nil {
		return out, err
	}
	it := t.Query(ctx, spanner.Statement{
		SQL: `SELECT r.RecordID, r.Payload FROM CGRecords r
 LEFT JOIN CGSearchEmbeddings e ON e.RepositoryID=r.RepositoryID AND e.Model=@model AND e.Dimensions=@dims AND e.NodeID=r.RecordID
 WHERE r.RepositoryID=@repo AND r.RecordKind='node' AND r.SearchHash IS NOT NULL AND r.RecordID>@after AND ` + openPredicate("r") + `
 AND (e.NodeID IS NULL OR e.DocumentHash!=r.SearchHash OR e.DocumentVersion!=@version)
 ORDER BY r.RecordID LIMIT @limit`,
		Params: map[string]any{"repo": repo, "model": model, "dims": int64(dimensions), "after": after, "gen": int64(live), "version": int64(codesearch.DocumentVersion), "limit": int64(searchPageSize + 1)},
	})
	defer it.Stop()
	for {
		row, err := nextRow(it)
		if err != nil {
			return out, err
		}
		if row == nil {
			return out, nil
		}
		var id string
		var b []byte
		if err = row.Columns(&id, &b); err != nil {
			return out, err
		}
		if len(out.Documents) == searchPageSize {
			out.NextCursor = s.cursors.encode(scope, live, out.Documents[searchPageSize-1].NodeID)
			return out, nil
		}
		var f graph.Fact
		if json.Unmarshal(b, &f) != nil || f.Node == nil {
			return out, fmt.Errorf("%w: node %s payload", graph.ErrIntegrity, id)
		}
		d, ok := codesearch.FromNode(*f.Node)
		if !ok {
			return out, fmt.Errorf("%w: node %s carries a search hash but is not a document", graph.ErrIntegrity, id)
		}
		out.Documents = append(out.Documents, d)
	}
}

// PendingDocuments counts the documents SearchDocuments would page: the
// nodes open at live whose embedding for (model, dimensions) is missing or
// stale. It sizes an embedding pass for progress reporting.
func (s *Store) PendingDocuments(ctx context.Context, repo, model string, dimensions int) (uint64, error) {
	if err := s.validSearch(repo, model, dimensions); err != nil {
		return 0, err
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	live, err := liveGeneration(ctx, t, repo)
	if err != nil {
		return 0, err
	}
	it := t.Query(ctx, spanner.Statement{
		SQL: `SELECT COUNT(*) FROM CGRecords r
 LEFT JOIN CGSearchEmbeddings e ON e.RepositoryID=r.RepositoryID AND e.Model=@model AND e.Dimensions=@dims AND e.NodeID=r.RecordID
 WHERE r.RepositoryID=@repo AND r.RecordKind='node' AND r.SearchHash IS NOT NULL AND ` + openPredicate("r") + `
 AND (e.NodeID IS NULL OR e.DocumentHash!=r.SearchHash OR e.DocumentVersion!=@version)`,
		Params: map[string]any{"repo": repo, "model": model, "dims": int64(dimensions), "gen": int64(live), "version": int64(codesearch.DocumentVersion)},
	})
	defer it.Stop()
	row, err := nextRow(it)
	if err != nil || row == nil {
		return 0, err
	}
	var n int64
	if err = row.Columns(&n); err != nil {
		return 0, err
	}
	return uint64(n), nil
}

// PutEmbeddings stores vectors for documents of the live generation. It is
// rejected when live moved since the documents were read, and each document
// must still match its node's current text.
func (s *Store) PutEmbeddings(ctx context.Context, repo, model string, generation uint64, rows []codesearch.Embedding) error {
	if len(rows) == 0 || len(rows) > maxEmbeddingRows || generation == 0 {
		return fmt.Errorf("%w: 1..%d embeddings and a generation", deployment.ErrInvalidRequest, maxEmbeddingRows)
	}
	dims := len(rows[0].Vector)
	if err := s.validSearch(repo, model, dims); err != nil {
		return err
	}
	ids := make([]string, 0, len(rows))
	seen := map[string]bool{}
	for _, row := range rows {
		if err := codesearch.ValidateVector(row.Vector, dims); err != nil {
			return err
		}
		if !graph.ValidID(row.Document.NodeID) || seen[row.Document.NodeID] {
			return fmt.Errorf("%w: duplicate or invalid document node", deployment.ErrInvalidRequest)
		}
		seen[row.Document.NodeID] = true
		ids = append(ids, row.Document.NodeID)
	}
	return s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		live, err := liveGeneration(ctx, t, repo)
		if err != nil {
			return err
		}
		if live != generation {
			return fmt.Errorf("%w: live generation %d, embeddings for %d", deployment.ErrStaleDeployment, live, generation)
		}
		nodes, err := readOpenNodes(ctx, t, repo, ids, live, live)
		if err != nil {
			return err
		}
		ms := make([]*spanner.Mutation, 0, len(rows))
		for _, row := range rows {
			v, ok := nodes[row.Document.NodeID]
			if !ok {
				return fmt.Errorf("%w: node %s is not open at generation %d", deployment.ErrStaleDeployment, row.Document.NodeID, live)
			}
			d, ok := codesearch.FromNode(*v.Fact.Node)
			if !ok || d.Hash != row.Document.Hash || d.Version != row.Document.Version {
				return fmt.Errorf("%w: document for %s changed", deployment.ErrStaleDeployment, row.Document.NodeID)
			}
			ms = append(ms, spanner.InsertOrUpdate("CGSearchEmbeddings",
				[]string{"RepositoryID", "Model", "Dimensions", "NodeID", "DocumentHash", "DocumentVersion", "DocumentText", "Embedding", "UpdatedGeneration"},
				[]any{repo, model, int64(dims), d.NodeID, d.Hash, int64(d.Version), d.Text, float32Vector(row.Vector), int64(generation)}))
		}
		return t.BufferWrite(ms)
	})
}
