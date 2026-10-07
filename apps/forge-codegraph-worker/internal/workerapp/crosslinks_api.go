package workerapp

import (
	"context"
	"encoding/json"
	"io"
	"net/http"

	"ei-aitiger-codegraph/agentquery"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

// Cross-repository links join a node of one repository's graph to a node of
// another's: the method that calls an API to the handler serving it, the one
// that publishes an event to its listener. The Forge admin API keeps them,
// one set per team, and replaces a team's whole set whenever it changes, so
// the set here always matches the admin's; they are stored beside the
// graphs, never in a generation. The admin decides who may draw and read
// them, as for every graph read on this listener.
//
//	PUT /v1/cross-links/{owner}                              the owner's whole set: {"links": [...]}
//	GET /v1/repositories/{repo}/node?node=&generation=       one node
//	GET /v1/repositories/{repo}/cross-links?node=&direction= the links touching a node, followed
//	    to the node at their far end in its own repository's live generation
type crossLinkQueries interface {
	Node(ctx context.Context, repo, id string, generation uint64) (graph.Version, error)
	CrossHops(ctx context.Context, repo, id string, direction graph.Direction) ([]agentquery.CrossHop, error)
	ReplaceCrossLinks(ctx context.Context, owner string, links []graph.CrossLink) error
}

// crossLinkBodyBytes fits an owner's largest set.
const crossLinkBodyBytes = 4 << 20

func (g storeGraph) Node(ctx context.Context, repo, id string, generation uint64) (graph.Version, error) {
	return g.store.GetNode(ctx, repo, id, generation)
}

func (g storeGraph) CrossHops(ctx context.Context, repo, id string, direction graph.Direction) ([]agentquery.CrossHop, error) {
	node, err := g.store.GetNode(ctx, repo, id, 0)
	if err != nil {
		return nil, err
	}
	// The admin decides which far repositories its caller may see.
	return agentquery.CrossHops(ctx, g.store, repo, *node.Fact.Node, direction, nil)
}

func (g storeGraph) ReplaceCrossLinks(ctx context.Context, owner string, links []graph.CrossLink) error {
	return g.store.ReplaceCrossLinks(ctx, owner, links)
}

func (a *workerAPI) registerCrossLinks(mux *http.ServeMux) {
	mux.HandleFunc("PUT /v1/cross-links/{owner}", a.guard(a.replaceCrossLinks))
	mux.HandleFunc("GET /v1/repositories/{repo}/node", a.guard(a.graphNode))
	mux.HandleFunc("GET /v1/repositories/{repo}/cross-links", a.guard(a.crossLinks))
}

type crossLinkSet struct {
	Links []graph.CrossLink `json:"links"`
}

func (a *workerAPI) replaceCrossLinks(r *http.Request) (int, any, error) {
	owner := r.PathValue("owner")
	if !graph.ValidCrossToken(owner) {
		return 0, nil, invalid("owner must be a short token, e.g. team:<id>")
	}
	var in crossLinkSet
	decoder := json.NewDecoder(io.LimitReader(r.Body, crossLinkBodyBytes))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&in); err != nil {
		return 0, nil, invalid("body must be one JSON object: %v", err)
	}
	if decoder.More() {
		return 0, nil, invalid("body must be one JSON object")
	}
	if len(in.Links) > spannerstore.MaxCrossLinks {
		return 0, nil, invalid("at most %d links", spannerstore.MaxCrossLinks)
	}
	if err := a.links.ReplaceCrossLinks(r.Context(), owner, in.Links); err != nil {
		return 0, nil, err
	}
	a.logger.InfoContext(r.Context(), "cross links replaced", "owner", owner, "links", len(in.Links))
	return http.StatusOK, map[string]any{"owner": owner, "links": len(in.Links)}, nil
}

func (a *workerAPI) graphNode(r *http.Request) (int, any, error) {
	node, err := requiredParam(r, "node")
	if err != nil {
		return 0, nil, err
	}
	generation, err := generationParam(r)
	if err != nil {
		return 0, nil, err
	}
	v, err := a.links.Node(r.Context(), r.PathValue("repo"), node, generation)
	if err != nil {
		return 0, nil, err
	}
	return http.StatusOK, v, nil
}

func (a *workerAPI) crossLinks(r *http.Request) (int, any, error) {
	node, err := requiredParam(r, "node")
	if err != nil {
		return 0, nil, err
	}
	direction := graph.Direction(r.URL.Query().Get("direction"))
	switch direction {
	case "":
		direction = graph.Both
	case graph.Both, graph.Incoming, graph.Outgoing:
	default:
		return 0, nil, invalid("direction must be in, out or both")
	}
	hops, err := a.links.CrossHops(r.Context(), r.PathValue("repo"), node, direction)
	if err != nil {
		return 0, nil, err
	}
	if hops == nil {
		hops = []agentquery.CrossHop{}
	}
	return http.StatusOK, map[string]any{"hops": hops}, nil
}
