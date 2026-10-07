package agentquery

import (
	"context"
	"fmt"
	"sort"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

const (
	defaultSeeds     = 8
	maxSeeds         = 25
	defaultExpand    = 5
	maxExpand        = 10
	defaultNeighbors = 20
	maxNeighbors     = 100
	maxQuestionBytes = 8000
	docSummaryBytes  = 400
)

// ExploreRequest is a natural-language question about one or more
// repositories. Limit bounds the seeds search returns, Expand how many of
// them are expanded into their graph neighborhood, and Neighbors how many
// neighbors each expansion may return.
type ExploreRequest struct {
	RepositoryIDs []string
	Question      string
	Limit         int
	Expand        int
	Neighbors     int
	Kinds         []string
	PathPrefix    string
	NearPath      string
	Mode          spannerstore.SearchMode
	EnhanceQuery  bool
	// ExpandQuery adds the repository's own vocabulary to the question when
	// the first search pass finds no exact match; see Search.
	ExpandQuery bool
}

// Seed is a search hit chosen as an anchor for the question.
type Seed struct {
	Summary
	RepositoryID  string   `json:"repository_id"`
	Score         float64  `json:"score"`
	Lexical       float64  `json:"lexical,omitempty"`
	Vector        float64  `json:"vector,omitempty"`
	ExactMatch    bool     `json:"exact_match,omitempty"`
	NameMatch     bool     `json:"name_match,omitempty"`
	Callers       int64    `json:"callers"`
	Callees       int64    `json:"callees"`
	Matched       []string `json:"matched,omitempty"`
	Snippet       string   `json:"snippet,omitempty"`
	Documentation string   `json:"documentation,omitempty"`
	Expanded      bool     `json:"expanded"`
}

// Related is a node one semantic edge away from an expanded seed.
type Related struct {
	Summary
	RepositoryID string          `json:"repository_id"`
	SeedID       string          `json:"seed_id"`
	EdgeID       string          `json:"edge_id"`
	Via          string          `json:"via"`
	Direction    graph.Direction `json:"direction"` // "in": this node points at the seed; "out": the seed points at it
}

// Link is a semantic edge between two seeds.
type Link struct {
	RepositoryID string `json:"repository_id"`
	EdgeID       string `json:"edge_id"`
	Via          string `json:"via"`
	SourceID     string `json:"source_id"`
	TargetID     string `json:"target_id"`
}

// FileGroup counts what the answer touches per file.
type FileGroup struct {
	RepositoryID string `json:"repository_id"`
	FilePath     string `json:"file_path"`
	Language     string `json:"language,omitempty"`
	Seeds        int    `json:"seeds"`
	Related      int    `json:"related"`
}

// ExploreResult is a grounded context pack: what search understood, the
// seeds it found, the seeds' immediate semantic neighborhood, the edges
// among seeds, and the files involved. Every node carries the span and
// content hash an agent needs to read exact source next.
type ExploreResult struct {
	Question  string           `json:"question"`
	Query     codesearch.Query `json:"query"`
	Semantic  bool             `json:"semantic"`
	Expanded  []string         `json:"expanded_terms,omitempty"`
	Seeds     []Seed           `json:"seeds"`
	Related   []Related        `json:"related"`
	Links     []Link           `json:"links,omitempty"`
	Files     []FileGroup      `json:"files"`
	Truncated bool             `json:"truncated"` // an expanded seed had more neighbors than returned
}

// Explore answers a question with evidence rather than prose: hybrid search
// finds seeds, the strongest seeds are expanded one semantic hop in both
// directions, and everything is summarized with spans so the agent can read
// source or walk further. It never invents relationships: every related
// node arrives through a stored edge.
func Explore(ctx context.Context, st Store, embedder codesearch.Provider, req ExploreRequest) (ExploreResult, error) {
	if len(req.RepositoryIDs) == 0 || len(req.Question) == 0 || len(req.Question) > maxQuestionBytes ||
		req.Limit < 0 || req.Limit > maxSeeds || req.Expand < 0 || req.Expand > maxExpand || req.Neighbors < 0 || req.Neighbors > maxNeighbors {
		return ExploreResult{}, fmt.Errorf("%w: explore needs repositories, a question of at most %d bytes, limit 0-%d, expand 0-%d and neighbors 0-%d", deployment.ErrInvalidRequest, maxQuestionBytes, maxSeeds, maxExpand, maxNeighbors)
	}
	if req.Limit == 0 {
		req.Limit = defaultSeeds
	}
	if req.Expand == 0 {
		req.Expand = defaultExpand
	}
	if req.Neighbors == 0 {
		req.Neighbors = defaultNeighbors
	}
	if req.Mode == "" {
		req.Mode = spannerstore.SearchHybrid
	}
	parsed := codesearch.ParseQuery(req.Question)
	found, err := Search(ctx, st, embedder, SearchRequest{RepositoryIDs: req.RepositoryIDs, Text: parsed.Text, Mode: req.Mode, Limit: req.Limit, Kinds: req.Kinds, PathPrefix: req.PathPrefix, NearPath: req.NearPath, EnhanceQuery: req.EnhanceQuery, Expand: req.ExpandQuery})
	if err != nil {
		return ExploreResult{}, err
	}
	result := ExploreResult{Question: parsed.Text, Query: parsed, Semantic: found.Semantic, Expanded: found.Expanded, Seeds: []Seed{}, Related: []Related{}, Files: []FileGroup{}}
	hits := found.Hits
	seedIndex := map[string]int{} // node ID to position in result.Seeds
	for _, h := range hits {
		n := h.Node.Fact.Node
		seed := Seed{Summary: Summarize(*n), RepositoryID: h.RepositoryID, Score: h.Score, Lexical: h.Lexical, Vector: h.Vector, ExactMatch: h.ExactMatch, NameMatch: h.NameMatch,
			Callers: h.Callers, Callees: h.Callees, Matched: h.Matched, Snippet: h.Snippet, Documentation: truncate(graph.Text(n.Properties, "docstring"), docSummaryBytes)}
		seedIndex[seedKey(h.RepositoryID, n.ID)] = len(result.Seeds)
		result.Seeds = append(result.Seeds, seed)
	}

	// Expand exact matches first, then the best fused scores; hits arrive in
	// score order with exact matches already on top, so the order is kept.
	expanded := 0
	seenRelated := map[string]bool{}
	seenLinks := map[string]bool{}
	for i := range result.Seeds {
		if expanded == req.Expand {
			break
		}
		seed := &result.Seeds[i]
		page, err := st.Neighbors(ctx, graph.NeighborQuery{RepositoryID: seed.RepositoryID, NodeID: seed.ID, Direction: graph.Both, EdgeKinds: spannerstore.SemanticEdgeKinds(), Limit: req.Neighbors})
		if err != nil {
			return ExploreResult{}, err
		}
		seed.Expanded = true
		expanded++
		if page.NextCursor != "" {
			result.Truncated = true
		}
		for _, nb := range page.Neighbors {
			e := nb.Edge.Fact.Edge
			if nb.Node == nil || nb.Node.Fact.Node == nil || e == nil {
				continue
			}
			far := nb.Node.Fact.Node
			if _, isSeed := seedIndex[seedKey(seed.RepositoryID, far.ID)]; isSeed {
				if key := seedKey(seed.RepositoryID, e.ID); !seenLinks[key] {
					seenLinks[key] = true
					result.Links = append(result.Links, Link{RepositoryID: seed.RepositoryID, EdgeID: e.ID, Via: e.Kind, SourceID: e.SourceID, TargetID: e.TargetID})
				}
				continue
			}
			if key := seedKey(seed.RepositoryID, far.ID); !seenRelated[key] {
				seenRelated[key] = true
				direction := graph.Outgoing
				if e.TargetID == seed.ID {
					direction = graph.Incoming
				}
				result.Related = append(result.Related, Related{Summary: Summarize(*far), RepositoryID: seed.RepositoryID, SeedID: seed.ID, EdgeID: e.ID, Via: e.Kind, Direction: direction})
			}
		}
	}
	result.Files = groupFiles(result.Seeds, result.Related)
	return result, nil
}

func seedKey(repo, id string) string { return repo + "\x00" + id }

// groupFiles counts seeds and related nodes per file, most relevant first.
func groupFiles(seeds []Seed, related []Related) []FileGroup {
	groups := map[string]*FileGroup{}
	order := []string{}
	get := func(repo, path, language string) *FileGroup {
		key := seedKey(repo, path)
		g := groups[key]
		if g == nil {
			g = &FileGroup{RepositoryID: repo, FilePath: path, Language: language}
			groups[key] = g
			order = append(order, key)
		}
		return g
	}
	for _, s := range seeds {
		if s.FilePath != "" {
			get(s.RepositoryID, s.FilePath, s.Language).Seeds++
		}
	}
	for _, r := range related {
		if r.FilePath != "" {
			get(r.RepositoryID, r.FilePath, r.Language).Related++
		}
	}
	out := make([]FileGroup, 0, len(order))
	for _, key := range order {
		out = append(out, *groups[key])
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Seeds != out[j].Seeds {
			return out[i].Seeds > out[j].Seeds
		}
		if out[i].Related != out[j].Related {
			return out[i].Related > out[j].Related
		}
		if out[i].RepositoryID != out[j].RepositoryID {
			return out[i].RepositoryID < out[j].RepositoryID
		}
		return out[i].FilePath < out[j].FilePath
	})
	return out
}
