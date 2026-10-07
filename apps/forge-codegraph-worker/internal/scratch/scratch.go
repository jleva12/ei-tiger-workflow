// Package scratch removes what crashed runs leave behind. A run deletes its
// own temporary directories when it ends; a process that is killed cannot,
// and on a shared volume the leftovers accumulate until the disk is full.
package scratch

import (
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Stale is how old a leftover must be before it is removed: far longer than
// any run may last, so a directory a live run on another process sharing
// the volume still uses is never touched.
const Stale = 24 * time.Hour

// Sweep removes the entries of dir whose names start with prefix (every
// entry when prefix is empty) and that were last modified more than
// olderThan ago. It never follows symlinks and reports how many it removed;
// a directory that cannot be read or removed is logged and passed over.
func Sweep(dir, prefix string, olderThan time.Duration) int {
	entries, err := os.ReadDir(dir)
	if err != nil {
		if !os.IsNotExist(err) {
			slog.Warn("could not read a scratch directory to sweep it", "dir", dir, "error", err)
		}
		return 0
	}
	cutoff := time.Now().Add(-olderThan)
	removed := 0
	for _, entry := range entries {
		if !strings.HasPrefix(entry.Name(), prefix) || entry.Type()&os.ModeSymlink != 0 {
			continue
		}
		info, err := entry.Info()
		if err != nil || info.ModTime().After(cutoff) {
			continue
		}
		path := filepath.Join(dir, entry.Name())
		if err := os.RemoveAll(path); err != nil {
			slog.Warn("could not remove a stale scratch directory", "path", path, "error", err)
			continue
		}
		removed++
	}
	if removed > 0 {
		slog.Info("removed scratch left by earlier runs", "dir", dir, "prefix", prefix, "removed", removed)
	}
	return removed
}
