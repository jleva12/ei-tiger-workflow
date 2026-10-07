package agentquery

import (
	"context"
	"fmt"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

const (
	defaultChanges = 200
	maxChanges     = 2000
	changesPage    = 500
	defaultRoots   = 500
	maxRoots       = 2000
)

// declarationKinds are the node kinds a change set reports by default: what
// a developer would recognize as a changed declaration, not occurrences,
// chunks or the symbols a repository merely refers to.
var declarationKinds = map[string]bool{"class": true, "interface": true, "enum": true, "record": true, "annotation_type": true, "type_alias": true, "namespace": true, "method": true, "constructor": true, "function": true, "field": true, "variable": true, "enum_constant": true, "initializer": true}

// ChangesRequest asks what a published generation changed. Generation zero
// means live. Kinds filters node kinds; empty means declarations.
type ChangesRequest struct {
	RepositoryID string
	Generation   uint64
	Kinds        []string
	Limit        int
	Cursor       string
}

// ChangedNode is one changed declaration in brief with what happened to it.
type ChangedNode struct {
	Brief
	Op string `json:"op"`
}

// ChangeSet is a page of a generation's changed declarations with the
// generation's totals over all node records.
type ChangeSet struct {
	Generation uint64        `json:"generation"`
	Commit     string        `json:"commit,omitempty"`
	Added      int64         `json:"added"`
	Updated    int64         `json:"updated"`
	Retired    int64         `json:"retired"`
	Nodes      []ChangedNode `json:"nodes"`
	NextCursor string        `json:"next_cursor,omitempty"`
}

// Changes pages through the generation's node changes and keeps the kinds
// asked for. It returns at least Limit matching nodes when more exist, and
// possibly a few more from the last page read.
func Changes(ctx context.Context, st Store, req ChangesRequest) (ChangeSet, error) {
	if req.Limit < 0 || req.Limit > maxChanges {
		return ChangeSet{}, fmt.Errorf("%w: changes limit 0-%d", deployment.ErrInvalidRequest, maxChanges)
	}
	if req.Limit == 0 {
		req.Limit = defaultChanges
	}
	keep := map[string]bool{}
	for _, k := range req.Kinds {
		keep[k] = true
	}
	if len(keep) == 0 {
		keep = declarationKinds
	}
	out := ChangeSet{Nodes: []ChangedNode{}}
	cursor := req.Cursor
	for {
		page, err := st.Changes(ctx, req.RepositoryID, req.Generation, graph.RecordNode, changesPage, cursor)
		if err != nil {
			return ChangeSet{}, err
		}
		out.Generation, out.Added, out.Updated, out.Retired = page.Generation, page.Added, page.Updated, page.Retired
		if out.Commit == "" {
			out.Commit = page.Commit
		}
		for _, c := range page.Changes {
			n := c.Version.Fact.Node
			if n == nil || !keep[n.Kind] {
				continue
			}
			out.Nodes = append(out.Nodes, ChangedNode{Brief: Briefly(*n), Op: string(c.Op)})
		}
		out.NextCursor = page.NextCursor
		if page.NextCursor == "" || len(out.Nodes) >= req.Limit {
			return out, nil
		}
		cursor = page.NextCursor
	}
}

// ChangeImpactRequest asks what a published generation's changes affect:
// every changed declaration becomes a root of one impact walk (retired
// declarations are walked at the generation before, where their dependants
// still exist). MaxRoots bounds how many changed declarations are used.
type ChangeImpactRequest struct {
	RepositoryID  string
	Generation    uint64
	Change        string
	Depth         int
	Limit         int
	MaxRoots      int
	IncludeLocals bool
}

// ChangeImpactResult is the merged impact of a generation's changes.
type ChangeImpactResult struct {
	Generation     uint64        `json:"generation"`
	Commit         string        `json:"commit,omitempty"`
	Changed        []ChangedNode `json:"changed"`
	RootsTruncated bool          `json:"roots_truncated"`
	Impact         ImpactResult  `json:"impact"`
}

// CompactChangeImpactResult is ChangeImpactResult with a compact impact.
type CompactChangeImpactResult struct {
	Generation     uint64              `json:"generation"`
	Commit         string              `json:"commit,omitempty"`
	Changed        []ChangedNode       `json:"changed"`
	RootsTruncated bool                `json:"roots_truncated"`
	Impact         CompactImpactResult `json:"impact"`
}

// ChangeImpact reads the generation's changed declarations, walks the
// dependants of the added and updated ones at the generation and of the
// retired ones at the generation before, merges the hits (each node once,
// first reach wins) and assesses the result at the generation.
func ChangeImpact(ctx context.Context, st Store, req ChangeImpactRequest) (ChangeImpactResult, error) {
	if req.Depth < 0 || req.Depth > maxImpactDepth || req.Limit < 0 || req.Limit > maxImpactLimit || req.MaxRoots < 0 || req.MaxRoots > maxRoots {
		return ChangeImpactResult{}, fmt.Errorf("%w: change impact depth 0-%d, limit 0-%d and max_roots 0-%d", deployment.ErrInvalidRequest, maxImpactDepth, maxImpactLimit, maxRoots)
	}
	profile, err := profileFor(req.Change)
	if err != nil {
		return ChangeImpactResult{}, err
	}
	if req.Depth == 0 {
		req.Depth = defaultImpactDepth
	}
	if req.Limit == 0 {
		req.Limit = defaultImpactLimit
	}
	if req.MaxRoots == 0 {
		req.MaxRoots = defaultRoots
	}
	changes, err := Changes(ctx, st, ChangesRequest{RepositoryID: req.RepositoryID, Generation: req.Generation, Limit: req.MaxRoots})
	if err != nil {
		return ChangeImpactResult{}, err
	}
	result := ChangeImpactResult{Generation: changes.Generation, Commit: changes.Commit, Changed: changes.Nodes}
	if len(result.Changed) > req.MaxRoots {
		result.Changed = result.Changed[:req.MaxRoots]
		result.RootsTruncated = true
	}
	if changes.NextCursor != "" {
		result.RootsTruncated = true
	}
	var live, gone []string
	for _, c := range result.Changed {
		if c.Op == string(spannerstore.ChangeRetired) {
			gone = append(gone, c.ID)
		} else {
			live = append(live, c.ID)
		}
	}
	merged := ImpactResult{Roots: append(append([]string{}, live...), gone...), Change: changeName(profile), Note: profile.note, Hits: []ImpactHit{}}
	seen := map[string]bool{}
	for _, id := range merged.Roots {
		seen[id] = true
	}
	walk := func(roots []string, generation uint64) error {
		if len(roots) == 0 {
			return nil
		}
		part, err := impactFrom(ctx, st, req.RepositoryID, generation, roots, profile, req.Depth, req.Limit, req.IncludeLocals)
		if err != nil {
			return err
		}
		merged.Edges += part.Edges
		merged.Skipped += part.Skipped
		merged.Truncated = merged.Truncated || part.Truncated
		for _, h := range part.Hits {
			id := h.Node.Fact.Key().ID
			if seen[id] {
				continue
			}
			if len(merged.Hits) >= req.Limit {
				merged.Truncated = true
				break
			}
			seen[id] = true
			merged.Hits = append(merged.Hits, h)
		}
		return nil
	}
	if err := walk(live, changes.Generation); err != nil {
		return ChangeImpactResult{}, err
	}
	if changes.Generation > 1 {
		if err := walk(gone, changes.Generation-1); err != nil {
			return ChangeImpactResult{}, err
		}
	}
	if err := assess(ctx, st, req.RepositoryID, changes.Generation, &merged); err != nil {
		return ChangeImpactResult{}, err
	}
	result.Impact = merged
	return result, nil
}

// CompactChangeImpact projects a change impact result.
func CompactChangeImpact(r ChangeImpactResult) CompactChangeImpactResult {
	return CompactChangeImpactResult{Generation: r.Generation, Commit: r.Commit, Changed: r.Changed, RootsTruncated: r.RootsTruncated, Impact: CompactImpact(r.Impact)}
}
