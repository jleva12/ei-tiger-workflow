package agentquery

import (
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

// Brief is the compact form of a node: enough to decide the next hop and to
// ask for more. Spans, hashes and properties come from get_node on request.
type Brief struct {
	ID            string `json:"id"`
	Kind          string `json:"kind"`
	QualifiedName string `json:"qualified_name,omitempty"`
	Name          string `json:"name,omitempty"` // only when it is not the start of the qualified name
	File          string `json:"file,omitempty"`
	Line          uint32 `json:"line,omitempty"`
}

// Briefly reduces a node to its Brief.
func Briefly(n graph.Node) Brief {
	b := Brief{ID: n.ID, Kind: n.Kind, QualifiedName: n.QualifiedName, File: graph.Text(n.Properties, "file_path")}
	if n.QualifiedName == "" || (n.Name != "" && !hasPrefixWord(n.QualifiedName, n.Name)) {
		b.Name = n.Name
	}
	if n.Source != nil {
		b.Line = n.Source.Span.Start.Line
	}
	return b
}

// hasPrefixWord reports whether qualified starts with name as a whole token,
// as method qualified names do: placeOrder(shop.Cart) starts with placeOrder.
func hasPrefixWord(qualified, name string) bool {
	if len(qualified) < len(name) || qualified[:len(name)] != name {
		return false
	}
	if len(qualified) == len(name) {
		return true
	}
	switch qualified[len(name)] {
	case '(', '.', ':', '<', '[':
		return true
	}
	return false
}

// CompactNeighbor is one edge of a node in compact form: the far node, the
// edge kind, which way it points and the line of the occurrence that proves
// it, when the edge has one.
type CompactNeighbor struct {
	Brief
	Via       string          `json:"via"`
	Direction graph.Direction `json:"direction"` // "in": the far node points at the node; "out": the node points at it
	EdgeID    string          `json:"edge_id"`
	AtLine    uint32          `json:"at_line,omitempty"`
}

// CompactNeighborPage is NeighborPage without records.
type CompactNeighborPage struct {
	Generation uint64            `json:"generation"`
	Neighbors  []CompactNeighbor `json:"neighbors"`
	NextCursor string            `json:"next_cursor,omitempty"`
}

// CompactNeighbors projects a neighbor page around nodeID. Edges whose far
// node is not open at the generation are kept with only the edge and id.
func CompactNeighbors(page graph.NeighborPage, nodeID string) CompactNeighborPage {
	out := CompactNeighborPage{Generation: page.Generation, Neighbors: make([]CompactNeighbor, 0, len(page.Neighbors)), NextCursor: page.NextCursor}
	for _, nb := range page.Neighbors {
		e := nb.Edge.Fact.Edge
		if e == nil {
			continue
		}
		c := CompactNeighbor{Via: e.Kind, EdgeID: e.ID, Direction: graph.Outgoing}
		far := e.TargetID
		if e.TargetID == nodeID && e.SourceID != nodeID {
			c.Direction, far = graph.Incoming, e.SourceID
		}
		if nb.Node != nil && nb.Node.Fact.Node != nil {
			c.Brief = Briefly(*nb.Node.Fact.Node)
		} else {
			c.Brief = Brief{ID: far}
		}
		if e.Source != nil {
			c.AtLine = e.Source.Span.Start.Line
		}
		out.Neighbors = append(out.Neighbors, c)
	}
	return out
}

// CompactHit is a search hit without the record: the node in brief, the
// scores, the exact-match flag, the matched terms and the snippet.
type CompactHit struct {
	Brief
	RepositoryID string   `json:"repository_id"`
	Score        float64  `json:"score"`
	ExactMatch   bool     `json:"exact_match,omitempty"`
	NameMatch    bool     `json:"name_match,omitempty"`
	Callers      int64    `json:"callers"`
	Callees      int64    `json:"callees"`
	Matched      []string `json:"matched,omitempty"`
	Snippet      string   `json:"snippet,omitempty"`
}

// CompactHits projects search hits.
func CompactHits(hits []spannerstore.SearchHit) []CompactHit {
	out := make([]CompactHit, 0, len(hits))
	for _, h := range hits {
		c := CompactHit{RepositoryID: h.RepositoryID, Score: h.Score, ExactMatch: h.ExactMatch, NameMatch: h.NameMatch, Callers: h.Callers, Callees: h.Callees, Matched: h.Matched, Snippet: h.Snippet}
		if h.Node.Fact.Node != nil {
			c.Brief = Briefly(*h.Node.Fact.Node)
		}
		out = append(out, c)
	}
	return out
}

// CompactSeed and CompactRelated are the compact forms of Seed and Related.
type CompactSeed struct {
	Brief
	RepositoryID string  `json:"repository_id"`
	Score        float64 `json:"score"`
	ExactMatch   bool    `json:"exact_match,omitempty"`
	Expanded     bool    `json:"expanded"`
	Snippet      string  `json:"snippet,omitempty"`
}

type CompactRelated struct {
	Brief
	SeedID    string          `json:"seed_id"`
	Via       string          `json:"via"`
	Direction graph.Direction `json:"direction"`
}

// CompactExploreResult is ExploreResult without records, signatures,
// documentation or byte spans: briefs, scores and edges.
type CompactExploreResult struct {
	Question  string           `json:"question"`
	Semantic  bool             `json:"semantic"`
	Expanded  []string         `json:"expanded_terms,omitempty"`
	Seeds     []CompactSeed    `json:"seeds"`
	Related   []CompactRelated `json:"related"`
	Links     []Link           `json:"links,omitempty"`
	Files     []FileGroup      `json:"files"`
	Truncated bool             `json:"truncated"`
}

// CompactExplore projects an explore result.
func CompactExplore(r ExploreResult) CompactExploreResult {
	out := CompactExploreResult{Question: r.Question, Semantic: r.Semantic, Expanded: r.Expanded, Seeds: make([]CompactSeed, 0, len(r.Seeds)), Related: make([]CompactRelated, 0, len(r.Related)), Links: r.Links, Files: r.Files, Truncated: r.Truncated}
	for _, s := range r.Seeds {
		out.Seeds = append(out.Seeds, CompactSeed{Brief: briefOfSummary(s.Summary), RepositoryID: s.RepositoryID, Score: s.Score, ExactMatch: s.ExactMatch, Expanded: s.Expanded, Snippet: s.Snippet})
	}
	for _, rel := range r.Related {
		out.Related = append(out.Related, CompactRelated{Brief: briefOfSummary(rel.Summary), SeedID: rel.SeedID, Via: rel.Via, Direction: rel.Direction})
	}
	return out
}

func briefOfSummary(s Summary) Brief {
	b := Brief{ID: s.ID, Kind: s.Kind, QualifiedName: s.QualifiedName, File: s.FilePath, Line: s.StartLine}
	if s.QualifiedName == "" || (s.Name != "" && !hasPrefixWord(s.QualifiedName, s.Name)) {
		b.Name = s.Name
	}
	return b
}
