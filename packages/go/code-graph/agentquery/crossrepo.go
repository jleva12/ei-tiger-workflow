package agentquery

import (
	"context"
	"errors"
	"sort"

	"ei-aitiger-codegraph/pkg/graph"
)

// Readable says whether the caller may read a repository. Cross-repository
// links into a repository it can't read are left out, as if they weren't
// there.
type Readable func(repositoryID string) bool

// CrossHop is one cross-repository link followed from a node: the link,
// which way it was followed, and the node at its far end, read in its own
// repository at that repository's live generation (each repository's graph
// is versioned on its own). Node is absent and Stale set when the far node
// is gone: removed, or renamed so neither its ID nor its qualified name is
// there any more.
type CrossHop struct {
	Link         graph.CrossLink `json:"link"`
	Direction    graph.Direction `json:"direction"` // out: the node links to the far one; in: the far one links to it
	RepositoryID string          `json:"repository_id"`
	Node         *graph.Version  `json:"node,omitempty"`
	Stale        bool            `json:"stale,omitempty"`
}

// CrossHops follows the cross-repository links touching a node in repo, in
// direction, into the repositories the caller can read. A link finds the
// node by its ID, or by its qualified name when an ingestion gave it a new
// ID.
func CrossHops(ctx context.Context, st Store, repo string, node graph.Node, direction graph.Direction, readable Readable) ([]CrossHop, error) {
	q := graph.CrossLinkQuery{RepositoryID: repo, Direction: direction, NodeIDs: []string{node.ID}}
	if node.QualifiedName != "" {
		q.QualifiedNames = []string{node.QualifiedName}
	}
	links, err := st.CrossLinks(ctx, q)
	if err != nil {
		return nil, err
	}
	hops := []CrossHop{}
	for _, l := range links {
		for _, dir := range []graph.Direction{graph.Outgoing, graph.Incoming} {
			if (direction != graph.Both && direction != dir) || !q.Matches(l, dir) {
				continue
			}
			far := l.Far(dir)
			if readable != nil && !readable(far.RepositoryID) {
				continue
			}
			v, err := resolveEnd(ctx, st, far)
			if err != nil {
				return nil, err
			}
			hops = append(hops, CrossHop{Link: l, Direction: dir, RepositoryID: far.RepositoryID, Node: v, Stale: v == nil})
		}
	}
	return hops, nil
}

// resolveEnd reads a link's end at its repository's live generation: the
// node by ID, else the one node of its kind with its qualified name. It is
// nil when neither is there, including when the repository is gone.
func resolveEnd(ctx context.Context, st Store, end graph.CrossEnd) (*graph.Version, error) {
	nodes, err := st.GetNodes(ctx, end.RepositoryID, []string{end.NodeID}, 0)
	if errors.Is(err, graph.ErrNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	// GetNodes returns a retired node's last version: that isn't the node now.
	if v, ok := nodes[end.NodeID]; ok && v.GenTo == 0 && !v.Retired {
		return &v, nil
	}
	if end.QualifiedName == "" {
		return nil, nil
	}
	var kinds []string
	if end.Kind != "" {
		kinds = []string{end.Kind}
	}
	found, err := st.FindNodes(ctx, end.RepositoryID, "", end.QualifiedName, kinds, 0, 2)
	if errors.Is(err, graph.ErrNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if len(found) != 1 {
		return nil, nil
	}
	return &found[0], nil
}

// CompactCrossHop is a CrossHop without records: the far node in brief with
// its repository, the link's kind, label and ID, and which way it points.
type CompactCrossHop struct {
	Brief
	RepositoryID string          `json:"repository_id"`
	Via          string          `json:"via"`
	Direction    graph.Direction `json:"direction"`
	Label        string          `json:"label,omitempty"`
	LinkID       string          `json:"link_id"`
	Stale        bool            `json:"stale,omitempty"`
}

// CompactCrossHops projects hops. A stale hop keeps the far node's ID,
// qualified name and kind as the link recorded them.
func CompactCrossHops(hops []CrossHop) []CompactCrossHop {
	out := make([]CompactCrossHop, 0, len(hops))
	for _, h := range hops {
		c := CompactCrossHop{RepositoryID: h.RepositoryID, Via: h.Link.Kind, Direction: h.Direction, Label: h.Link.Label, LinkID: h.Link.ID, Stale: h.Stale}
		if h.Node != nil && h.Node.Fact.Node != nil {
			c.Brief = Briefly(*h.Node.Fact.Node)
		} else {
			far := h.Link.Far(h.Direction)
			c.Brief = Brief{ID: far.NodeID, Kind: far.Kind, QualifiedName: far.QualifiedName}
		}
		out = append(out, c)
	}
	return out
}

// maxCrossings bounds how many links one impact walk crosses.
const maxCrossings = 100

// ImpactAcross is where a change reaches another repository through a
// cross-repository link: a declaration the walk reached (or a root) that
// code in another repository links to, by calling its API or consuming its
// events, makes that code impacted too, and the walk goes on there with the
// depth left, at that repository's live generation, following callers and
// references as for a body change. From is the declaration crossed from, in
// FromRepositoryID; Depth the hop at which the link was crossed, counting
// the link as one; Hits the linking code's dependants there, their depths
// counted from the root. Node is absent and Stale set when the linking
// declaration is gone.
type ImpactAcross struct {
	RepositoryID     string          `json:"repository_id"`
	Link             graph.CrossLink `json:"link"`
	From             string          `json:"from"`
	FromRepositoryID string          `json:"from_repository_id"`
	Depth            int             `json:"depth"`
	Node             *graph.Version  `json:"node,omitempty"`
	Stale            bool            `json:"stale,omitempty"`
	Hits             []ImpactHit     `json:"hits"`
}

// reach is one repository's part of an impact walk: the nodes reached there
// and the depth each was reached at, to cross links from.
type reach struct {
	repo       string
	generation uint64
	depths     map[string]int
	names      map[string]string // node id -> qualified name
}

func (r reach) add(v graph.Version, depth int) {
	if n := v.Fact.Node; n != nil {
		r.depths[n.ID] = depth
		if n.QualifiedName != "" {
			r.names[n.ID] = n.QualifiedName
		}
	}
}

// impactAcross continues an impact walk across the cross-repository links
// into the nodes it reached, repository by repository, until the depth, the
// limit or maxCrossings runs out. It returns the crossings and whether it
// stopped short.
func impactAcross(ctx context.Context, st Store, req ImpactRequest, start ImpactResult) ([]ImpactAcross, bool, error) {
	first := reach{repo: req.RepositoryID, generation: req.Generation, depths: map[string]int{}, names: map[string]string{}}
	roots, err := st.GetNodes(ctx, req.RepositoryID, start.Roots, req.Generation)
	if err != nil {
		return nil, false, err
	}
	for _, v := range roots {
		first.add(v, 0)
	}
	for _, h := range start.Hits {
		first.add(h.Node, h.Depth)
	}
	visited := map[string]bool{}
	key := func(repo, id string) string { return repo + "\x00" + id }
	for id := range first.depths {
		visited[key(first.repo, id)] = true
	}
	body, _ := profileFor(ChangeBody)
	left := req.Limit - len(start.Hits)
	var across []ImpactAcross
	truncated := false
	queue := []reach{first}
	for len(queue) > 0 && !truncated {
		here := queue[0]
		queue = queue[1:]
		q := graph.CrossLinkQuery{RepositoryID: here.repo, Direction: graph.Incoming}
		for id := range here.depths {
			q.NodeIDs = append(q.NodeIDs, id)
		}
		for _, name := range here.names {
			q.QualifiedNames = append(q.QualifiedNames, name)
		}
		sort.Strings(q.NodeIDs)
		sort.Strings(q.QualifiedNames)
		links, err := st.CrossLinks(ctx, q)
		if err != nil {
			return nil, false, err
		}
		for _, l := range links {
			from, depth, ok := here.reached(l.Target)
			if !ok || depth+1 > req.Depth {
				continue
			}
			far := l.Source
			if !req.Across(far.RepositoryID) || visited[key(far.RepositoryID, far.NodeID)] {
				continue
			}
			if len(across) >= maxCrossings || left <= 0 {
				truncated = true
				break
			}
			visited[key(far.RepositoryID, far.NodeID)] = true
			crossing := ImpactAcross{RepositoryID: far.RepositoryID, Link: l, From: from, FromRepositoryID: here.repo, Depth: depth + 1, Hits: []ImpactHit{}}
			v, err := resolveEnd(ctx, st, far)
			if err != nil {
				return nil, false, err
			}
			if v == nil {
				crossing.Stale = true
				across = append(across, crossing)
				continue
			}
			crossing.Node = v
			id := v.Fact.Node.ID
			visited[key(far.RepositoryID, id)] = true
			left--
			next := reach{repo: far.RepositoryID, depths: map[string]int{}, names: map[string]string{}}
			next.add(*v, crossing.Depth)
			if rest := req.Depth - crossing.Depth; rest > 0 && left > 0 {
				walkRoots, err := withDispatch(ctx, st, far.RepositoryID, 0, []string{id}, body)
				if err != nil {
					return nil, false, err
				}
				walk, err := impactFrom(ctx, st, far.RepositoryID, 0, walkRoots, body, rest, left, req.IncludeLocals)
				if err != nil {
					return nil, false, err
				}
				truncated = truncated || walk.Truncated
				for _, h := range walk.Hits {
					if visited[key(far.RepositoryID, h.Node.Fact.Node.ID)] {
						continue
					}
					visited[key(far.RepositoryID, h.Node.Fact.Node.ID)] = true
					h.Depth += crossing.Depth
					crossing.Hits = append(crossing.Hits, h)
					next.add(h.Node, h.Depth)
					left--
				}
			}
			across = append(across, crossing)
			queue = append(queue, next)
		}
	}
	return across, truncated, nil
}

// reached finds a link's end among the nodes reached: by ID, or by
// qualified name. It returns the node's ID and depth.
func (r reach) reached(end graph.CrossEnd) (string, int, bool) {
	if end.RepositoryID != r.repo {
		return "", 0, false
	}
	if d, ok := r.depths[end.NodeID]; ok {
		return end.NodeID, d, true
	}
	if end.QualifiedName == "" {
		return "", 0, false
	}
	for id, name := range r.names {
		if name == end.QualifiedName {
			return id, r.depths[id], true
		}
	}
	return "", 0, false
}

// CompactImpactAcross is ImpactAcross without records.
type CompactImpactAcross struct {
	RepositoryID     string        `json:"repository_id"`
	Via              string        `json:"via"`
	Label            string        `json:"label,omitempty"`
	LinkID           string        `json:"link_id"`
	From             string        `json:"from"`
	FromRepositoryID string        `json:"from_repository_id"`
	Depth            int           `json:"depth"`
	Node             Brief         `json:"node"`
	Stale            bool          `json:"stale,omitempty"`
	Nodes            []ImpactBrief `json:"nodes"`
}

func compactAcross(across []ImpactAcross) []CompactImpactAcross {
	if len(across) == 0 {
		return nil
	}
	out := make([]CompactImpactAcross, 0, len(across))
	for _, a := range across {
		c := CompactImpactAcross{RepositoryID: a.RepositoryID, Via: a.Link.Kind, Label: a.Link.Label, LinkID: a.Link.ID, From: a.From, FromRepositoryID: a.FromRepositoryID, Depth: a.Depth, Stale: a.Stale, Nodes: make([]ImpactBrief, 0, len(a.Hits))}
		if a.Node != nil && a.Node.Fact.Node != nil {
			c.Node = Briefly(*a.Node.Fact.Node)
		} else {
			c.Node = Brief{ID: a.Link.Source.NodeID, Kind: a.Link.Source.Kind, QualifiedName: a.Link.Source.QualifiedName}
		}
		for _, h := range a.Hits {
			b := ImpactBrief{Via: h.Via, From: h.From, Depth: h.Depth, Edges: h.Edges, Module: h.Module, Root: h.Root}
			if h.Node.Fact.Node != nil {
				b.Brief = Briefly(*h.Node.Fact.Node)
			}
			c.Nodes = append(c.Nodes, b)
		}
		out = append(out, c)
	}
	return out
}
