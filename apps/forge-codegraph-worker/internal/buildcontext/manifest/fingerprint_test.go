package manifest

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func TestFingerprintCacheSkipsUnchangedFilesAndRehashesChangedOnes(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	jar := filepath.Join(root, "lib.jar")
	if err := os.WriteFile(jar, []byte("jar bytes"), 0600); err != nil {
		t.Fatal(err)
	}
	cache, err := LoadFingerprintCache(filepath.Join(t.TempDir(), "fingerprints.json"))
	if err != nil {
		t.Fatal(err)
	}
	plain, err := Fingerprint(ctx, root, "lib.jar", bc.InputJAR, bc.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	cached, err := FingerprintCached(ctx, cache, root, "lib.jar", bc.InputJAR, bc.DefaultLimits())
	if err != nil || cached != plain {
		t.Fatalf("cached digest %q differs from %q: %v", cached, plain, err)
	}
	// The second call must not read the file: prove it by revoking read access.
	if err = os.Chmod(jar, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(jar, 0600) })
	if _, err = Fingerprint(ctx, root, "lib.jar", bc.InputJAR, bc.DefaultLimits()); err == nil {
		t.Skip("running with privileges that ignore file permissions")
	}
	again, err := FingerprintCached(ctx, cache, root, "lib.jar", bc.InputJAR, bc.DefaultLimits())
	if err != nil || again != plain {
		t.Fatalf("unchanged file was read again: %q %v", again, err)
	}
	// A changed modification time invalidates the entry; a changed size too.
	if err = os.Chmod(jar, 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(jar, []byte("jar bytes"), 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.Chtimes(jar, time.Now(), time.Now().Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	if err = os.Chmod(jar, 0); err != nil {
		t.Fatal(err)
	}
	if _, err = FingerprintCached(ctx, cache, root, "lib.jar", bc.InputJAR, bc.DefaultLimits()); err == nil {
		t.Fatal("changed modification time reused a remembered digest")
	}
	if err = os.Chmod(jar, 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(jar, []byte("different jar bytes"), 0600); err != nil {
		t.Fatal(err)
	}
	changed, err := FingerprintCached(ctx, cache, root, "lib.jar", bc.InputJAR, bc.DefaultLimits())
	if err != nil || changed == plain {
		t.Fatalf("changed content kept the old digest: %v", err)
	}
	if cache.Len() != 1 {
		t.Fatalf("expected one remembered file, got %d", cache.Len())
	}
}

func TestFingerprintCacheTreeDigestMatchesUncachedAndPersists(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	for name, body := range map[string]string{"classes/a/A.class": "A", "classes/b/B.class": "BB", "classes/c.txt": ""} {
		full := filepath.Join(root, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(full), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
	}
	path := filepath.Join(t.TempDir(), "nested", "fingerprints.json")
	cache, err := LoadFingerprintCache(path)
	if err != nil {
		t.Fatal(err)
	}
	plain, err := Fingerprint(ctx, root, "classes", bc.InputClasses, bc.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	cached, err := FingerprintCached(ctx, cache, root, "classes", bc.InputClasses, bc.DefaultLimits())
	if err != nil || cached != plain {
		t.Fatalf("tree digest with cache %q differs from %q: %v", cached, plain, err)
	}
	if cache.Len() != 3 {
		t.Fatalf("expected three remembered files, got %d", cache.Len())
	}
	if err = cache.Save(); err != nil {
		t.Fatal(err)
	}
	reloaded, err := LoadFingerprintCache(path)
	if err != nil {
		t.Fatal(err)
	}
	if reloaded.Len() != 3 {
		t.Fatalf("expected three persisted files, got %d", reloaded.Len())
	}
	if err = os.Chmod(filepath.Join(root, "classes", "b", "B.class"), 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(filepath.Join(root, "classes", "b", "B.class"), 0600) })
	if _, err = Fingerprint(ctx, root, "classes", bc.InputClasses, bc.DefaultLimits()); err == nil {
		t.Skip("running with privileges that ignore file permissions")
	}
	again, err := FingerprintCached(ctx, reloaded, root, "classes", bc.InputClasses, bc.DefaultLimits())
	if err != nil || again != plain {
		t.Fatalf("persisted entries were not reused: %q %v", again, err)
	}
	// Save prunes entries for files that disappeared, so the file stays bounded.
	if err = os.Chmod(filepath.Join(root, "classes", "b", "B.class"), 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.RemoveAll(filepath.Join(root, "classes", "a")); err != nil {
		t.Fatal(err)
	}
	pruning, err := LoadFingerprintCache(path)
	if err != nil {
		t.Fatal(err)
	}
	pruning.Remember(filepath.Join(root, "classes", "c.txt"), mustStat(t, filepath.Join(root, "classes", "c.txt")), plain)
	if err = pruning.Save(); err != nil {
		t.Fatal(err)
	}
	final, err := LoadFingerprintCache(path)
	if err != nil {
		t.Fatal(err)
	}
	if final.Len() != 2 {
		t.Fatalf("expected the deleted file to be pruned, got %d entries", final.Len())
	}
	// A corrupt cache file is an empty cache, never an error.
	if err = os.WriteFile(path, []byte("{not json"), 0600); err != nil {
		t.Fatal(err)
	}
	corrupt, err := LoadFingerprintCache(path)
	if err != nil || corrupt.Len() != 0 {
		t.Fatalf("corrupt cache: %d entries, %v", corrupt.Len(), err)
	}
	if _, err = LoadFingerprintCache("relative/fingerprints.json"); err == nil {
		t.Fatal("relative cache path accepted")
	}
	var nilCache *FingerprintCache
	if digest, ok := nilCache.Lookup(root, mustStat(t, filepath.Join(root, "classes", "c.txt"))); ok || digest != "" {
		t.Fatal("nil cache returned a digest")
	}
	if err = nilCache.Save(); err != nil {
		t.Fatal(err)
	}
}

func mustStat(t *testing.T, name string) os.FileInfo {
	t.Helper()
	info, err := os.Stat(name)
	if err != nil {
		t.Fatal(err)
	}
	return info
}
