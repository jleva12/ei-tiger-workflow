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

func (s *healthStatus) handler(api *workerAPI) http.Handler {
	mux := http.NewServeMux()
	if api != nil {
		api.register(mux)
	}
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

// startHealth serves the probes and, when api is set, the worker's API.
func startHealth(ctx context.Context, addr string, status *healthStatus, api *workerAPI, logger *slog.Logger) (*http.Server, <-chan error, error) {
	if addr == "" {
		return nil, nil, nil
	}
	listener, err := (&net.ListenConfig{}).Listen(ctx, "tcp", addr)
	if err != nil {
		return nil, nil, err
	}
	// A graph read or the audit may take far longer than a probe.
	write := 5 * time.Second
	if api != nil {
		write = apiTimeout + 5*time.Second
	}
	server := &http.Server{
		Handler: status.handler(api), ReadHeaderTimeout: 2 * time.Second,
		ReadTimeout: 5 * time.Second, WriteTimeout: write,
		IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8 << 10,
		ErrorLog: slog.NewLogLogger(logger.Handler(), slog.LevelError),
	}
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	logger.Info("worker health listener started", "address", listener.Addr().String(), "api", api != nil)
	return server, done, nil
}
