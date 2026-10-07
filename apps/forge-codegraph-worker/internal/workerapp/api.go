package workerapp

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/worker/internal/repository/github"
	"ei-aitiger-codegraph/worker/internal/retry"
)

// The worker's API lets one trusted caller, such as the Forge admin API,
// read the published graphs, their runs and the ingestion audit, and keep
// cross-repository links. It shares the health listener and is on only when
// CODEGRAPH_ADMISSION_TOKEN is set; every call presents that token as a
// bearer token. The caller decides who may read what.
//
// Ingestions are not submitted here: the Forge admin API queues them in its
// MySQL, where the worker claims them (ingest.go).
//
//	GET  /v1/repositories/{repo}/runs/{run}           a run
//
// The same listener serves graph reads (graph_api.go), cross-repository
// links (crosslinks_api.go), the ingestion audit (audit_api.go) and code
// search (search_api.go).
type workerAPI struct {
	token [sha256.Size]byte
	store apiStore
	// graph reads published graphs; see graph_api.go.
	graph graphQueries
	// links keeps cross-repository links; see crosslinks_api.go.
	links crossLinkQueries
	// audit reads graph totals and run history; see audit_api.go.
	audit auditQueries
	// searcher searches repositories' code; see search_api.go.
	searcher codeSearcher
	logger   *slog.Logger
}

type apiStore interface {
	GetRepository(context.Context, string) (deployment.Repository, error)
	GetRun(context.Context, deployment.RunKey) (deployment.Run, error)
}

// apiTimeout bounds one call.
const apiTimeout = time.Minute

func newWorkerAPI(token string, store apiStore, graph graphQueries, links crossLinkQueries, audit auditQueries, searcher codeSearcher, logger *slog.Logger) *workerAPI {
	return &workerAPI{token: sha256.Sum256([]byte(token)), store: store, graph: graph, links: links, audit: audit, searcher: searcher, logger: logger}
}

func (a *workerAPI) register(mux *http.ServeMux) {
	mux.HandleFunc("GET /v1/repositories/{repo}/runs/{run}", a.guard(a.inspect))
	a.registerGraph(mux)
	a.registerCrossLinks(mux)
	a.registerAudit(mux)
	a.registerSearch(mux)
}

type runDetail struct {
	Run deployment.Run `json:"run"`
}

// apiError is an error with the status and code the caller sees.
type apiError struct {
	status  int
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *apiError) Error() string { return e.Message }

func (a *workerAPI) guard(handle func(*http.Request) (int, any, error)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		given, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
		// Compare digests so the comparison takes the same time for any length.
		if sum := sha256.Sum256([]byte(given)); !ok || subtle.ConstantTimeCompare(sum[:], a.token[:]) != 1 {
			w.Header().Set("WWW-Authenticate", "Bearer")
			writeJSON(w, http.StatusUnauthorized, &apiError{Code: "unauthenticated", Message: "a valid admission token is required"})
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), apiTimeout)
		defer cancel()
		status, body, err := handle(r.WithContext(ctx))
		if err != nil {
			e := a.mapError(ctx, r, err)
			writeJSON(w, e.status, e)
			return
		}
		writeJSON(w, status, body)
	}
}

func (a *workerAPI) inspect(r *http.Request) (int, any, error) {
	run, err := a.store.GetRun(r.Context(), deployment.RunKey{RepositoryID: r.PathValue("repo"), RunID: r.PathValue("run")})
	if err != nil {
		return 0, nil, err
	}
	return http.StatusOK, runDetail{Run: run}, nil
}

// mapError maps a failure to what the caller sees; everything unexpected is
// logged, not shown.
func (a *workerAPI) mapError(ctx context.Context, r *http.Request, err error) *apiError {
	var e *apiError
	switch {
	case errors.As(err, &e):
		return e
	case errors.Is(err, graph.ErrNotFound):
		return &apiError{http.StatusNotFound, "not_found", err.Error()}
	case errors.Is(err, deployment.ErrInvalidRequest), errors.Is(err, github.ErrInvalidArgument), errors.Is(err, graph.ErrInvalid):
		return &apiError{http.StatusBadRequest, "invalid_request", err.Error()}
	case errors.Is(err, deployment.ErrNotFound):
		return &apiError{http.StatusNotFound, "not_found", "run not found"}
	case errors.Is(err, deployment.ErrStaleDeployment), errors.Is(err, deployment.ErrConflictingReplay),
		errors.Is(err, deployment.ErrRepositoryBusy), errors.Is(err, deployment.ErrInvalidTransition):
		return &apiError{http.StatusConflict, "conflict", err.Error()}
	case errors.Is(err, context.DeadlineExceeded):
		return &apiError{http.StatusGatewayTimeout, "timeout", "the request did not finish in time; retry"}
	case errors.Is(err, deployment.ErrUnavailable), retry.Transient(err):
		a.logger.WarnContext(ctx, "worker API call unavailable", "path", r.URL.Path, "error", err)
		return &apiError{http.StatusServiceUnavailable, "unavailable", "a dependency is unavailable; retry"}
	}
	a.logger.ErrorContext(ctx, "worker API call failed", "path", r.URL.Path, "error", err)
	return &apiError{http.StatusInternalServerError, "internal", "the request failed; see the worker logs"}
}

func invalid(format string, args ...any) error {
	return &apiError{http.StatusBadRequest, "invalid_request", fmt.Sprintf(format, args...)}
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
