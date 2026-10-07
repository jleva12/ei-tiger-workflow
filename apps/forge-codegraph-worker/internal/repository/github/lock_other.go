//go:build !(darwin || linux || freebsd || netbsd || openbsd || dragonfly)

package github

import "context"

// Other platforms rely on the in-process mirror semaphore only. Two worker
// processes sharing one volume are not protected from each other there.
func lockFile(ctx context.Context, path string) (func(), error) {
	return func() {}, nil
}
