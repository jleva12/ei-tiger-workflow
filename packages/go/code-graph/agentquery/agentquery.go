// Package agentquery composes the store's reads into the operations an
// agent-facing surface exposes: exploring a natural-language question into a
// grounded context pack, walking the impact of a change, and reading a
// node's exact source. The HTTP API and the MCP server both build on it, so
// a tool and an endpoint answer identically.
package agentquery

import (
	"context"

	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

// Store is the read surface these operations compose. *spannerstore.Store
// implements it; tests use an in-memory implementation.
type Store interface {
	HybridSearch(context.Context, spannerstore.SearchRequest) ([]spannerstore.SearchHit, error)
	FindNodes(ctx context.Context, repo, name, qualifiedName string, kinds []string, generation uint64, limit int) ([]graph.Version, error)
	GetNode(ctx context.Context, repo, id string, generation uint64) (graph.Version, error)
	GetEdge(ctx context.Context, repo, id string, generation uint64) (graph.Version, error)
	Neighbors(context.Context, graph.NeighborQuery) (graph.NeighborPage, error)
	// DependencyEdges reads the open edges touching any of the nodes from
	// the edge indexes, without payloads: one query per chunk of nodes.
	DependencyEdges(ctx context.Context, repo string, nodeIDs []string, direction graph.Direction, kinds []string, generation uint64, limit int) ([]spannerstore.EdgeRef, bool, error)
	// GetNodes batch-reads nodes; ids not open at the generation are absent.
	GetNodes(ctx context.Context, repo string, ids []string, generation uint64) (map[string]graph.Version, error)
	// Hubs ranks nodes by incoming edges of the kinds, semantic kinds when empty.
	Hubs(ctx context.Context, repo string, generation uint64, kinds []string, limit int) ([]spannerstore.Hub, error)
	// Changes pages through what a generation added, updated and retired.
	Changes(ctx context.Context, repo string, generation uint64, kind graph.RecordKind, limit int, cursor string) (spannerstore.ChangesPage, error)
	History(ctx context.Context, repo string, key graph.Key) ([]graph.Version, error)
	GetSource(ctx context.Context, repo, sha256sum string) ([]byte, error)
	State(ctx context.Context, repo string) (graph.RepositoryState, error)
	// LineagesByPath is every file lineage (one per module and source set)
	// that has had the path, at or before the generation.
	LineagesByPath(ctx context.Context, repo, path string, generation uint64) ([]string, error)
	// RecordsByLineage is every node and edge of one file lineage open at
	// the generation.
	RecordsByLineage(ctx context.Context, repo, lineage string, generation uint64) ([]graph.Version, error)
	// CrossLinks returns the cross-repository links touching a repository's
	// nodes; a store without any returns none.
	CrossLinks(ctx context.Context, q graph.CrossLinkQuery) ([]graph.CrossLink, error)
}

// Summary is the part of a node an agent needs to decide whether to look
// closer: identity, location and signature, never the body.
type Summary struct {
	ID            string `json:"id"`
	Kind          string `json:"kind"`
	Name          string `json:"name,omitempty"`
	QualifiedName string `json:"qualified_name,omitempty"`
	FilePath      string `json:"file_path,omitempty"`
	Language      string `json:"language,omitempty"`
	Signature     string `json:"signature,omitempty"`
	StartLine     uint32 `json:"start_line,omitempty"`
	EndLine       uint32 `json:"end_line,omitempty"`
	ByteStart     uint64 `json:"byte_start,omitempty"`
	ByteEnd       uint64 `json:"byte_end,omitempty"`
	ContentSHA256 string `json:"content_sha256,omitempty"`
}

// Summarize reduces a node to its Summary.
func Summarize(n graph.Node) Summary {
	s := Summary{ID: n.ID, Kind: n.Kind, Name: n.Name, QualifiedName: n.QualifiedName,
		FilePath: graph.Text(n.Properties, "file_path"), Language: graph.Text(n.Properties, "language"), Signature: graph.Text(n.Properties, "signature")}
	if n.Source != nil {
		s.StartLine, s.EndLine = n.Source.Span.Start.Line, n.Source.Span.End.Line
		s.ByteStart, s.ByteEnd = n.Source.Span.Start.ByteOffset, n.Source.Span.End.ByteOffset
		s.ContentSHA256 = n.Source.ContentSHA256
	}
	return s
}

// truncate bounds text for a summary field on a rune boundary.
func truncate(text string, max int) string {
	if len(text) <= max {
		return text
	}
	cut := max
	for cut > 0 && cut < len(text) && text[cut]&0xC0 == 0x80 {
		cut--
	}
	return text[:cut] + "…"
}
