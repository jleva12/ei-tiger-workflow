package workerapp

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

// The audit API reports how a repository's ingestion went, for the Forge
// admin API's ingestion overview. It shares the worker API's listener and
// token.
//
//	GET /v1/repositories/{repo}/stats
//	    totals of the live graph: nodes and edges by kind, searchable nodes
//	    and their embeddings for the model this worker indexes with
//	GET /v1/repositories/{repo}/runs?limit=&cursor=
//	    the repository's runs, newest first
type auditQueries interface {
	Stats(ctx context.Context, repo string) (spannerstore.RepositoryStats, error)
	Runs(ctx context.Context, repo string, limit int, cursor string) ([]deployment.Run, string, error)
}

// storeAudit answers audit reads from the Spanner store. Embeddings are
// counted for the model and dimensions the pipeline's index step embeds
// with; an empty model means this worker embeds nothing.
type storeAudit struct {
	store      *spannerstore.Store
	model      string
	dimensions int
}

// newStoreAudit counts embeddings for embedder, which is nil when embeddings
// are off.
func newStoreAudit(store *spannerstore.Store, embedder codesearch.Provider) storeAudit {
	a := storeAudit{store: store}
	if embedder != nil {
		a.model, a.dimensions = embedder.Model(), embedder.Dimensions()
	}
	return a
}

func (a storeAudit) Stats(ctx context.Context, repo string) (spannerstore.RepositoryStats, error) {
	return a.store.Stats(ctx, repo, a.model, a.dimensions)
}

func (a storeAudit) Runs(ctx context.Context, repo string, limit int, cursor string) ([]deployment.Run, string, error) {
	return a.store.ListRuns(ctx, repo, limit, cursor)
}

type runPage struct {
	Runs       []deployment.Run `json:"runs"`
	NextCursor string           `json:"next_cursor,omitempty"`
}

func (a *workerAPI) registerAudit(mux *http.ServeMux) {
	mux.HandleFunc("GET /v1/repositories/{repo}/stats", a.guard(a.repositoryStats))
	mux.HandleFunc("GET /v1/repositories/{repo}/runs", a.guard(a.repositoryRuns))
}

func (a *workerAPI) repositoryStats(r *http.Request) (int, any, error) {
	stats, err := a.audit.Stats(r.Context(), r.PathValue("repo"))
	if err != nil {
		return 0, nil, err
	}
	return http.StatusOK, stats, nil
}

func (a *workerAPI) repositoryRuns(r *http.Request) (int, any, error) {
	limit, err := intParam(r, "limit", 10, 1, 50)
	if err != nil {
		return 0, nil, err
	}
	repo, cursor := r.PathValue("repo"), r.URL.Query().Get("cursor")
	runs, next, err := a.audit.Runs(r.Context(), repo, limit, cursor)
	if errors.Is(err, deployment.ErrLimitExceeded) {
		return 0, nil, invalid("cursor is too long")
	}
	if err != nil {
		return 0, nil, err
	}
	if len(runs) == 0 && cursor == "" {
		// No runs at all: tell a repository without any from one never
		// registered.
		if _, err = a.store.GetRepository(r.Context(), repo); errors.Is(err, deployment.ErrNotFound) {
			return 0, nil, fmt.Errorf("%w: repository %s", graph.ErrNotFound, repo)
		} else if err != nil {
			return 0, nil, err
		}
	}
	if runs == nil {
		runs = []deployment.Run{}
	}
	return http.StatusOK, runPage{Runs: runs, NextCursor: next}, nil
}
