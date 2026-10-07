//go:build darwin || linux || freebsd || netbsd || openbsd || dragonfly

package github

import (
	"context"
	"errors"
	"os"
	"syscall"
	"time"
)

const lockPollInterval = 50 * time.Millisecond

// lockFile takes an exclusive advisory flock on path, creating it if needed.
// flock locks belong to the open file description, so goroutines holding
// separate descriptors exclude each other as well as other processes, and
// closing the file releases the lock even if the process dies. The
// non-blocking attempt is polled so the wait honors ctx.
func lockFile(ctx context.Context, path string) (release func(), err error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, err
	}
	for {
		err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			return func() {
				_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
				_ = f.Close()
			}, nil
		}
		if !errors.Is(err, syscall.EWOULDBLOCK) && !errors.Is(err, syscall.EINTR) {
			_ = f.Close()
			return nil, err
		}
		select {
		case <-ctx.Done():
			_ = f.Close()
			return nil, ctx.Err()
		case <-time.After(lockPollInterval):
		}
	}
}
