package spannerstore

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

// Generation semantics: a reader at generation L sees a record version iff
// GenFrom <= L AND (GenTo IS NULL OR GenTo > L). Nothing above the live
// generation is ever exposed: a version closed above live (by a generation
// still loading) is presented as open, and versions opened above live are
// invisible.

const recordSelect = "RecordKind, RecordID, GenFrom, GenTo, CommitFrom, CommitTo, Retired, Lineage, FactDigest, Payload"

var recordColumns = []string{"RecordKind", "RecordID", "GenFrom", "GenTo", "CommitFrom", "CommitTo", "Retired", "Lineage", "FactDigest", "Payload"}

// nullFilteredHint lets the emulator use a NULL_FILTERED index for queries
// whose predicates already exclude NULL keys (equality, IN UNNEST, GenTo>@live),
// which Cloud Spanner verifies itself; production ignores the hint.
const nullFilteredHint = "@{spanner_emulator.disable_query_null_filtered_index_check=true} "

// openPredicate is the SQL form of the generation rule for parameter @gen.
func openPredicate(alias string) string {
	if alias != "" {
		alias += "."
	}
	return alias + "GenFrom<=@gen AND (" + alias + "GenTo IS NULL OR " + alias + "GenTo>@gen)"
}

// openAt is the same rule over decoded columns.
func openAt(genFrom int64, genTo spanner.NullInt64, generation uint64) bool {
	return genFrom >= 0 && uint64(genFrom) <= generation && (!genTo.Valid || genTo.Int64 < 0 || uint64(genTo.Int64) > generation)
}

// resolveGeneration maps 0 to the live generation and rejects generations
// above it: a generation still loading is never visible through the Reader.
func resolveGeneration(live, requested uint64) (uint64, error) {
	if requested == 0 {
		return live, nil
	}
	if requested > live {
		return 0, fmt.Errorf("%w: generation %d above live %d", graph.ErrInvalid, requested, live)
	}
	return requested, nil
}

// liveGeneration reads the repository gate; missing repositories are
// graph.ErrNotFound for readers.
func liveGeneration(ctx context.Context, t reader, repo string) (uint64, error) {
	r, err := readRepo(ctx, t, repo)
	if err != nil {
		if errors.Is(err, deployment.ErrNotFound) {
			return 0, fmt.Errorf("%w: repository %s", graph.ErrNotFound, repo)
		}
		return 0, err
	}
	return uint64(r.LiveGeneration), nil
}

// generationFor resolves the requested generation and returns it with live.
func (s *Store) generationFor(ctx context.Context, t reader, repo string, requested uint64) (gen, live uint64, err error) {
	if live, err = liveGeneration(ctx, t, repo); err != nil {
		return 0, 0, err
	}
	gen, err = resolveGeneration(live, requested)
	return gen, live, err
}

// maskAboveLive hides a closure made by a generation that is not published.
func maskAboveLive(v graph.Version, live uint64) graph.Version {
	if v.GenTo > live {
		v.GenTo, v.CommitTo, v.Retired = 0, "", false
	}
	return v
}

// versionFromRow decodes a record row selected with recordSelect.
func versionFromRow(row *spanner.Row) (graph.Version, error) {
	var kind, id, commitFrom, commitTo, digest string
	var genFrom int64
	var genTo spanner.NullInt64
	var retired bool
	var lineage spanner.NullString
	var b []byte
	if err := row.Columns(&kind, &id, &genFrom, &genTo, &commitFrom, &commitTo, &retired, &lineage, &digest, &b); err != nil {
		return graph.Version{}, err
	}
	v := graph.Version{Lineage: lineage.StringVal, GenFrom: uint64(genFrom), CommitFrom: commitFrom, CommitTo: commitTo, Retired: retired, FactDigest: digest}
	if genTo.Valid {
		v.GenTo = uint64(genTo.Int64)
	}
	if err := json.Unmarshal(b, &v.Fact); err != nil {
		return v, fmt.Errorf("%w: record %s %s payload: %v", graph.ErrIntegrity, kind, id, err)
	}
	if err := v.Validate(); err != nil {
		return v, fmt.Errorf("%w: record %s %s: %v", graph.ErrIntegrity, kind, id, err)
	}
	if v.Fact.Key() != (graph.Key{Kind: graph.RecordKind(kind), ID: id}) {
		return v, fmt.Errorf("%w: record %s %s payload key differs", graph.ErrIntegrity, kind, id)
	}
	return v, nil
}

// collectVersions decodes rows selected with recordSelect as seen at live.
func collectVersions(it *spanner.RowIterator, live uint64) ([]graph.Version, error) {
	defer it.Stop()
	var out []graph.Version
	for {
		row, err := nextRow(it)
		if err != nil {
			return nil, err
		}
		if row == nil {
			return out, nil
		}
		v, err := versionFromRow(row)
		if err != nil {
			return nil, err
		}
		out = append(out, maskAboveLive(v, live))
	}
}

func (s *Store) pageLimit(limit int) int {
	if limit <= 0 || limit > s.limits.MaxPageSize {
		return s.limits.MaxPageSize
	}
	return limit
}

func validKinds(kinds []string) bool {
	if len(kinds) > 32 {
		return false
	}
	for _, k := range kinds {
		if k == "" || len(k) > 64 {
			return false
		}
	}
	return true
}

// --- graph.Reader ---------------------------------------------------------

func (s *Store) State(ctx context.Context, repositoryID string) (graph.RepositoryState, error) {
	if !validRepositoryID(repositoryID) {
		return graph.RepositoryState{}, fmt.Errorf("%w: repository id", graph.ErrInvalid)
	}
	r, err := readRepo(ctx, s.client.Single(), repositoryID)
	if err != nil {
		if errors.Is(err, deployment.ErrNotFound) {
			return graph.RepositoryState{}, fmt.Errorf("%w: repository %s", graph.ErrNotFound, repositoryID)
		}
		return graph.RepositoryState{}, err
	}
	return r.state()
}

// GenerationCommit reports the exact revision of a successfully published
// generation. Failed attempts at the same generation are never returned.
func (s *Store) GenerationCommit(ctx context.Context, repo string, generation uint64) (string, error) {
	if !validRepositoryID(repo) || generation == 0 || generation > math.MaxInt64 {
		return "", graph.ErrInvalid
	}
	it := s.client.Single().Query(ctx, spanner.Statement{SQL: "SELECT CommitSHA FROM CGRuns WHERE RepositoryID=@repo AND Generation=@gen AND Phase=@phase LIMIT 1", Params: map[string]any{"repo": repo, "gen": int64(generation), "phase": string(deployment.Succeeded)}})
	defer it.Stop()
	row, err := nextRow(it)
	if err != nil {
		return "", err
	}
	if row == nil {
		return "", graph.ErrNotFound
	}
	var commit string
	err = row.Columns(&commit)
	return commit, err
}

func (s *Store) GetNode(ctx context.Context, repositoryID, id string, generation uint64) (graph.Version, error) {
	return s.getRecord(ctx, repositoryID, graph.Key{Kind: graph.RecordNode, ID: id}, generation)
}

func (s *Store) GetEdge(ctx context.Context, repositoryID, id string, generation uint64) (graph.Version, error) {
	return s.getRecord(ctx, repositoryID, graph.Key{Kind: graph.RecordEdge, ID: id}, generation)
}

func (s *Store) getRecord(ctx context.Context, repo string, key graph.Key, generation uint64) (graph.Version, error) {
	if !validRepositoryID(repo) || !graph.ValidID(key.ID) {
		return graph.Version{}, fmt.Errorf("%w: repository or record id", graph.ErrInvalid)
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen, live, err := s.generationFor(ctx, t, repo, generation)
	if err != nil {
		return graph.Version{}, err
	}
	return readOpenRecord(ctx, t, repo, key, gen, live)
}

// readOpenRecord returns the version of one record open at gen.
func readOpenRecord(ctx context.Context, t reader, repo string, key graph.Key, gen, live uint64) (graph.Version, error) {
	it := t.Query(ctx, spanner.Statement{
		SQL:    "SELECT " + recordSelect + " FROM CGRecords WHERE RepositoryID=@repo AND RecordKind=@kind AND RecordID=@id AND " + openPredicate("") + " LIMIT 1",
		Params: map[string]any{"repo": repo, "kind": string(key.Kind), "id": key.ID, "gen": int64(gen)},
	})
	vs, err := collectVersions(it, live)
	if err != nil {
		return graph.Version{}, err
	}
	if len(vs) == 0 {
		return graph.Version{}, fmt.Errorf("%w: %s %s at generation %d", graph.ErrNotFound, key.Kind, key.ID, gen)
	}
	return vs[0], nil
}

func (s *Store) ListNodes(ctx context.Context, q graph.ListQuery) (graph.NodePage, error) {
	gen, vs, next, err := s.listRecords(ctx, graph.RecordNode, q)
	return graph.NodePage{Generation: gen, Nodes: vs, NextCursor: next}, err
}

func (s *Store) ListEdges(ctx context.Context, q graph.ListQuery) (graph.EdgePage, error) {
	gen, vs, next, err := s.listRecords(ctx, graph.RecordEdge, q)
	return graph.EdgePage{Generation: gen, Edges: vs, NextCursor: next}, err
}

func (s *Store) listRecords(ctx context.Context, kind graph.RecordKind, q graph.ListQuery) (uint64, []graph.Version, string, error) {
	if !validRepositoryID(q.RepositoryID) || len(q.Kind) > 64 {
		return 0, nil, "", fmt.Errorf("%w: list query", graph.ErrInvalid)
	}
	limit := s.pageLimit(q.Limit)
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen, live, err := s.generationFor(ctx, t, q.RepositoryID, q.Generation)
	if err != nil {
		return 0, nil, "", err
	}
	scope := s.cursors.scope("list", q.RepositoryID, string(kind), q.Kind)
	after, err := s.cursors.decode(scope, gen, q.Cursor)
	if err != nil {
		return 0, nil, "", err
	}
	table, filter := "CGRecords", ""
	params := map[string]any{"repo": q.RepositoryID, "rk": string(kind), "after": after, "gen": int64(gen), "limit": int64(limit + 1)}
	if q.Kind != "" {
		table, filter = "CGRecords@{FORCE_INDEX=CGRecordsByKind}", " AND Kind=@kind"
		params["kind"] = q.Kind
	}
	it := t.Query(ctx, spanner.Statement{
		SQL:    "SELECT " + recordSelect + " FROM " + table + " WHERE RepositoryID=@repo AND RecordKind=@rk" + filter + " AND RecordID>@after AND " + openPredicate("") + " ORDER BY RecordID LIMIT @limit",
		Params: params,
	})
	vs, err := collectVersions(it, live)
	if err != nil {
		return 0, nil, "", err
	}
	next := ""
	if len(vs) > limit {
		vs = vs[:limit]
		next = s.cursors.encode(scope, gen, vs[limit-1].Fact.Key().ID)
	}
	return gen, vs, next, nil
}

// Neighbors pages the edges touching a node in RecordID order and attaches
// each far endpoint's version open at the same generation.
func (s *Store) Neighbors(ctx context.Context, q graph.NeighborQuery) (graph.NeighborPage, error) {
	if !validRepositoryID(q.RepositoryID) || !graph.ValidID(q.NodeID) || !validKinds(q.EdgeKinds) {
		return graph.NeighborPage{}, fmt.Errorf("%w: neighbor query", graph.ErrInvalid)
	}
	switch q.Direction {
	case graph.Outgoing, graph.Incoming, graph.Both:
	default:
		return graph.NeighborPage{}, fmt.Errorf("%w: direction %q", graph.ErrInvalid, q.Direction)
	}
	limit := s.pageLimit(q.Limit)
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen, live, err := s.generationFor(ctx, t, q.RepositoryID, q.Generation)
	if err != nil {
		return graph.NeighborPage{}, err
	}
	scope := s.cursors.scope("neighbors", q.RepositoryID, q.NodeID, string(q.Direction), q.EdgeKinds)
	after, err := s.cursors.decode(scope, gen, q.Cursor)
	if err != nil {
		return graph.NeighborPage{}, err
	}
	var edges []graph.Version
	seen := map[string]bool{}
	for _, dir := range []graph.Direction{graph.Outgoing, graph.Incoming} {
		if q.Direction != graph.Both && q.Direction != dir {
			continue
		}
		vs, err := s.edgesTouching(ctx, t, q.RepositoryID, q.NodeID, dir, q.EdgeKinds, gen, live, after, limit+1)
		if err != nil {
			return graph.NeighborPage{}, err
		}
		for _, v := range vs {
			if id := v.Fact.Edge.ID; !seen[id] {
				seen[id] = true
				edges = append(edges, v)
			}
		}
	}
	sort.Slice(edges, func(i, j int) bool { return edges[i].Fact.Edge.ID < edges[j].Fact.Edge.ID })
	page := graph.NeighborPage{Generation: gen}
	if len(edges) > limit {
		edges = edges[:limit]
		page.NextCursor = s.cursors.encode(scope, gen, edges[limit-1].Fact.Edge.ID)
	}
	var far []string
	for _, v := range edges {
		e := v.Fact.Edge
		if e.SourceID == q.NodeID {
			far = append(far, e.TargetID)
		} else {
			far = append(far, e.SourceID)
		}
	}
	nodes, err := readOpenNodes(ctx, t, q.RepositoryID, far, gen, live)
	if err != nil {
		return graph.NeighborPage{}, err
	}
	for i, v := range edges {
		n := graph.Neighbor{Edge: v}
		if node, ok := nodes[far[i]]; ok {
			n.Node = &node
		}
		page.Neighbors = append(page.Neighbors, n)
	}
	return page, nil
}

func (s *Store) edgesTouching(ctx context.Context, t reader, repo, node string, dir graph.Direction, kinds []string, gen, live uint64, after string, limit int) ([]graph.Version, error) {
	index, column := "CGEdgesBySource", "SourceID"
	if dir == graph.Incoming {
		index, column = "CGEdgesByTarget", "TargetID"
	}
	params := map[string]any{"repo": repo, "node": node, "after": after, "gen": int64(gen), "limit": int64(limit)}
	filter := ""
	if len(kinds) > 0 {
		filter = " AND Kind IN UNNEST(@kinds)"
		params["kinds"] = kinds
	}
	it := t.Query(ctx, spanner.Statement{
		SQL:    nullFilteredHint + "SELECT " + recordSelect + " FROM CGRecords@{FORCE_INDEX=" + index + "} WHERE RepositoryID=@repo AND " + column + " IS NOT NULL AND " + column + "=@node AND RecordID>@after AND " + openPredicate("") + filter + " ORDER BY RecordID LIMIT @limit",
		Params: params,
	})
	return collectVersions(it, live)
}

const idBatch = 500

// readOpenNodes batch-reads the versions of nodes open at gen; absent nodes are
// simply missing from the map.
func readOpenNodes(ctx context.Context, t reader, repo string, ids []string, gen, live uint64) (map[string]graph.Version, error) {
	out := make(map[string]graph.Version, len(ids))
	unique := make([]string, 0, len(ids))
	seen := map[string]bool{}
	for _, id := range ids {
		if !seen[id] {
			seen[id] = true
			unique = append(unique, id)
		}
	}
	for start := 0; start < len(unique); start += idBatch {
		chunk := unique[start:min(start+idBatch, len(unique))]
		it := t.Query(ctx, spanner.Statement{
			SQL:    "SELECT " + recordSelect + " FROM CGRecords WHERE RepositoryID=@repo AND RecordKind='node' AND RecordID IN UNNEST(@ids) AND " + openPredicate(""),
			Params: map[string]any{"repo": repo, "ids": chunk, "gen": int64(gen)},
		})
		vs, err := collectVersions(it, live)
		if err != nil {
			return nil, err
		}
		for _, v := range vs {
			out[v.Fact.Node.ID] = v
		}
	}
	return out, nil
}

// readLatestVersions returns, per id, the version open at gen or, when the
// record is not open there, its most recent version with GenFrom <= gen
// (a retired record's last version). IDs with no version at all are absent.
func readLatestVersions(ctx context.Context, t reader, repo string, kind graph.RecordKind, ids []string, gen, live uint64) (map[string]graph.Version, error) {
	out := make(map[string]graph.Version, len(ids))
	unique := make([]string, 0, len(ids))
	seen := map[string]bool{}
	for _, id := range ids {
		if !seen[id] {
			seen[id] = true
			unique = append(unique, id)
		}
	}
	for start := 0; start < len(unique); start += idBatch {
		chunk := unique[start:min(start+idBatch, len(unique))]
		it := t.Query(ctx, spanner.Statement{
			SQL:    "SELECT " + recordSelect + " FROM CGRecords WHERE RepositoryID=@repo AND RecordKind=@kind AND RecordID IN UNNEST(@ids) AND GenFrom<=@gen",
			Params: map[string]any{"repo": repo, "kind": string(kind), "ids": chunk, "gen": int64(gen)},
		})
		vs, err := collectVersions(it, live)
		if err != nil {
			return nil, err
		}
		for _, v := range vs {
			id := v.Fact.Key().ID
			current, ok := out[id]
			switch {
			case !ok, v.OpenAt(gen) && !current.OpenAt(gen), !current.OpenAt(gen) && v.GenFrom > current.GenFrom:
				out[id] = v
			}
		}
	}
	return out, nil
}

// GetNodes batch-reads the given nodes: the version open at generation, or
// the most recent earlier version for a record that was retired by then.
func (s *Store) GetNodes(ctx context.Context, repo string, ids []string, generation uint64) (map[string]graph.Version, error) {
	return s.getVersions(ctx, repo, graph.RecordNode, ids, generation)
}

// GetEdges is GetNodes for edges.
func (s *Store) GetEdges(ctx context.Context, repo string, ids []string, generation uint64) (map[string]graph.Version, error) {
	return s.getVersions(ctx, repo, graph.RecordEdge, ids, generation)
}

func (s *Store) getVersions(ctx context.Context, repo string, kind graph.RecordKind, ids []string, generation uint64) (map[string]graph.Version, error) {
	if !validRepositoryID(repo) {
		return nil, fmt.Errorf("%w: repository id", graph.ErrInvalid)
	}
	for _, id := range ids {
		if !graph.ValidID(id) {
			return nil, fmt.Errorf("%w: %s id %q", graph.ErrInvalid, kind, id)
		}
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen, live, err := s.generationFor(ctx, t, repo, generation)
	if err != nil {
		return nil, err
	}
	return readLatestVersions(ctx, t, repo, kind, ids, gen, live)
}

// History returns every version of a record oldest first, as seen at the live
// generation: versions above live are omitted and a closure above live is
// presented as still open.
func (s *Store) History(ctx context.Context, repositoryID string, key graph.Key) ([]graph.Version, error) {
	if !validRepositoryID(repositoryID) || !graph.ValidID(key.ID) || (key.Kind != graph.RecordNode && key.Kind != graph.RecordEdge) {
		return nil, fmt.Errorf("%w: history key", graph.ErrInvalid)
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	live, err := liveGeneration(ctx, t, repositoryID)
	if err != nil {
		return nil, err
	}
	it := t.Read(ctx, "CGRecords", spanner.Key{repositoryID, string(key.Kind), key.ID}.AsPrefix(), recordColumns)
	vs, err := collectVersions(it, live)
	if err != nil {
		return nil, err
	}
	out := vs[:0]
	for _, v := range vs {
		if v.GenFrom <= live {
			out = append(out, v)
		}
	}
	return out, nil
}

// RecordsByLineage returns the versions open at a generation for one file lineage.
func (s *Store) RecordsByLineage(ctx context.Context, repositoryID, lineage string, generation uint64) ([]graph.Version, error) {
	if !validRepositoryID(repositoryID) || !graph.ValidID(lineage) {
		return nil, fmt.Errorf("%w: lineage query", graph.ErrInvalid)
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen, live, err := s.generationFor(ctx, t, repositoryID, generation)
	if err != nil {
		return nil, err
	}
	it := t.Query(ctx, spanner.Statement{
		SQL:    nullFilteredHint + "SELECT " + recordSelect + " FROM CGRecords@{FORCE_INDEX=CGRecordsByLineage} WHERE RepositoryID=@repo AND Lineage IS NOT NULL AND Lineage=@lineage AND " + openPredicate("") + " ORDER BY RecordKind, RecordID",
		Params: map[string]any{"repo": repositoryID, "lineage": lineage, "gen": int64(gen)},
	})
	return collectVersions(it, live)
}

// CountOpen counts the nodes and edges open at generation. Unlike the Reader
// it accepts a generation above live so a load can be checked before the flip;
// zero still means live.
func (s *Store) CountOpen(ctx context.Context, repo string, generation uint64) (nodes, edges uint64, err error) {
	if !validRepositoryID(repo) {
		return 0, 0, fmt.Errorf("%w: repository id", graph.ErrInvalid)
	}
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen := generation
	if gen == 0 {
		if gen, err = liveGeneration(ctx, t, repo); err != nil {
			return 0, 0, err
		}
	}
	it := t.Query(ctx, spanner.Statement{
		SQL:    "SELECT RecordKind, COUNT(*) FROM CGRecords@{FORCE_INDEX=CGRecordsByKind} WHERE RepositoryID=@repo AND " + openPredicate("") + " GROUP BY RecordKind",
		Params: map[string]any{"repo": repo, "gen": int64(gen)},
	})
	defer it.Stop()
	for {
		row, err := nextRow(it)
		if err != nil {
			return 0, 0, err
		}
		if row == nil {
			return nodes, edges, nil
		}
		var kind string
		var n int64
		if err = row.Columns(&kind, &n); err != nil {
			return 0, 0, err
		}
		switch graph.RecordKind(kind) {
		case graph.RecordNode:
			nodes = uint64(n)
		case graph.RecordEdge:
			edges = uint64(n)
		}
	}
}
