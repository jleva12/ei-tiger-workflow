// Package memstore is an in-memory agentquery.Store for tests of the
// agent-facing operations and tool surfaces. Search results are canned:
// ranking is the Spanner store's job and is tested against the emulator.
package memstore

import (
	"context"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

// Store holds one repository's live graph, and optionally others that
// cross-repository links lead to: a read naming one of Others is answered
// by it.
type Store struct {
	Repo   string
	Others map[string]*Store
	// Links are the cross-repository links CrossLinks answers from.
	Links     []graph.CrossLink
	Nodes     map[string]graph.Version
	Edges     []graph.Version
	Sources   map[string][]byte
	Histories map[string][]graph.Version
	// Hits are returned by HybridSearch, cut to the request's limit.
	Hits []spannerstore.SearchHit
	// Searches records every search request received.
	Searches []spannerstore.SearchRequest
	// NeighborPage bounds one Neighbors page; zero means 100.
	NeighborPage int
}

func New(repo string) *Store {
	return &Store{Repo: repo, Others: map[string]*Store{}, Nodes: map[string]graph.Version{}, Sources: map[string][]byte{}, Histories: map[string][]graph.Version{}}
}

// Add holds another repository beside this one and returns it, for links
// to lead to.
func (s *Store) Add(repo string) *Store {
	other := New(repo)
	s.Others[repo] = other
	return other
}

// Link records a cross-repository link from a node of one held repository
// to a node of another, and returns it.
func (s *Store) Link(id, kind, fromRepo, fromID, toRepo, toID string) graph.CrossLink {
	end := func(repo, id string) graph.CrossEnd {
		e := graph.CrossEnd{RepositoryID: repo, NodeID: id}
		if held := s.at(repo); held != nil {
			if v, ok := held.Nodes[id]; ok {
				e.QualifiedName, e.Kind = v.Fact.Node.QualifiedName, v.Fact.Node.Kind
			}
		}
		return e
	}
	l := graph.CrossLink{ID: id, Owner: "team:test", Kind: kind, Source: end(fromRepo, fromID), Target: end(toRepo, toID), Provenance: graph.CrossManual}
	s.Links = append(s.Links, l)
	return l
}

// at returns the store holding repo, or nil.
func (s *Store) at(repo string) *Store {
	if repo == s.Repo {
		return s
	}
	return s.Others[repo]
}

// other returns the store holding repo when it is one of Others.
func (s *Store) other(repo string) *Store {
	if repo == s.Repo {
		return nil
	}
	return s.Others[repo]
}

// CrossLinks filters Links by the query.
func (s *Store) CrossLinks(_ context.Context, q graph.CrossLinkQuery) ([]graph.CrossLink, error) {
	if err := q.Validate(); err != nil {
		return nil, err
	}
	var out []graph.CrossLink
	for _, l := range s.Links {
		if (q.Direction != graph.Incoming && q.Matches(l, graph.Outgoing)) || (q.Direction != graph.Outgoing && q.Matches(l, graph.Incoming)) {
			out = append(out, l)
		}
	}
	return out, nil
}

// AddNode stores a node open at generation 1, with its file lineage when the
// node has a source anchor, and returns it.
func (s *Store) AddNode(n graph.Node) graph.Node {
	v := graph.Version{Fact: graph.Fact{Node: &n}, GenFrom: 1, CommitFrom: "c1"}
	if n.Source != nil {
		v.Lineage = n.Source.Lineage
	}
	s.Nodes[n.ID] = v
	return n
}

// AddVersion stores an arbitrary node version: an open one replaces the
// node's current version, a closed one is kept as history. Tests use it to
// shape a generation's change set.
func (s *Store) AddVersion(v graph.Version) {
	id := v.Fact.Key().ID
	if v.GenTo == 0 {
		s.Nodes[id] = v
		return
	}
	s.Histories[id] = append(s.Histories[id], v)
}

// AddEdge stores an edge of kind from one node to another and returns it.
func (s *Store) AddEdge(kind, from, to string) graph.Edge {
	e := graph.Edge{ID: graph.ID("edge", kind, from, to), Kind: kind, SourceID: from, TargetID: to}
	s.Edges = append(s.Edges, graph.Version{Fact: graph.Fact{Edge: &e}, GenFrom: 1, CommitFrom: "c1"})
	return e
}

// LineagesByPath returns the lineages of the stored nodes whose file is path.
func (s *Store) LineagesByPath(ctx context.Context, repo, path string, generation uint64) ([]string, error) {
	if o := s.other(repo); o != nil {
		return o.LineagesByPath(ctx, repo, path, generation)
	}
	if repo != s.Repo {
		return nil, nil
	}
	seen := map[string]bool{}
	var lineages []string
	for _, v := range s.Nodes {
		if v.Lineage != "" && !seen[v.Lineage] && graph.Text(v.Fact.Node.Properties, "file_path") == path {
			seen[v.Lineage] = true
			lineages = append(lineages, v.Lineage)
		}
	}
	sort.Strings(lineages)
	return lineages, nil
}

// RecordsByLineage returns the stored nodes of one file lineage, by ID.
func (s *Store) RecordsByLineage(ctx context.Context, repo, lineage string, generation uint64) ([]graph.Version, error) {
	if o := s.other(repo); o != nil {
		return o.RecordsByLineage(ctx, repo, lineage, generation)
	}
	if repo != s.Repo {
		return nil, nil
	}
	var records []graph.Version
	for _, v := range s.Nodes {
		if v.Lineage == lineage {
			records = append(records, v)
		}
	}
	sort.Slice(records, func(i, j int) bool { return records[i].Fact.Key().ID < records[j].Fact.Key().ID })
	return records, nil
}

// Hit builds a search hit for a stored node.
func (s *Store) Hit(id string, score float64, exact bool) spannerstore.SearchHit {
	v := s.Nodes[id]
	return spannerstore.SearchHit{RepositoryID: s.Repo, Node: v, Score: score, ExactMatch: exact, Signature: graph.Text(v.Fact.Node.Properties, "signature")}
}

func (s *Store) HybridSearch(_ context.Context, q spannerstore.SearchRequest) ([]spannerstore.SearchHit, error) {
	s.Searches = append(s.Searches, q)
	if _, _, err := validBranches(q); err != nil {
		return nil, err
	}
	hits := append([]spannerstore.SearchHit(nil), s.Hits...)
	if q.Limit > 0 && len(hits) > q.Limit {
		hits = hits[:q.Limit]
	}
	return hits, nil
}

func validBranches(q spannerstore.SearchRequest) (lexical, vector bool, err error) {
	switch q.Mode {
	case "", spannerstore.SearchHybrid, spannerstore.SearchLexical, spannerstore.SearchSemantic:
	default:
		return false, false, fmt.Errorf("%w: search mode %q", deployment.ErrInvalidRequest, q.Mode)
	}
	if strings.TrimSpace(q.Text) == "" && q.Vector == nil {
		return false, false, fmt.Errorf("%w: query text or vector required", deployment.ErrInvalidRequest)
	}
	return true, q.Vector != nil, nil
}

func (s *Store) FindNodes(ctx context.Context, repo, name, qualifiedName string, kinds []string, generation uint64, limit int) ([]graph.Version, error) {
	if o := s.other(repo); o != nil {
		return o.FindNodes(ctx, repo, name, qualifiedName, kinds, generation, limit)
	}
	if repo != s.Repo || (name == "") == (qualifiedName == "") {
		return nil, fmt.Errorf("%w: repository, one of name or qualified name", graph.ErrInvalid)
	}
	var out []graph.Version
	for _, v := range s.Nodes {
		n := v.Fact.Node
		if name != "" && !strings.EqualFold(n.Name, name) {
			continue
		}
		if qualifiedName != "" && n.QualifiedName != qualifiedName {
			continue
		}
		if len(kinds) > 0 && !contains(kinds, n.Kind) {
			continue
		}
		out = append(out, v)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Fact.Node.ID < out[j].Fact.Node.ID })
	if limit > 0 && len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

func (s *Store) GetNode(ctx context.Context, repo, id string, generation uint64) (graph.Version, error) {
	if o := s.other(repo); o != nil {
		return o.GetNode(ctx, repo, id, generation)
	}
	if v, ok := s.Nodes[id]; ok && repo == s.Repo {
		return v, nil
	}
	return graph.Version{}, fmt.Errorf("%w: node %s", graph.ErrNotFound, id)
}

func (s *Store) GetEdge(_ context.Context, repo, id string, _ uint64) (graph.Version, error) {
	for _, v := range s.Edges {
		if v.Fact.Edge.ID == id && repo == s.Repo {
			return v, nil
		}
	}
	return graph.Version{}, fmt.Errorf("%w: edge %s", graph.ErrNotFound, id)
}

func (s *Store) Neighbors(ctx context.Context, q graph.NeighborQuery) (graph.NeighborPage, error) {
	if o := s.other(q.RepositoryID); o != nil {
		return o.Neighbors(ctx, q)
	}
	if q.RepositoryID != s.Repo {
		return graph.NeighborPage{Generation: 1}, nil
	}
	var edges []graph.Version
	for _, v := range s.Edges {
		e := v.Fact.Edge
		out, in := e.SourceID == q.NodeID, e.TargetID == q.NodeID
		if !(out && q.Direction != graph.Incoming) && !(in && q.Direction != graph.Outgoing) {
			continue
		}
		if len(q.EdgeKinds) > 0 && !contains(q.EdgeKinds, e.Kind) {
			continue
		}
		edges = append(edges, v)
	}
	sort.Slice(edges, func(i, j int) bool { return edges[i].Fact.Edge.ID < edges[j].Fact.Edge.ID })
	offset := 0
	if q.Cursor != "" {
		n, err := strconv.Atoi(q.Cursor)
		if err != nil || n < 0 || n > len(edges) {
			return graph.NeighborPage{}, fmt.Errorf("%w: cursor", graph.ErrInvalid)
		}
		offset = n
	}
	limit := q.Limit
	if limit <= 0 {
		limit = 100
	}
	if s.NeighborPage > 0 && limit > s.NeighborPage {
		limit = s.NeighborPage
	}
	page := graph.NeighborPage{Generation: 1, Neighbors: []graph.Neighbor{}}
	end := offset + limit
	if end < len(edges) {
		page.NextCursor = strconv.Itoa(end)
	} else {
		end = len(edges)
	}
	for _, v := range edges[offset:end] {
		e := v.Fact.Edge
		far := e.TargetID
		if far == q.NodeID {
			far = e.SourceID
		}
		nb := graph.Neighbor{Edge: v}
		if node, ok := s.Nodes[far]; ok {
			nb.Node = &node
		}
		page.Neighbors = append(page.Neighbors, nb)
	}
	return page, nil
}

// DependencyEdges filters the stored edges by endpoint set and kinds.
func (s *Store) DependencyEdges(ctx context.Context, repo string, nodeIDs []string, direction graph.Direction, kinds []string, generation uint64, limit int) ([]spannerstore.EdgeRef, bool, error) {
	if o := s.other(repo); o != nil {
		return o.DependencyEdges(ctx, repo, nodeIDs, direction, kinds, generation, limit)
	}
	if repo != s.Repo || len(nodeIDs) == 0 || limit < 1 {
		return nil, false, fmt.Errorf("%w: dependency edge query", graph.ErrInvalid)
	}
	in := map[string]bool{}
	for _, id := range nodeIDs {
		in[id] = true
	}
	var out []spannerstore.EdgeRef
	for _, v := range s.Edges {
		e := v.Fact.Edge
		if len(kinds) > 0 && !contains(kinds, e.Kind) {
			continue
		}
		if (direction != graph.Incoming && in[e.SourceID]) || (direction != graph.Outgoing && in[e.TargetID]) {
			out = append(out, spannerstore.EdgeRef{ID: e.ID, Kind: e.Kind, SourceID: e.SourceID, TargetID: e.TargetID})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	if len(out) > limit {
		return out[:limit], true, nil
	}
	return out, false, nil
}

// GetNodes returns the stored nodes among ids.
func (s *Store) GetNodes(ctx context.Context, repo string, ids []string, generation uint64) (map[string]graph.Version, error) {
	if o := s.other(repo); o != nil {
		return o.GetNodes(ctx, repo, ids, generation)
	}
	out := map[string]graph.Version{}
	if repo != s.Repo {
		return out, nil
	}
	for _, id := range ids {
		if v, ok := s.Nodes[id]; ok {
			out[id] = v
		}
	}
	return out, nil
}

// Hubs counts incoming edges per target over the stored edges.
func (s *Store) Hubs(_ context.Context, repo string, _ uint64, kinds []string, limit int) ([]spannerstore.Hub, error) {
	if repo != s.Repo || limit < 1 {
		return nil, fmt.Errorf("%w: hubs query", graph.ErrInvalid)
	}
	if len(kinds) == 0 {
		kinds = spannerstore.SemanticEdgeKinds()
	}
	byTarget := map[string]*spannerstore.Hub{}
	for _, v := range s.Edges {
		e := v.Fact.Edge
		if !contains(kinds, e.Kind) {
			continue
		}
		h := byTarget[e.TargetID]
		if h == nil {
			h = &spannerstore.Hub{NodeID: e.TargetID, ByKind: map[string]int64{}}
			byTarget[e.TargetID] = h
		}
		h.InDegree++
		h.ByKind[e.Kind]++
	}
	out := make([]spannerstore.Hub, 0, len(byTarget))
	for _, h := range byTarget {
		out = append(out, *h)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].InDegree != out[j].InDegree {
			return out[i].InDegree > out[j].InDegree
		}
		return out[i].NodeID < out[j].NodeID
	})
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

// Changes classifies stored versions by the generation they opened or closed at.
func (s *Store) Changes(_ context.Context, repo string, generation uint64, kind graph.RecordKind, limit int, cursor string) (spannerstore.ChangesPage, error) {
	if repo != s.Repo || kind != graph.RecordNode {
		return spannerstore.ChangesPage{}, fmt.Errorf("%w: changes query", graph.ErrInvalid)
	}
	if generation == 0 {
		generation = 1
	}
	opened := map[string]graph.Version{}
	closed := map[string]graph.Version{}
	for id, v := range s.Nodes {
		if v.GenFrom == generation {
			opened[id] = v
		}
	}
	for id, vs := range s.Histories {
		for _, v := range vs {
			if v.GenFrom == generation && v.GenTo != 0 {
				opened[id] = v
			}
			if v.GenTo == generation {
				closed[id] = v
			}
		}
	}
	ids := map[string]bool{}
	for id := range opened {
		ids[id] = true
	}
	for id := range closed {
		ids[id] = true
	}
	var order []string
	for id := range ids {
		if id > cursor {
			order = append(order, id)
		}
	}
	sort.Strings(order)
	page := spannerstore.ChangesPage{Generation: generation, Changes: []spannerstore.RecordChange{}}
	for _, id := range order {
		o, hasOpen := opened[id]
		c, hasClosed := closed[id]
		switch {
		case hasOpen && hasClosed:
			before := c
			page.Changes = append(page.Changes, spannerstore.RecordChange{Op: spannerstore.ChangeUpdated, ID: id, Version: o, Before: &before})
			page.Updated++
		case hasOpen:
			page.Changes = append(page.Changes, spannerstore.RecordChange{Op: spannerstore.ChangeAdded, ID: id, Version: o})
			page.Added++
		default:
			page.Changes = append(page.Changes, spannerstore.RecordChange{Op: spannerstore.ChangeRetired, ID: id, Version: c})
			page.Retired++
		}
		if page.Commit == "" {
			page.Commit = "c" + strconv.FormatUint(generation, 10)
		}
	}
	if limit > 0 && len(page.Changes) > limit {
		page.NextCursor = page.Changes[limit-1].ID
		page.Changes = page.Changes[:limit]
	}
	return page, nil
}

func (s *Store) History(_ context.Context, repo string, key graph.Key) ([]graph.Version, error) {
	if vs, ok := s.Histories[key.ID]; ok && repo == s.Repo {
		return vs, nil
	}
	if key.Kind == graph.RecordNode {
		if v, ok := s.Nodes[key.ID]; ok {
			return []graph.Version{v}, nil
		}
	}
	return nil, fmt.Errorf("%w: %s %s", graph.ErrNotFound, key.Kind, key.ID)
}

func (s *Store) GetSource(ctx context.Context, repo, sha string) ([]byte, error) {
	if o := s.other(repo); o != nil {
		return o.GetSource(ctx, repo, sha)
	}
	if data, ok := s.Sources[sha]; ok && repo == s.Repo {
		return data, nil
	}
	return nil, fmt.Errorf("%w: source %s", deployment.ErrNotFound, sha)
}

func (s *Store) State(ctx context.Context, repo string) (graph.RepositoryState, error) {
	if o := s.other(repo); o != nil {
		return o.State(ctx, repo)
	}
	if repo != s.Repo {
		return graph.RepositoryState{}, fmt.Errorf("%w: repository %s", graph.ErrNotFound, repo)
	}
	return graph.RepositoryState{RepositoryID: repo, LiveGeneration: 1, LiveCommit: "c1"}, nil
}

func contains(xs []string, x string) bool {
	for _, s := range xs {
		if s == x {
			return true
		}
	}
	return false
}
