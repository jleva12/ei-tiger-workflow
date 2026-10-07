package agentquery

import (
	"context"
	"fmt"
	"sort"
	"strings"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

// Root is a declaration whose change is walked, and the kind of change.
type Root struct {
	ID     string `json:"id"`
	Change string `json:"change"`
}

// RootsImpactRequest asks what depends on several declarations, each
// changed in its own way, at Generation (zero means live): the declarations
// a pull request touches, for instance.
type RootsImpactRequest struct {
	RepositoryID  string
	Generation    uint64
	Roots         []Root
	Depth         int
	Limit         int
	IncludeLocals bool
}

// The order kinds of change are walked in: the widest first, so a node two
// roots reach is reported through the one that matters most.
var changeOrder = map[string]int{ChangeRemove: 0, ChangeSignature: 1, ChangeContract: 2, ChangeAny: 3, ChangeBody: 4}

// RootsImpact walks every root by its own kind of change, each adding the
// declarations it overrides or implements, or that override or implement
// it, as Impact does, and merges them into one result with one assessment
// at the generation, as ChangeImpact does: each node once, first reach
// wins. Change and Note list every kind walked.
func RootsImpact(ctx context.Context, st Store, req RootsImpactRequest) (ImpactResult, error) {
	if req.Depth < 0 || req.Depth > maxImpactDepth || req.Limit < 0 || req.Limit > maxImpactLimit || len(req.Roots) > maxRoots {
		return ImpactResult{}, fmt.Errorf("%w: impact depth 0-%d, limit 0-%d and at most %d roots", deployment.ErrInvalidRequest, maxImpactDepth, maxImpactLimit, maxRoots)
	}
	if req.Depth == 0 {
		req.Depth = defaultImpactDepth
	}
	if req.Limit == 0 {
		req.Limit = defaultImpactLimit
	}
	byKind := map[string][]string{}
	profiles := map[string]changeProfile{}
	merged := ImpactResult{Roots: []string{}, Hits: []ImpactHit{}}
	seen := map[string]bool{}
	for _, r := range req.Roots {
		profile, err := profileFor(r.Change)
		if err != nil {
			return ImpactResult{}, err
		}
		if seen[r.ID] {
			continue
		}
		seen[r.ID] = true
		name := changeName(profile)
		profiles[name] = profile
		byKind[name] = append(byKind[name], r.ID)
		merged.Roots = append(merged.Roots, r.ID)
	}
	kinds := make([]string, 0, len(byKind))
	for kind := range byKind {
		kinds = append(kinds, kind)
	}
	sort.Slice(kinds, func(i, j int) bool { return changeOrder[kinds[i]] < changeOrder[kinds[j]] })
	var notes []string
	for _, kind := range kinds {
		profile := profiles[kind]
		roots, err := withDispatch(ctx, st, req.RepositoryID, req.Generation, byKind[kind], profile)
		if err != nil {
			return ImpactResult{}, err
		}
		part, err := impactFrom(ctx, st, req.RepositoryID, req.Generation, roots, profile, req.Depth, req.Limit, req.IncludeLocals)
		if err != nil {
			return ImpactResult{}, err
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
		notes = append(notes, kind+": "+profile.note)
	}
	merged.Change = strings.Join(kinds, ",")
	merged.Note = strings.Join(notes, "; ")
	if err := assess(ctx, st, req.RepositoryID, req.Generation, &merged); err != nil {
		return ImpactResult{}, err
	}
	return merged, nil
}

// withDispatch adds to the roots what they override or implement (upward)
// or what overrides or implements them (downward), as the profile says: a
// caller bound to a base declaration reaches its overrides at runtime.
func withDispatch(ctx context.Context, st Store, repo string, generation uint64, roots []string, profile changeProfile) ([]string, error) {
	out := append([]string(nil), roots...)
	seen := make(map[string]bool, len(roots))
	for _, id := range roots {
		seen[id] = true
	}
	add := func(direction graph.Direction) error {
		refs, _, err := st.DependencyEdges(ctx, repo, roots, direction, dispatchKinds, generation, impactEdgeBudget)
		if err != nil {
			return err
		}
		for _, e := range refs {
			far := e.TargetID
			if direction == graph.Incoming {
				far = e.SourceID
			}
			if far != "" && !seen[far] {
				seen[far] = true
				out = append(out, far)
			}
		}
		return nil
	}
	if profile.upward {
		if err := add(graph.Outgoing); err != nil {
			return nil, err
		}
	}
	if profile.downward {
		if err := add(graph.Incoming); err != nil {
			return nil, err
		}
	}
	return out, nil
}

// FileDeclarations are the declarations of one file at the generation (zero
// means live), in every module and source set that has the path, each with
// its source span: what a change to the file's lines lands on.
func FileDeclarations(ctx context.Context, st Store, repo, path string, generation uint64) ([]graph.Version, error) {
	lineages, err := st.LineagesByPath(ctx, repo, path, generation)
	if err != nil {
		return nil, err
	}
	var out []graph.Version
	for _, lineage := range lineages {
		records, err := st.RecordsByLineage(ctx, repo, lineage, generation)
		if err != nil {
			return nil, err
		}
		for _, v := range records {
			if n := v.Fact.Node; n != nil && n.Source != nil && declarationKinds[n.Kind] {
				out = append(out, v)
			}
		}
	}
	return out, nil
}
