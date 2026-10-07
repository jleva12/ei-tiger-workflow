package spannerstore

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"unicode"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

// Search is two-level retrieval for agents. The query text is first read by
// codesearch.ParseQuery: an identifier, a partial identifier, or a natural-
// language question whose symbols, paths and content words are separated
// from its question words. Level one runs up to two branches per repository
// inside one read-only transaction: the lexical branch queries the
// CGSearchDocumentsIndex search index (full-text tokens of the retrieval
// document, full-text and substring tokens of the identifier forms) with the
// content words ranked by Spanner's SCORE, and the vector branch ranks stored
// embeddings by cosine distance. Each branch yields ranks that weighted
// reciprocal rank fusion combines, so their unrelated score scales never
// matter, and either branch works alone. Level two reranks the fused
// candidates with facts the text does not carry: a node whose name or
// qualified name is the query, or a symbol the question names, goes to the
// top; a node whose name is merely a word of the question edges ahead; well-
// connected declarations edge ahead of isolated ones; and hits near the
// caller's current file, or under a path the question mentions, edge ahead
// of far ones. Only nodes open at each repository's live generation whose
// document or embedding still matches their current text are candidates.
//
// The vector branch is an approximate nearest-neighbour query on the
// CGSearchEmbeddingsVector vector index: APPROX_COSINE_DISTANCE against the
// fixed-length FLOAT32 embedding column, ordered by that distance alone with
// a bounded LIMIT, as the index requires. Repository, model and document
// version are filtered in the query; the stored document hash lets the
// rerank drop candidates whose node text has changed since they were
// embedded, and kind and path filters are applied to the candidates, so the
// candidate pool is widened when such filters are present.

const (
	searchPageSize   = 256 // a page fills the embedding requests in flight
	maxEmbeddingRows = 64
	maxSearchLimit   = 50
	rrfK             = 60.0
	exactMatchBoost  = 1.0   // dwarfs any fused score, which is at most 2/(rrfK+1)
	nameMatchBoost   = 0.02  // between one and two first-rank branch contributions
	rerankBoost      = 0.004 // about a quarter of a first-rank branch contribution
	minNeedleLength  = 3     // words shorter than this are never tried as names
	snippetBytes     = 240
	candidateFactor  = 4   // candidates per branch relative to the requested page
	minCandidates    = 32  // candidate floor so ties are reranked, not cut, on small pages
	exactTierLimit   = 200 // exact name matches are never cut by the page size
	substringMinimum = 4   // TOKENIZE_SUBSTRING's default shortest n-gram
	// annLeavesToSearch is the num_leaves_to_search of the vector index query.
	// Spanner sizes the index automatically; searching this many leaves keeps
	// recall high for graphs of up to a few million documents, and the count
	// grows with the candidate pool when filters narrow it afterwards.
	annLeavesToSearch = 32
	annFilterFactor   = 4
)

// semanticEdgeKinds are the edges the graph rerank counts as callers and callees.
var semanticEdgeKinds = []string{graph.EdgeCalls, graph.EdgeReferences, graph.EdgeUsesType, graph.EdgeInherits, graph.EdgeOverrides, graph.EdgeImplements, graph.EdgeFrameworkBinding}

type SearchMode string

const (
	SearchHybrid   SearchMode = "hybrid"   // every branch whose inputs are present (default)
	SearchLexical  SearchMode = "lexical"  // the search index only
	SearchSemantic SearchMode = "semantic" // stored embeddings only; needs Model and Vector
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

type SearchRequest struct {
	RepositoryIDs []string
	Mode          SearchMode // empty means hybrid
	Model         string     // embedding model of Vector; empty disables the vector branch
	Text          string     // query text: the lexical branch, the exact tier, matched terms and snippets
	Vector        []float64  // query embedding; nil disables the vector branch
	Kinds         []string   // node kinds to keep; empty means all
	PathPrefix    string     // keep nodes whose file_path property starts with this
	NearPath      string     // the caller's current file; hits in nearby directories rank higher
	EnhanceQuery  bool       // Spanner query enhancement (spelling, synonyms, plurals); production only
	Limit         int        // default 10, at most 50
}

type SearchHit struct {
	RepositoryID string        `json:"repository_id"`
	Node         graph.Version `json:"node"`
	Score        float64       `json:"score"`       // fused and reranked score used for ordering
	Lexical      float64       `json:"lexical"`     // Spanner SCORE of the lexical branch, 0 when absent
	Vector       float64       `json:"vector"`      // cosine similarity to the query vector, 0 when absent
	ExactMatch   bool          `json:"exact_match"` // the name or qualified name equals the query or a symbol it names
	NameMatch    bool          `json:"name_match"`  // the name equals a plain word of the question
	Callers      int64         `json:"callers"`     // open incoming semantic edges
	Callees      int64         `json:"callees"`     // open outgoing semantic edges
	Signature    string        `json:"signature,omitempty"`
	Snippet      string        `json:"snippet,omitempty"`
	Matched      []string      `json:"matched,omitempty"` // query terms present in the document
}

// searchTerms is the lexical tokenizer shared with matched-term reporting.
func searchTerms(text string) []string { return codesearch.Terms(text) }

// lexicalQuery is the rquery the search index evaluates: any term may match,
// and SCORE rewards documents that match more of them.
func lexicalQuery(terms []string) string { return strings.Join(terms, " OR ") }

// substringProbe is the longest term long enough for the substring index,
// so a partial identifier such as "AsyncImp" still finds runAsyncImpl.
func substringProbe(terms []string) string {
	probe := ""
	for _, term := range terms {
		if len(term) >= substringMinimum && len(term) > len(probe) {
			probe = term
		}
	}
	return probe
}

// exactNeedle is the lower-cased query when it is a single token; only such
// queries are compared with node names for the exact tier.
func exactNeedle(text string) string {
	text = strings.TrimSpace(text)
	if text == "" || strings.ContainsAny(text, " \t\r\n") {
		return ""
	}
	return strings.ToLower(text)
}

// identifierLike reports a query that spells a symbol rather than describing
// one: a single token with camelCase, dots, parentheses or underscores.
func identifierLike(text string) bool {
	text = strings.TrimSpace(text)
	if text == "" || strings.ContainsAny(text, " \t\r\n") {
		return false
	}
	if strings.ContainsAny(text, ".:(_$") {
		return true
	}
	lower, upper := false, false
	for _, r := range text {
		lower = lower || unicode.IsLower(r)
		upper = upper || unicode.IsUpper(r)
	}
	return lower && upper
}

// branchWeights favours the lexical branch for identifier-shaped queries and
// the vector branch for natural language; a lone branch always weighs one.
func branchWeights(q codesearch.Query, lexical, vector bool) (float64, float64) {
	switch {
	case !(lexical && vector):
		return 1, 1
	case identifierLike(q.Text):
		return 1, 0.5
	case q.Sentence:
		return 0.7, 1
	}
	return 1, 1
}

// nameNeedles are the lower-cased names the exact tier looks up. Strong
// needles come from the whole query when it is one token and from every
// symbol the question names (its simple name and, for a member reference,
// its owner); a hit on one is an exact match. Weak needles are the plain
// content words of a sentence; a hit on one is a name match. A word is never
// both.
func nameNeedles(q codesearch.Query) (strong, weak map[string]bool) {
	strong, weak = map[string]bool{}, map[string]bool{}
	if needle := exactNeedle(q.Text); needle != "" {
		strong[needle] = true
	}
	for _, symbol := range q.Symbols {
		for _, name := range []string{codesearch.SimpleName(symbol), codesearch.Owner(symbol)} {
			if len(name) >= 2 {
				strong[strings.ToLower(name)] = true
			}
		}
	}
	if q.Sentence {
		for _, term := range q.Terms {
			if len(term) >= minNeedleLength && !strong[term] {
				weak[term] = true
			}
		}
	}
	return strong, weak
}

// pathHint reports whether a hit's file lies under, or is, a path the
// question mentions.
func pathHint(filePath string, paths []string) bool {
	lower := strings.ToLower(filePath)
	for _, p := range paths {
		if strings.Contains(lower, strings.ToLower(strings.Trim(p, "/"))) {
			return true
		}
	}
	return false
}

// pathProximity is the share of leading directory segments two paths have in
// common, 0 when they share none or either path has no directory.
func pathProximity(a, b string) float64 {
	da, db := strings.Split(a, "/"), strings.Split(b, "/")
	da, db = da[:len(da)-1], db[:len(db)-1]
	total := max(len(da), len(db))
	if total == 0 {
		return 0
	}
	shared := 0
	for shared < len(da) && shared < len(db) && da[shared] == db[shared] {
		shared++
	}
	return float64(shared) / float64(total)
}

// snippetSource is the text a snippet is cut from: the body, else the
// documentation, else the signature.
func snippetSource(n graph.Node) string {
	for _, key := range []string{"source_text", "docstring", "signature"} {
		if v := graph.Text(n.Properties, key); v != "" {
			return v
		}
	}
	return ""
}

func matchedTerms(text string, terms []string) []string {
	lower := strings.ToLower(text)
	var out []string
	for _, term := range terms {
		if strings.Contains(lower, term) {
			out = append(out, term)
		}
	}
	return out
}

// branches decides which level-one branches run for the request.
func (q SearchRequest) branches() (lexical, vector bool, err error) {
	mode := q.Mode
	if mode == "" {
		mode = SearchHybrid
	}
	hasText := len(searchTerms(q.Text)) > 0
	hasVector := q.Vector != nil && q.Model != ""
	switch mode {
	case SearchHybrid:
		lexical, vector = hasText, hasVector
	case SearchLexical:
		lexical = hasText
	case SearchSemantic:
		vector = hasVector
	default:
		return false, false, fmt.Errorf("%w: search mode %q", deployment.ErrInvalidRequest, q.Mode)
	}
	if !lexical && !vector {
		return false, false, fmt.Errorf("%w: query text, or a vector with its model, required for mode %s", deployment.ErrInvalidRequest, mode)
	}
	return lexical, vector, nil
}

// HybridSearch runs the two-level search described at the top of this file
// across the requested repositories and returns at most Limit hits ordered
// by score.
func (s *Store) HybridSearch(ctx context.Context, q SearchRequest) ([]SearchHit, error) {
	if len(q.RepositoryIDs) == 0 || len(q.RepositoryIDs) > 32 || len(q.Model) > 256 || !validKinds(q.Kinds) || len(q.PathPrefix) > 2048 || len(q.NearPath) > 2048 || len(q.Text) > 8000 || q.Limit < 0 || q.Limit > maxSearchLimit {
		return nil, fmt.Errorf("%w: search request", deployment.ErrInvalidRequest)
	}
	for _, repo := range q.RepositoryIDs {
		if !validRepositoryID(repo) {
			return nil, fmt.Errorf("%w: repository id", deployment.ErrInvalidRequest)
		}
	}
	lexical, vector, err := q.branches()
	if err != nil {
		return nil, err
	}
	if vector {
		if len(q.Vector) != s.vectorLength {
			return nil, fmt.Errorf("%w: %d-dimensional query vector, database vector length is %d", deployment.ErrInvalidRequest, len(q.Vector), s.vectorLength)
		}
		if err := codesearch.ValidateVector(q.Vector, len(q.Vector)); err != nil {
			return nil, err
		}
	}
	if q.Limit == 0 {
		q.Limit = 10
	}
	parsed := codesearch.ParseQuery(q.Text)
	weightLexical, weightVector := branchWeights(parsed, lexical, vector)
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	var hits []SearchHit
	for _, repo := range q.RepositoryIDs {
		repoHits, err := s.searchRepository(ctx, t, repo, q, parsed, lexical, vector, weightLexical, weightVector)
		if err != nil {
			return nil, err
		}
		hits = append(hits, repoHits...)
	}
	sort.Slice(hits, func(i, j int) bool {
		if hits[i].Score != hits[j].Score {
			return hits[i].Score > hits[j].Score
		}
		if hits[i].RepositoryID != hits[j].RepositoryID {
			return hits[i].RepositoryID < hits[j].RepositoryID
		}
		return hits[i].Node.Fact.Node.ID < hits[j].Node.Fact.Node.ID
	})
	if len(hits) > q.Limit {
		hits = hits[:q.Limit]
	}
	return hits, nil
}

func (s *Store) searchRepository(ctx context.Context, t reader, repo string, q SearchRequest, parsed codesearch.Query, lexical, vector bool, weightLexical, weightVector float64) ([]SearchHit, error) {
	live, err := liveGeneration(ctx, t, repo)
	if err != nil {
		return nil, err
	}
	terms := parsed.Terms
	depth := int64(max(q.Limit*candidateFactor, minCandidates))
	scores := map[string]*SearchHit{}
	docHashes := map[string]string{} // index candidates: the document hash the index row describes
	collect := func(sql string, params map[string]any, weight float64, onRow func(*SearchHit, *spanner.Row) error) error {
		it := t.Query(ctx, spanner.Statement{SQL: sql, Params: params})
		defer it.Stop()
		rank := 0
		for {
			row, err := nextRow(it)
			if err != nil {
				return err
			}
			if row == nil {
				return nil
			}
			var id string
			if err = row.Column(0, &id); err != nil {
				return err
			}
			rank++
			h := scores[id]
			if h == nil {
				h = &SearchHit{RepositoryID: repo}
				scores[id] = h
			}
			h.Score += weight / (rrfK + float64(rank))
			if err = onRow(h, row); err != nil {
				return err
			}
		}
	}

	// Level one, lexical branch: the search index over documents and identifiers.
	if lexical {
		params := map[string]any{"repo": repo, "q": lexicalQuery(terms), "enhance": q.EnhanceQuery, "version": int64(codesearch.DocumentVersion), "limit": depth}
		where := "SEARCH(d.Tokens, @q, enhance_query => @enhance) OR SEARCH(d.IdentifierTokens, @q, enhance_query => @enhance)"
		if probe := substringProbe(terms); probe != "" {
			params["sub"] = probe
			where += " OR SEARCH_SUBSTRING(d.IdentifierSubstrings, @sub)"
		}
		sql := "SELECT d.NodeID, d.DocumentHash, SCORE(d.Tokens, @q, enhance_query => @enhance) + 2 * SCORE(d.IdentifierTokens, @q, enhance_query => @enhance) AS score" +
			" FROM CGSearchDocuments@{FORCE_INDEX=CGSearchDocumentsIndex} d WHERE d.RepositoryID=@repo AND d.DocumentVersion=@version AND (" + where + ")"
		if len(q.Kinds) > 0 {
			sql += " AND d.Kind IN UNNEST(@kinds)"
			params["kinds"] = q.Kinds
		}
		if q.PathPrefix != "" {
			sql += " AND STARTS_WITH(d.FilePath, @prefix)"
			params["prefix"] = q.PathPrefix
		}
		err = collect(sql+" ORDER BY score DESC, d.NodeID LIMIT @limit", params, weightLexical, func(h *SearchHit, row *spanner.Row) error {
			var id, hash string
			var score float64
			if err := row.Columns(&id, &hash, &score); err != nil {
				return err
			}
			docHashes[id] = hash
			h.Lexical = score
			return nil
		})
		if err != nil {
			return nil, err
		}
	}

	// Level one, vector branch: approximate nearest neighbours on the vector
	// index. The index dictates the query shape (distance as the sole sort
	// key, a LIMIT, no joins), so the open-at-live check, the kind and path
	// filters and the document-hash check happen on the candidates below;
	// with kind or path filters the candidate pool is widened to compensate.
	if vector {
		candidates, leaves := depth, annLeavesToSearch
		if len(q.Kinds) > 0 || q.PathPrefix != "" {
			candidates *= annFilterFactor
			leaves *= annFilterFactor
		}
		params := map[string]any{"repo": repo, "model": q.Model, "version": int64(codesearch.DocumentVersion), "limit": candidates, "vector": float32Vector(q.Vector)}
		sql := fmt.Sprintf(`SELECT e.NodeID, e.DocumentHash, APPROX_COSINE_DISTANCE(e.Embedding, @vector, options => JSON '{"num_leaves_to_search": %d}') AS distance
 FROM CGSearchEmbeddings@{FORCE_INDEX=CGSearchEmbeddingsVector} e
 WHERE e.RepositoryID=@repo AND e.Model=@model AND e.DocumentVersion=@version
 ORDER BY distance LIMIT @limit`, leaves)
		err = collect(sql, params, weightVector, func(h *SearchHit, row *spanner.Row) error {
			var id, hash string
			var distance float64
			if err := row.Columns(&id, &hash, &distance); err != nil {
				return err
			}
			docHashes[id] = hash
			h.Vector = 1 - distance
			return nil
		})
		if err != nil {
			return nil, err
		}
	}

	// Exact tier: nodes named by the query. A strong needle (the whole query
	// when it is one token, or a symbol the question names) makes an exact
	// match on any kind; a weak needle (a plain word of the question) makes a
	// name match on types and callables only, so a question mentioning
	// "order" reaches the Order class and not every field called order.
	strong, weak := nameNeedles(parsed)
	exact, err := s.namedNodes(ctx, t, repo, strong, q.Kinds)
	if err != nil {
		return nil, err
	}
	named := map[string]bool{}
	if kinds, ok := intersectKinds(q.Kinds, namedKinds); ok {
		if named, err = s.namedNodes(ctx, t, repo, weak, kinds); err != nil {
			return nil, err
		}
	}
	for id := range exact {
		if scores[id] == nil {
			scores[id] = &SearchHit{RepositoryID: repo}
		}
	}
	for id := range named {
		if scores[id] == nil && !exact[id] {
			scores[id] = &SearchHit{RepositoryID: repo}
		}
	}
	symbols := map[string]bool{}
	for _, symbol := range parsed.Symbols {
		symbols[symbol] = true
	}

	ids := make([]string, 0, len(scores))
	for id := range scores {
		ids = append(ids, id)
	}
	nodes, err := readOpenNodes(ctx, t, repo, ids, live, live)
	if err != nil {
		return nil, err
	}
	wholeQuery := strings.TrimSpace(q.Text)
	out := make([]SearchHit, 0, len(scores))
	for id, h := range scores {
		v, ok := nodes[id]
		if !ok {
			continue // not open at live
		}
		n := v.Fact.Node
		if q.PathPrefix != "" && !strings.HasPrefix(graph.Text(n.Properties, "file_path"), q.PathPrefix) {
			continue
		}
		if len(q.Kinds) > 0 && !containsString(q.Kinds, n.Kind) {
			continue
		}
		doc, isDocument := codesearch.FromNode(*n)
		if hash, fromIndex := docHashes[id]; fromIndex && (!isDocument || hash != doc.Hash) {
			continue // the index row describes text this node no longer has
		}
		switch {
		case exact[id] || (wholeQuery != "" && n.QualifiedName == wholeQuery) || symbols[n.QualifiedName]:
			h.ExactMatch = true
			h.Score += exactMatchBoost
		case named[id]:
			h.NameMatch = true
			h.Score += nameMatchBoost
		}
		h.Node = v
		h.Signature = graph.Text(n.Properties, "signature")
		if isDocument {
			h.Matched = matchedTerms(doc.Text, terms)
		}
		if src := snippetSource(*n); src != "" {
			h.Snippet = codesearch.Snippet(src, terms, snippetBytes)
		}
		out = append(out, *h)
	}
	if len(out) == 0 {
		return nil, nil
	}

	// Level two: graph rerank by connectivity and path proximity.
	hitIDs := make([]string, len(out))
	for i := range out {
		hitIDs[i] = out[i].Node.Fact.Node.ID
	}
	callers, err := s.degrees(ctx, t, repo, "CGEdgesByTarget", "TargetID", hitIDs, live)
	if err != nil {
		return nil, err
	}
	callees, err := s.degrees(ctx, t, repo, "CGEdgesBySource", "SourceID", hitIDs, live)
	if err != nil {
		return nil, err
	}
	var maxCallers int64
	for _, n := range callers {
		maxCallers = max(maxCallers, n)
	}
	for i := range out {
		id := out[i].Node.Fact.Node.ID
		out[i].Callers, out[i].Callees = callers[id], callees[id]
		if maxCallers > 0 {
			out[i].Score += rerankBoost * math.Log1p(float64(out[i].Callers)) / math.Log1p(float64(maxCallers))
		}
		filePath := graph.Text(out[i].Node.Fact.Node.Properties, "file_path")
		if q.NearPath != "" {
			out[i].Score += rerankBoost * pathProximity(filePath, q.NearPath)
		}
		if pathHint(filePath, parsed.Paths) {
			out[i].Score += rerankBoost
		}
	}
	return out, nil
}

// namedKinds are the kinds a plain word of a question may name: types and
// callables, whose names are chosen to be spoken about. Fields, constants
// and chunks are reached through symbols or the lexical branch instead.
var namedKinds = []string{"class", "interface", "enum", "record", "annotation_type", "method", "constructor", "function"}

// intersectKinds narrows the requested kinds to the allowed ones; with no
// request every allowed kind is returned. False means the caller asked only
// for kinds a word cannot name.
func intersectKinds(requested, allowed []string) ([]string, bool) {
	if len(requested) == 0 {
		return allowed, true
	}
	var out []string
	for _, k := range requested {
		if containsString(allowed, k) {
			out = append(out, k)
		}
	}
	return out, len(out) > 0
}

// SemanticEdgeKinds are the edge kinds that relate declarations by meaning
// rather than containment: the ones an agent follows to find callers,
// callees, users of a type and the members of a hierarchy.
func SemanticEdgeKinds() []string { return append([]string(nil), semanticEdgeKinds...) }

// namedNodes returns the IDs of the search documents whose lower-cased name
// is one of the needles, restricted to kinds when given, through the name
// index. Rows are bounded per needle so one common name cannot crowd out the
// others.
func (s *Store) namedNodes(ctx context.Context, t reader, repo string, needles map[string]bool, kinds []string) (map[string]bool, error) {
	out := map[string]bool{}
	if len(needles) == 0 {
		return out, nil
	}
	sorted := make([]string, 0, len(needles))
	for n := range needles {
		sorted = append(sorted, n)
	}
	sort.Strings(sorted)
	for _, needle := range sorted {
		params := map[string]any{"repo": repo, "needle": needle, "limit": int64(exactTierLimit)}
		sql := "SELECT NodeID FROM CGSearchDocuments@{FORCE_INDEX=CGSearchDocumentsByName} WHERE RepositoryID=@repo AND NameLower=@needle"
		if len(kinds) > 0 {
			sql += " AND Kind IN UNNEST(@kinds)"
			params["kinds"] = kinds
		}
		it := t.Query(ctx, spanner.Statement{SQL: sql + " ORDER BY NodeID LIMIT @limit", Params: params})
		for {
			row, err := nextRow(it)
			if err != nil {
				it.Stop()
				return nil, err
			}
			if row == nil {
				break
			}
			var id string
			if err = row.Column(0, &id); err != nil {
				it.Stop()
				return nil, err
			}
			out[id] = true
		}
		it.Stop()
	}
	return out, nil
}

// degrees counts, per node, the open semantic edges on one side at generation.
func (s *Store) degrees(ctx context.Context, t reader, repo, index, column string, ids []string, generation uint64) (map[string]int64, error) {
	out := make(map[string]int64, len(ids))
	for start := 0; start < len(ids); start += idBatch {
		chunk := ids[start:min(start+idBatch, len(ids))]
		it := t.Query(ctx, spanner.Statement{
			SQL:    nullFilteredHint + "SELECT " + column + ", COUNT(*) FROM CGRecords@{FORCE_INDEX=" + index + "} WHERE RepositoryID=@repo AND " + column + " IS NOT NULL AND " + column + " IN UNNEST(@ids) AND Kind IN UNNEST(@kinds) AND " + openPredicate("") + " GROUP BY " + column,
			Params: map[string]any{"repo": repo, "ids": chunk, "kinds": semanticEdgeKinds, "gen": int64(generation)},
		})
		for {
			row, err := nextRow(it)
			if err != nil {
				it.Stop()
				return nil, err
			}
			if row == nil {
				break
			}
			var id string
			var n int64
			if err = row.Columns(&id, &n); err != nil {
				it.Stop()
				return nil, err
			}
			out[id] = n
		}
		it.Stop()
	}
	return out, nil
}

func containsString(xs []string, x string) bool {
	for _, s := range xs {
		if s == x {
			return true
		}
	}
	return false
}
