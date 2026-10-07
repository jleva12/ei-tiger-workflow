package workerapp

import (
	"context"
	"net/http"
	"strconv"
	"strings"

	"ei-aitiger-codegraph/agentquery"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

// The graph API reads a repository's published code graph for the Forge
// admin API's graph explorer, which decides who may read which repository.
// It shares the worker API's listener and token. Every call reads one
// generation: the live one when generation is 0 or absent, so a client pins
// the generation its first answer names for the calls that follow. Node IDs
// travel as query parameters, so no ID's characters can collide with the
// path.
//
//	GET /v1/repositories/{repo}/graph?kind=&generation=&cursor=
//	    a page of nodes, of one kind or any, with the edges among them
//	GET /v1/repositories/{repo}/neighbors?node=&direction=&generation=&limit=&cursor=
//	    a node's edges and the nodes at their other ends
//	GET /v1/repositories/{repo}/symbols?name=|qualified_name=&generation=&limit=
//	    nodes with exactly that name
//	GET /v1/repositories/{repo}/source?node=&generation=&context=
//	    the source a node was read from, with lines of context around it
type graphQueries interface {
	View(context.Context, agentquery.GraphViewRequest) (agentquery.GraphViewResult, error)
	Neighbors(context.Context, graph.NeighborQuery) (graph.NeighborPage, error)
	Symbols(ctx context.Context, repo, name, qualifiedName string, generation uint64, limit int) ([]graph.Version, error)
	Source(context.Context, agentquery.SourceRequest) (agentquery.SourceResult, error)
}

// storeGraph answers graph queries from the Spanner store.
type storeGraph struct{ store *spannerstore.Store }

func (g storeGraph) View(ctx context.Context, q agentquery.GraphViewRequest) (agentquery.GraphViewResult, error) {
	return agentquery.GraphView(ctx, g.store, q)
}

func (g storeGraph) Neighbors(ctx context.Context, q graph.NeighborQuery) (graph.NeighborPage, error) {
	return g.store.Neighbors(ctx, q)
}

func (g storeGraph) Symbols(ctx context.Context, repo, name, qualifiedName string, generation uint64, limit int) ([]graph.Version, error) {
	return g.store.FindNodes(ctx, repo, name, qualifiedName, nil, generation, limit)
}

func (g storeGraph) Source(ctx context.Context, q agentquery.SourceRequest) (agentquery.SourceResult, error) {
	return agentquery.NodeSource(ctx, g.store, q)
}

func (a *workerAPI) registerGraph(mux *http.ServeMux) {
	mux.HandleFunc("GET /v1/repositories/{repo}/graph", a.guard(a.graphView))
	mux.HandleFunc("GET /v1/repositories/{repo}/neighbors", a.guard(a.graphNeighbors))
	mux.HandleFunc("GET /v1/repositories/{repo}/symbols", a.guard(a.graphSymbols))
	mux.HandleFunc("GET /v1/repositories/{repo}/source", a.guard(a.graphSource))
}

func (a *workerAPI) graphView(r *http.Request) (int, any, error) {
	generation, err := generationParam(r)
	if err != nil {
		return 0, nil, err
	}
	q := r.URL.Query()
	view, err := a.graph.View(r.Context(), agentquery.GraphViewRequest{
		RepositoryID: r.PathValue("repo"), Generation: generation,
		Kind: q.Get("kind"), Cursor: q.Get("cursor"),
	})
	if err != nil {
		return 0, nil, err
	}
	return http.StatusOK, view, nil
}

func (a *workerAPI) graphNeighbors(r *http.Request) (int, any, error) {
	node, err := requiredParam(r, "node")
	if err != nil {
		return 0, nil, err
	}
	generation, err := generationParam(r)
	if err != nil {
		return 0, nil, err
	}
	limit, err := intParam(r, "limit", 40, 1, 200)
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
	page, err := a.graph.Neighbors(r.Context(), graph.NeighborQuery{
		RepositoryID: r.PathValue("repo"), NodeID: node, Direction: direction,
		Generation: generation, Limit: limit, Cursor: r.URL.Query().Get("cursor"),
	})
	if err != nil {
		return 0, nil, err
	}
	return http.StatusOK, page, nil
}

func (a *workerAPI) graphSymbols(r *http.Request) (int, any, error) {
	q := r.URL.Query()
	name, qualified := strings.TrimSpace(q.Get("name")), strings.TrimSpace(q.Get("qualified_name"))
	if (name == "") == (qualified == "") {
		return 0, nil, invalid("give exactly one of name and qualified_name")
	}
	generation, err := generationParam(r)
	if err != nil {
		return 0, nil, err
	}
	limit, err := intParam(r, "limit", 20, 1, 200)
	if err != nil {
		return 0, nil, err
	}
	nodes, err := a.graph.Symbols(r.Context(), r.PathValue("repo"), name, qualified, generation, limit)
	if err != nil {
		return 0, nil, err
	}
	if nodes == nil {
		nodes = []graph.Version{}
	}
	return http.StatusOK, map[string]any{"nodes": nodes}, nil
}

func (a *workerAPI) graphSource(r *http.Request) (int, any, error) {
	node, err := requiredParam(r, "node")
	if err != nil {
		return 0, nil, err
	}
	generation, err := generationParam(r)
	if err != nil {
		return 0, nil, err
	}
	lines, err := intParam(r, "context", 3, 0, 200)
	if err != nil {
		return 0, nil, err
	}
	source, err := a.graph.Source(r.Context(), agentquery.SourceRequest{
		RepositoryID: r.PathValue("repo"), NodeID: node, Generation: generation, ContextLines: lines,
	})
	if err != nil {
		return 0, nil, err
	}
	return http.StatusOK, source, nil
}

func requiredParam(r *http.Request, name string) (string, error) {
	value := r.URL.Query().Get(name)
	if value == "" || len(value) > 1024 {
		return "", invalid("%s is required", name)
	}
	return value, nil
}

// generationParam reads the generation; 0 or absent means the live one.
func generationParam(r *http.Request) (uint64, error) {
	raw := r.URL.Query().Get("generation")
	if raw == "" {
		return 0, nil
	}
	n, err := strconv.ParseUint(raw, 10, 63)
	if err != nil {
		return 0, invalid("generation must be a whole number")
	}
	return n, nil
}

func intParam(r *http.Request, name string, fallback, least, most int) (int, error) {
	raw := r.URL.Query().Get(name)
	if raw == "" {
		return fallback, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < least || n > most {
		return 0, invalid("%s must be %d-%d", name, least, most)
	}
	return n, nil
}
