package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"syscall"
	"testing"
	"time"
)

func TestSuperviseLifecycle(t *testing.T) {
	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))
	for _, mode := range []string{"second-signal", "deadline"} {
		t.Run(mode, func(t *testing.T) {
			signals := make(chan os.Signal, 2)
			started, canceled, release, finished := make(chan struct{}), make(chan struct{}), make(chan struct{}), make(chan struct{})
			defer func() { close(release); <-finished }()
			done := make(chan int, 1)
			timeout := time.Second
			if mode == "deadline" {
				timeout = 30 * time.Millisecond
			}
			go func() {
				done <- supervise(signals, timeout, logger, func(ctx context.Context) error {
					defer close(finished)
					close(started)
					<-ctx.Done()
					close(canceled)
					<-release
					return nil
				})
			}()
			<-started
			signals <- syscall.SIGTERM
			<-canceled
			select {
			case <-done:
				t.Fatal("exited before cleanup")
			default:
			}
			if mode == "second-signal" {
				signals <- os.Interrupt
			}
			select {
			case code := <-done:
				if code != 1 {
					t.Fatalf("forced exit code=%d", code)
				}
			case <-time.After(2 * time.Second):
				t.Fatal("forced exit stuck")
			}
		})
	}
}

func TestSuperviseCleanDrainAndFailures(t *testing.T) {
	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))
	signals := make(chan os.Signal, 2)
	cleaned := false
	code := supervise(signals, time.Second, logger, func(ctx context.Context) error {
		signals <- syscall.SIGTERM
		<-ctx.Done()
		cleaned = true
		return nil
	})
	if code != 0 || !cleaned {
		t.Fatalf("clean drain: code=%d cleaned=%v", code, cleaned)
	}
	for _, fn := range []func(context.Context) error{
		func(context.Context) error { return errors.New("startup failed") },
		func(context.Context) error { return errors.Join(context.Canceled, errors.New("cleanup failed")) },
		func(context.Context) error { panic("bootstrap failure") },
	} {
		if code := supervise(make(chan os.Signal), time.Second, logger, fn); code != 1 {
			t.Fatalf("failure exit code=%d", code)
		}
	}
}

func TestRunRejectsCLIArguments(t *testing.T) {
	if code := run([]string{"worker", "ingest"}, io.Discard); code != 2 {
		t.Fatalf("usage exit code=%d", code)
	}
}
