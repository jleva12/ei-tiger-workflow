//go:build darwin || linux || freebsd || netbsd || openbsd || dragonfly

package github

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"
)

func TestLockFileExcludesSecondHolder(t *testing.T) {
	path := filepath.Join(t.TempDir(), "mirror.git.lock")
	release, err := lockFile(context.Background(), path)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*lockPollInterval)
	defer cancel()
	if _, err := lockFile(ctx, path); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("second holder acquired a held lock: %v", err)
	}
	release()
	ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	second, err := lockFile(ctx, path)
	if err != nil {
		t.Fatalf("lock not released: %v", err)
	}
	second()
}
