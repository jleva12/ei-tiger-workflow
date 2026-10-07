//go:build unix

package github

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCancellationStopsHelpers(t *testing.T) {
	root := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, err := commandRunner("/bin/sh")(ctx, root, gitEnvironment(""), "-c", `(sleep 0.3; touch leaked) & touch ready; wait`)
		done <- err
	}()
	deadline := time.After(5 * time.Second)
	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	for {
		if _, err := os.Stat(filepath.Join(root, "ready")); err == nil {
			break
		}
		select {
		case <-deadline:
			t.Fatal("helper did not start")
		case err := <-done:
			t.Fatalf("helper exited before cancellation: %v", err)
		case <-ticker.C:
		}
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation lost: %v", err)
	}
	time.Sleep(400 * time.Millisecond)
	if _, err := os.Stat(filepath.Join(root, "leaked")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("helper continued after cancellation: %v", err)
	}
}
