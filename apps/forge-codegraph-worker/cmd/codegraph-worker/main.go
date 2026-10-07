// codegraph-worker consumes admitted ingestion jobs from Spanner.
package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"runtime/debug"
	"syscall"
	"time"

	"ei-aitiger-codegraph/worker/internal/languages/builtin"
	"ei-aitiger-codegraph/worker/internal/workerapp"
)

// main initializes the application's execution by invoking the run function with command-line arguments and error output.
func main() { os.Exit(run(os.Args, os.Stderr)) }

// run initializes the logger, loads configuration, and supervises the worker lifecycle based on the provided arguments.
func run(args []string, output io.Writer) int {
	level := new(slog.LevelVar)
	logger := slog.New(slog.NewJSONHandler(output, &slog.HandlerOptions{Level: level})).With("service", "codegraph-worker", "pid", os.Getpid())
	if len(args) != 1 {
		logger.Error("codegraph-worker accepts environment configuration only")
		return 2
	}
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)

	config, err := workerapp.LoadConfig()
	if err != nil {
		logger.Error("worker configuration failed", "error", err)
		return 2
	}

	var configuredLevel slog.Level
	_ = configuredLevel.UnmarshalText([]byte(config.Log.Level))
	level.Set(configuredLevel)
	slog.SetDefault(logger)
	if build, ok := debug.ReadBuildInfo(); ok {
		logger = logger.With("go_version", build.GoVersion)
	}

	logger.Info("worker starting", "queue", config.Queue, "task_concurrency", config.TaskConcurrency)
	return supervise(signals, config.ShutdownTimeout+config.CleanupTimeout, logger, func(ctx context.Context) error {
		registry, err := builtin.Registry()
		if err != nil {
			return err
		}

		dependencies, err := workerapp.Bootstrap(config, registry)
		if err != nil {
			return err
		}

		return workerapp.Serve(ctx, config, dependencies, logger)
	})
}

// supervise manages the lifecycle of a worker, handling signals and a shutdown timeout for clean termination.
func supervise(signals <-chan os.Signal, timeout time.Duration, logger *slog.Logger, serve func(context.Context) error) int {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		defer func() {
			if recover() != nil {
				logger.Error("worker bootstrap panicked", "stack", string(debug.Stack()))
				done <- errors.New("worker bootstrap panic")
			}
		}()
		done <- serve(ctx)
	}()
	select {
	case err := <-done:
		if err != nil {
			logger.Error("worker stopped", "error", err)
			return 1
		}
		return 0
	case sig := <-signals:
		logger.Info("shutdown signal received", "signal", sig.String())
		cancel()
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case err := <-done:
		if err != nil && !errors.Is(err, context.Canceled) {
			logger.Error("worker shutdown failed", "error", err)
			return 1
		}
		logger.Info("worker stopped cleanly")
		return 0
	case sig := <-signals:
		logger.Error("forcing worker exit after second signal", "signal", sig.String())
		return 1
	case <-timer.C:
		logger.Error("forcing worker exit after shutdown deadline", "timeout", timeout)
		return 1
	}
}
