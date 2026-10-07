package workerapp

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync"
	"unicode/utf8"

	"ei-aitiger-codegraph/agentquery"
	"ei-aitiger-codegraph/pkg/codesearch"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

// The search API finds the code that best answers a question across a set
// of repositories' live graphs: the Forge admin API's and the workflow
// worker's knowledge base tool searches a graph knowledge base's
// repositories with it. Search is hybrid, the search index's lexical
// ranking fused with the worker's embeddings when it has an embedding model,
// expanded with the repositories' own vocabulary when nothing matches
// exactly; the caller decides which repositories a reader may search.
//
//	POST /v1/search {"repository_ids", "query", "limit"?, "source"?}
//	    the best declarations, each with where it is and, with source, its code
type codeSearcher interface {
	Search(context.Context, agentquery.SearchRequest) (agentquery.SearchResult, error)
	Source(context.Context, agentquery.SourceRequest) (agentquery.SourceResult, error)
}

// storeSearch searches the Spanner store with the worker's embedder, if any.
type storeSearch struct {
	store    *spannerstore.Store
	embedder codesearch.Provider
	enhance  bool
}

func (s storeSearch) Search(ctx context.Context, q agentquery.SearchRequest) (agentquery.SearchResult, error) {
	q.EnhanceQuery = s.enhance
	return agentquery.Search(ctx, s.store, s.embedder, q)
}

func (s storeSearch) Source(ctx context.Context, q agentquery.SourceRequest) (agentquery.SourceResult, error) {
	return agentquery.NodeSource(ctx, s.store, q)
}

const (
	maxSearchRepositories = 50
	maxSearchLimit        = 25
	defaultSearchLimit    = 8
	maxSearchQueryBytes   = 2000
	// searchSourceBytes bounds the code a hit carries; a longer declaration
	// is cut at a line end and marked truncated.
	searchSourceBytes = 4000
	// searchSourceReads bounds the source reads one search runs at once.
	searchSourceReads = 6
)

type searchRequest struct {
	RepositoryIDs []string `json:"repository_ids"`
	Query         string   `json:"query"`
	Limit         int      `json:"limit"`
	// Source asks for each hit's code.
	Source bool `json:"source"`
}

// searchHit is a hit in brief, with its code when asked.
type searchHit struct {
	agentquery.CompactHit
	Signature string `json:"signature,omitempty"`
	Text      string `json:"text,omitempty"`
	StartLine uint32 `json:"text_start_line,omitempty"`
	EndLine   uint32 `json:"text_end_line,omitempty"`
	Truncated bool   `json:"truncated,omitempty"`
}

type searchAnswer struct {
	// Semantic says the embeddings ranked the hits too.
	Semantic bool        `json:"semantic"`
	Expanded []string    `json:"expanded_terms,omitempty"`
	Hits     []searchHit `json:"hits"`
}

func (a *workerAPI) registerSearch(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/search", a.guard(a.search))
}

func (a *workerAPI) search(r *http.Request) (int, any, error) {
	var in searchRequest
	decoder := json.NewDecoder(io.LimitReader(r.Body, 64<<10))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&in); err != nil {
		return 0, nil, invalid("body must be one JSON object: %v", err)
	}
	if len(in.RepositoryIDs) == 0 || len(in.RepositoryIDs) > maxSearchRepositories {
		return 0, nil, invalid("repository_ids must name 1 to %d repositories", maxSearchRepositories)
	}
	for _, id := range in.RepositoryIDs {
		if id == "" || len(id) > 256 {
			return 0, nil, invalid("repository_ids must be repository IDs")
		}
	}
	query := strings.TrimSpace(in.Query)
	if query == "" || len(query) > maxSearchQueryBytes {
		return 0, nil, invalid("query is required, at most %d bytes", maxSearchQueryBytes)
	}
	limit := in.Limit
	if limit == 0 {
		limit = defaultSearchLimit
	}
	if limit < 1 || limit > maxSearchLimit {
		return 0, nil, invalid("limit must be 1 to %d", maxSearchLimit)
	}
	result, err := a.searcher.Search(r.Context(), agentquery.SearchRequest{
		RepositoryIDs: in.RepositoryIDs, Text: query, Limit: limit, Expand: true,
	})
	if err != nil {
		return 0, nil, err
	}
	compact := agentquery.CompactHits(result.Hits)
	hits := make([]searchHit, len(compact))
	for i, hit := range compact {
		hits[i] = searchHit{CompactHit: hit, Signature: result.Hits[i].Signature}
	}
	if in.Source {
		a.attachSource(r.Context(), hits)
	}
	return http.StatusOK, searchAnswer{Semantic: result.Semantic, Expanded: result.Expanded, Hits: hits}, nil
}

// attachSource reads each hit's code from the live generation, a few at a
// time. A hit whose source can't be read keeps its snippet: the search
// still answers.
func (a *workerAPI) attachSource(ctx context.Context, hits []searchHit) {
	slots := make(chan struct{}, searchSourceReads)
	var wg sync.WaitGroup
	for i := range hits {
		wg.Add(1)
		slots <- struct{}{}
		go func(hit *searchHit) {
			defer wg.Done()
			defer func() { <-slots }()
			source, err := a.searcher.Source(ctx, agentquery.SourceRequest{RepositoryID: hit.RepositoryID, NodeID: hit.ID})
			if err != nil {
				return
			}
			hit.Text, hit.Truncated = boundSource(source.Text, searchSourceBytes)
			hit.Truncated = hit.Truncated || source.Truncated
			hit.StartLine, hit.EndLine = source.StartLine, source.EndLine
			if hit.Truncated {
				hit.EndLine = source.StartLine + uint32(strings.Count(hit.Text, "\n"))
			}
		}(&hits[i])
	}
	wg.Wait()
}

// boundSource cuts text to at most max bytes, at the last line end before it
// when there is one, never inside a character.
func boundSource(text string, max int) (string, bool) {
	if len(text) <= max {
		return text, false
	}
	cut := max
	for cut > 0 && !utf8.RuneStart(text[cut]) {
		cut--
	}
	if line := strings.LastIndexByte(text[:cut], '\n'); line > 0 {
		cut = line
	}
	return text[:cut], true
}
