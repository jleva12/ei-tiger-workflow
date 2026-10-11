package spannerstore

import (
	"strings"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/graph"
)

// searchDocumentRow is the CGSearchDocuments row of one node: the retrieval
// document lexical search matches, the identifier forms it matches partial
// names against, and the columns exact-name lookups and filters use. The
// readers (the Python package forge_codegraph_query) verify each row against
// the open record's search hash at query time, so rows need no generation
// versioning: a row written while a generation loads simply does not match
// live until the flip.
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
