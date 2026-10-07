package agentquery

import (
	"context"
	"fmt"
	"strings"

	"ei-aitiger-codegraph/pkg/deployment"
)

const (
	defaultHubs = 20
	maxHubs     = 200
)

// HubsRequest asks for the most depended-on nodes of a repository: the
// nodes with the most incoming edges of EdgeKinds (semantic kinds when
// empty), optionally only nodes of NodeKinds. Without NodeKinds, symbols
// the repository does not declare (external symbols, intrinsics,
// constructed types, unresolved references) are left out, since String and
// void top every Java repository; IncludeExternal keeps them.
type HubsRequest struct {
	RepositoryID    string
	Generation      uint64
	EdgeKinds       []string
	NodeKinds       []string
	Limit           int
	IncludeExternal bool
}

// declaredKind reports whether a node kind is something the repository
// itself declares, as opposed to a symbol it only refers to.
func declaredKind(kind string) bool {
	for _, prefix := range []string{"external_symbol", "intrinsic", "constructed_type", "unresolved_reference", "derived_"} {
		if strings.HasPrefix(kind, prefix) {
			return false
		}
	}
	return true
}

// HubEntry is one hub with its in-degree and the breakdown by edge kind.
type HubEntry struct {
	Brief
	InDegree int64            `json:"in_degree"`
	ByKind   map[string]int64 `json:"by_kind"`
}

// HubsResult lists hubs in descending in-degree.
type HubsResult struct {
	EdgeKinds []string   `json:"edge_kinds"`
	Hubs      []HubEntry `json:"hubs"`
}

// Hubs ranks nodes by incoming edges. With a node-kind filter it ranks more
// candidates than asked for and keeps the matching ones, so a filter over a
// rare kind may return fewer than Limit.
func Hubs(ctx context.Context, st Store, req HubsRequest) (HubsResult, error) {
	if req.Limit < 0 || req.Limit > maxHubs {
		return HubsResult{}, fmt.Errorf("%w: hubs limit 0-%d", deployment.ErrInvalidRequest, maxHubs)
	}
	if req.Limit == 0 {
		req.Limit = defaultHubs
	}
	// The aggregation costs the same whatever the limit, so rank the full
	// candidate window whenever a filter will drop some of it.
	fetch := req.Limit
	if len(req.NodeKinds) > 0 || !req.IncludeExternal {
		fetch = maxHubs
	}
	hubs, err := st.Hubs(ctx, req.RepositoryID, req.Generation, req.EdgeKinds, fetch)
	if err != nil {
		return HubsResult{}, err
	}
	ids := make([]string, 0, len(hubs))
	for _, h := range hubs {
		ids = append(ids, h.NodeID)
	}
	nodes, err := st.GetNodes(ctx, req.RepositoryID, ids, req.Generation)
	if err != nil {
		return HubsResult{}, err
	}
	keep := map[string]bool{}
	for _, k := range req.NodeKinds {
		keep[k] = true
	}
	result := HubsResult{EdgeKinds: req.EdgeKinds, Hubs: []HubEntry{}}
	for _, h := range hubs {
		v, ok := nodes[h.NodeID]
		if !ok || v.Fact.Node == nil {
			continue
		}
		if len(keep) > 0 && !keep[v.Fact.Node.Kind] {
			continue
		}
		if len(keep) == 0 && !req.IncludeExternal && !declaredKind(v.Fact.Node.Kind) {
			continue
		}
		result.Hubs = append(result.Hubs, HubEntry{Brief: Briefly(*v.Fact.Node), InDegree: h.InDegree, ByKind: h.ByKind})
		if len(result.Hubs) == req.Limit {
			break
		}
	}
	return result, nil
}
