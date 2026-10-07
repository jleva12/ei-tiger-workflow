package spannerstore

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

// searchDocumentRow is the CGSearchDocuments row of one node: the retrieval
// document the lexical branch searches, the identifier forms it matches
// partial names against, and the columns the exact tier and filters use.
// Rows are derived from records and verified against the open record's
// search hash at query time, so they need no generation versioning: a row
// written while a generation loads simply does not match live until the flip.
type searchDocumentRow struct {
	Repository    string
	NodeID        string
	Generation    uint64
	Kind          string
	Name          string
	QualifiedName string
	FilePath      string
	Document      codesearch.Document
	Identifiers   string
}

var searchDocumentColumns = []string{"RepositoryID", "NodeID", "Generation", "Kind", "Name", "NameLower", "QualifiedName", "FilePath", "DocumentHash", "DocumentVersion", "DocumentText", "Identifiers"}

func newSearchDocumentRow(repo string, generation uint64, n graph.Node) (searchDocumentRow, bool) {
	d, ok := codesearch.FromNode(n)
	if !ok {
		return searchDocumentRow{}, false
	}
	return searchDocumentRow{Repository: repo, NodeID: n.ID, Generation: generation, Kind: n.Kind, Name: n.Name, QualifiedName: n.QualifiedName,
		FilePath: graph.Text(n.Properties, "file_path"), Document: d, Identifiers: codesearch.IdentifierTerms(n.Name, n.QualifiedName)}, true
}

func (r searchDocumentRow) mutation() *spanner.Mutation {
	return spanner.InsertOrUpdate("CGSearchDocuments", searchDocumentColumns, []any{r.Repository, r.NodeID, int64(r.Generation), r.Kind, r.Name, strings.ToLower(r.Name), r.QualifiedName, r.FilePath, r.Document.Hash, int64(r.Document.Version), r.Document.Text, r.Identifiers})
}

// bytes estimates the row's indexed payload for commit sizing.
func (r searchDocumentRow) bytes() int { return len(r.Document.Text) + len(r.Identifiers) + 256 }

// BackfillSearchDocuments writes the search document of every node open at
// live whose row is missing or no longer matches the node's text. It exists
// for graphs loaded before documents were written by the loader. It is
// idempotent and needs no lease: rows are derived from records and checked
// against them at query time.
func (s *Store) BackfillSearchDocuments(ctx context.Context, repo string) (written uint64, err error) {
	if !validRepositoryID(repo) {
		return 0, fmt.Errorf("%w: repository id", graph.ErrInvalid)
	}
	after := ""
	for {
		t := s.client.ReadOnlyTransaction()
		live, err := liveGeneration(ctx, t, repo)
		if err != nil {
			t.Close()
			return written, err
		}
		it := t.Query(ctx, spanner.Statement{
			SQL: `SELECT r.RecordID, r.Payload FROM CGRecords r
 LEFT JOIN CGSearchDocuments d ON d.RepositoryID=r.RepositoryID AND d.NodeID=r.RecordID AND d.DocumentHash=r.SearchHash AND d.DocumentVersion=@version
 WHERE r.RepositoryID=@repo AND r.RecordKind='node' AND r.SearchHash IS NOT NULL AND r.RecordID>@after AND ` + openPredicate("r") + `
 AND d.NodeID IS NULL ORDER BY r.RecordID LIMIT @limit`,
			Params: map[string]any{"repo": repo, "after": after, "gen": int64(live), "version": int64(codesearch.DocumentVersion), "limit": int64(searchPageSize)},
		})
		var ms []*spanner.Mutation
		rows := 0
		for {
			row, err := nextRow(it)
			if err != nil {
				it.Stop()
				t.Close()
				return written, err
			}
			if row == nil {
				break
			}
			var id string
			var b []byte
			if err = row.Columns(&id, &b); err != nil {
				it.Stop()
				t.Close()
				return written, err
			}
			rows++
			after = id
			var f graph.Fact
			if json.Unmarshal(b, &f) != nil || f.Node == nil {
				it.Stop()
				t.Close()
				return written, fmt.Errorf("%w: node %s payload", graph.ErrIntegrity, id)
			}
			if r, ok := newSearchDocumentRow(repo, live, *f.Node); ok {
				ms = append(ms, r.mutation())
			}
		}
		it.Stop()
		t.Close()
		if err = s.apply(ctx, ms); err != nil {
			return written, err
		}
		written += uint64(len(ms))
		if rows < searchPageSize {
			return written, nil
		}
	}
}

// simpleName is the last dotted segment of a qualified name before any
// parameter list: "com.acme.Foo" and "bar(com.acme.Foo)" give Foo and bar.
func simpleName(qualified string) string {
	if i := strings.IndexByte(qualified, '('); i >= 0 {
		qualified = qualified[:i]
	}
	if i := strings.LastIndexByte(qualified, '.'); i >= 0 {
		qualified = qualified[i+1:]
	}
	return qualified
}

// FindNodes resolves a simple name, or a qualified name, to the nodes open at
// generation (zero means live) that carry it, through the search documents'
// name index. Only document-bearing kinds are findable: types, callables,
// fields, enum constants, initializers and chunks. Exactly one of name and
// qualifiedName is required; kinds narrows the result.
func (s *Store) FindNodes(ctx context.Context, repo, name, qualifiedName string, kinds []string, generation uint64, limit int) ([]graph.Version, error) {
	if !validRepositoryID(repo) || (name == "") == (qualifiedName == "") || len(name) > 1024 || len(qualifiedName) > 4096 || !validKinds(kinds) {
		return nil, fmt.Errorf("%w: repository, one of name or qualified name, and kinds", graph.ErrInvalid)
	}
	limit = s.pageLimit(limit)
	needle := strings.ToLower(name)
	if qualifiedName != "" {
		needle = strings.ToLower(simpleName(qualifiedName))
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen, live, err := s.generationFor(ctx, t, repo, generation)
	if err != nil {
		return nil, err
	}
	sql := "SELECT NodeID, QualifiedName FROM CGSearchDocuments@{FORCE_INDEX=CGSearchDocumentsByName} WHERE RepositoryID=@repo AND NameLower=@needle"
	params := map[string]any{"repo": repo, "needle": needle, "limit": int64(limit * candidateFactor)}
	if qualifiedName != "" {
		// The index stores the qualified name, so the exact match is applied
		// in the query rather than after the candidate window: a repository
		// can hold hundreds of same-named methods (every _run_async_impl
		// override), and the one asked for must not depend on its id sorting
		// into the first few candidates.
		sql += " AND QualifiedName=@qualified"
		params["qualified"] = qualifiedName
	}
	if len(kinds) > 0 {
		sql += " AND Kind IN UNNEST(@kinds)"
		params["kinds"] = kinds
	}
	it := t.Query(ctx, spanner.Statement{SQL: sql + " ORDER BY NodeID LIMIT @limit", Params: params})
	var ids []string
	for {
		row, err := nextRow(it)
		if err != nil {
			it.Stop()
			return nil, err
		}
		if row == nil {
			break
		}
		var id, qualified string
		if err = row.Columns(&id, &qualified); err != nil {
			it.Stop()
			return nil, err
		}
		if qualifiedName == "" || qualified == qualifiedName {
			ids = append(ids, id)
		}
	}
	it.Stop()
	if len(ids) == 0 {
		return nil, nil
	}
	nodes, err := readOpenNodes(ctx, t, repo, ids, gen, live)
	if err != nil {
		return nil, err
	}
	out := make([]graph.Version, 0, len(nodes))
	for _, id := range ids {
		if v, ok := nodes[id]; ok {
			out = append(out, v)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Fact.Node.ID < out[j].Fact.Node.ID })
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

var _ = deployment.ErrNotFound
