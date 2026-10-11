package workerapp

import (
	"context"
	"log/slog"
	"net"
	"net/http"
	"sync/atomic"
	"time"
)

type healthStatus struct {
	ready     atomic.Bool
	spannerOK atomic.Bool
	// queueOK is the admin API's MySQL, where the worker claims its jobs.
	queueOK atomic.Bool
}

// handler serves the unauthenticated probes, and nothing else: graph reads
// and search live in the Python package forge_codegraph_query.
func (s *healthStatus) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /livez", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if !s.ready.Load() || !s.spannerOK.Load() || !s.queueOK.Load() {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ready\n"))
	})
	return mux
}

// startHealth serves the probes on addr; an empty addr disables them.
func startHealth(ctx context.Context, addr string, status *healthStatus, logger *slog.Logger) (*http.Server, <-chan error, error) {
	if addr == "" {
		return nil, nil, nil
	}
	listener, err := (&net.ListenConfig{}).Listen(ctx, "tcp", addr)
	if err != nil {
		return nil, nil, err
	}
	server := &http.Server{
		Handler: status.handler(), ReadHeaderTimeout: 2 * time.Second,
		ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second,
		IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8 << 10,
		ErrorLog: slog.NewLogLogger(logger.Handler(), slog.LevelError),
	}
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	logger.Info("worker health listener started", "address", listener.Addr().String())
	return server, done, nil
}
