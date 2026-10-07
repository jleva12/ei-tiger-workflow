package agentquery

import (
	"context"
	"fmt"
	"sort"
	"strings"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

const (
	// expansionSeeds is how many top hits lend their vocabulary to a query;
	// expansionTerms how many terms are added; minExpansionQuery the number
	// of query terms below which no expansion happens (short queries are
	// usually identifiers, which are already exact).
	expansionSeeds    = 5
	expansionTerms    = 4
	minExpansionQuery = 3
	rrfK              = 60.0
)

// SearchRequest is a search as an agent asks it. Expand turns on query
// expansion with the repository's own vocabulary: when the first pass finds
// no exact match, the identifier fragments most common among its top hits
// are added to a lexical second pass and the two rankings are fused.
type SearchRequest struct {
	RepositoryIDs []string
	Text          string
	Mode          spannerstore.SearchMode
	Limit         int
	Kinds         []string
	PathPrefix    string
	NearPath      string
	EnhanceQuery  bool
	Expand        bool
}

// SearchResult is the ranked hits with what the query was understood as,
// whether the semantic branch ran, and the terms expansion added.
type SearchResult struct {
	Hits     []spannerstore.SearchHit `json:"hits"`
	Query    codesearch.Query         `json:"query"`
	Mode     spannerstore.SearchMode  `json:"mode"`
	Semantic bool                     `json:"semantic"`
	Expanded []string                 `json:"expanded_terms,omitempty"`
}

// Search runs hybrid search with the embedder's vector when one is given,
// then optionally the expansion pass.
func Search(ctx context.Context, st Store, embedder codesearch.Provider, req SearchRequest) (SearchResult, error) {
	text := strings.TrimSpace(req.Text)
	if text == "" {
		return SearchResult{}, fmt.Errorf("%w: query is required", deployment.ErrInvalidRequest)
	}
	mode := req.Mode
	if mode == "" {
		mode = spannerstore.SearchHybrid
	}
	request := spannerstore.SearchRequest{RepositoryIDs: req.RepositoryIDs, Mode: mode, Text: text, Limit: req.Limit, Kinds: req.Kinds, PathPrefix: req.PathPrefix, NearPath: req.NearPath, EnhanceQuery: req.EnhanceQuery}
	result := SearchResult{Query: codesearch.ParseQuery(text), Mode: mode}
	if embedder != nil && mode != spannerstore.SearchLexical {
		vector, err := codesearch.EmbedText(ctx, embedder, text)
		if err != nil {
			return SearchResult{}, err
		}
		request.Model, request.Vector = embedder.Model(), vector
		result.Semantic = true
	}
	hits, err := st.HybridSearch(ctx, request)
	if err != nil {
		return SearchResult{}, err
	}
	if hits == nil {
		hits = []spannerstore.SearchHit{}
	}
	result.Hits = hits
	if !req.Expand {
		return result, nil
	}
	terms := expansionTermsFor(result.Query, hits)
	if len(terms) == 0 {
		return result, nil
	}
	second := request
	second.Mode, second.Model, second.Vector = spannerstore.SearchLexical, "", nil
	second.Text = text + " " + strings.Join(terms, " ")
	more, err := st.HybridSearch(ctx, second)
	if err != nil {
		return SearchResult{}, err
	}
	result.Expanded = terms
	result.Hits = fuseHits(hits, more, request.Limit)
	return result, nil
}

// expansionTermsFor picks the identifier fragments most shared by the top
// hits' simple names that the query does not already contain. It returns nothing when a
// hit matched the query exactly or the query is too short to be a question.
func expansionTermsFor(q codesearch.Query, hits []spannerstore.SearchHit) []string {
	queryTerms := codesearch.Terms(q.Text)
	if len(queryTerms) < minExpansionQuery {
		return nil
	}
	present := map[string]bool{}
	for _, t := range queryTerms {
		present[t] = true
	}
	counts := map[string]int{}
	var order []string
	for i, h := range hits {
		if h.ExactMatch {
			return nil
		}
		if i == expansionSeeds {
			break
		}
		n := h.Node.Fact.Node
		if n == nil {
			continue
		}
		// The simple name and the owning type's simple name lend fragments;
		// qualified names would add package segments, which name no concept.
		owner := graph.Text(n.Properties, "owner_key")
		if i := strings.LastIndexAny(owner, ".$"); i >= 0 {
			owner = owner[i+1:]
		}
		seen := map[string]bool{}
		for _, term := range strings.Fields(strings.ToLower(codesearch.IdentifierTerms(n.Name, owner))) {
			if len(term) < 3 || present[term] || seen[term] || expansionStopwords[term] || strings.ContainsAny(term, ".()<>[]$,") {
				continue
			}
			seen[term] = true
			if counts[term] == 0 {
				order = append(order, term)
			}
			counts[term]++
		}
	}
	sort.SliceStable(order, func(i, j int) bool { return counts[order[i]] > counts[order[j]] })
	var out []string
	for _, term := range order {
		if counts[term] < 2 {
			break
		}
		out = append(out, term)
		if len(out) == expansionTerms {
			break
		}
	}
	return out
}

// expansionStopwords are fragments that name no concept of a repository.
var expansionStopwords = map[string]bool{"java": true, "lang": true, "util": true, "com": true, "org": true, "net": true, "string": true, "object": true, "int": true, "long": true, "boolean": true, "void": true, "list": true, "map": true, "get": true, "set": true, "new": true, "the": true, "and": true, "for": true, "with": true, "impl": true, "test": true, "main": true, "src": true}

// fuseHits merges two rankings by reciprocal rank fusion and returns at
// most limit hits, first-pass order winning ties.
func fuseHits(first, second []spannerstore.SearchHit, limit int) []spannerstore.SearchHit {
	if limit <= 0 {
		limit = 10
	}
	key := func(h spannerstore.SearchHit) string {
		if h.Node.Fact.Node == nil {
			return h.RepositoryID
		}
		return h.RepositoryID + "\x00" + h.Node.Fact.Node.ID
	}
	score := map[string]float64{}
	hit := map[string]spannerstore.SearchHit{}
	var order []string
	for _, list := range [][]spannerstore.SearchHit{first, second} {
		for rank, h := range list {
			k := key(h)
			if _, ok := hit[k]; !ok {
				hit[k] = h
				order = append(order, k)
			}
			score[k] += 1 / (rrfK + float64(rank+1))
		}
	}
	sort.SliceStable(order, func(i, j int) bool { return score[order[i]] > score[order[j]] })
	out := make([]spannerstore.SearchHit, 0, min(limit, len(order)))
	for _, k := range order {
		out = append(out, hit[k])
		if len(out) == limit {
			break
		}
	}
	return out
}
