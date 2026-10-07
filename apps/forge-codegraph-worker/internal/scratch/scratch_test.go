package scratch

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestSweepRemovesOnlyOldMatchingEntries(t *testing.T) {
	dir := t.TempDir()
	old := time.Now().Add(-2 * Stale)
	for _, name := range []string{"javac-old", "javac-new", "other-old"} {
		if err := os.MkdirAll(filepath.Join(dir, name, "inner"), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{"javac-old", "other-old"} {
		if err := os.Chtimes(filepath.Join(dir, name), old, old); err != nil {
			t.Fatal(err)
		}
	}
	target := t.TempDir()
	if err := os.Symlink(target, filepath.Join(dir, "javac-link")); err != nil {
		t.Fatal(err)
	}
	if removed := Sweep(dir, "javac-", Stale); removed != 1 {
		t.Fatalf("removed %d", removed)
	}
	for name, want := range map[string]bool{"javac-old": false, "javac-new": true, "other-old": true, "javac-link": true} {
		if _, err := os.Lstat(filepath.Join(dir, name)); (err == nil) != want {
			t.Fatalf("%s present = %v", name, err == nil)
		}
	}
	if Sweep(filepath.Join(dir, "absent"), "", Stale) != 0 {
		t.Fatal("an absent directory has nothing to sweep")
	}
}
