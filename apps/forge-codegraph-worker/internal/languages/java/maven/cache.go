package maven

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
)

// Build-context cache layout below CacheDir:
//
//	fingerprints.json                      (path, size, mtime) -> SHA-256 of hashed files
//	buildcontext/<repository>/<key>.json   sealed BuildContext plus its referenced inputs
//	objects/<sha256>.jar                   immutable dependency JAR copies (retainJAR)
//	outputs/<tree-digest>/...              retained checkout output trees (classes,
//	                                       generated sources) named by ei-tree-sha256-v1
//
// <key> fingerprints the build definition: every pom.xml, .mvn/**, mvnw*, the
// Maven executable path, JavaHome, the build JDKs and the provider version. A key only makes
// an entry a candidate. Class directories and generated roots are compiled
// from the checkout's sources, so an entry is reused only for the snapshot it
// was built from, and only after every referenced JAR, JDK and output tree
// verifies against the digests the fresh build recorded. Outputs missing from
// a fresh worktree are restored from the retained copies before verification.
const cacheLayout = 1

type cacheEntry struct {
	Layout      int             `json:"layout"`
	Provider    string          `json:"provider_version"`
	Fingerprint string          `json:"fingerprint"`
	SnapshotID  string          `json:"snapshot_id"`
	Context     bc.BuildContext `json:"context"`
	Inputs      []cachedInput   `json:"inputs"`
}

// cachedInput lists one local input the context references. Retained names a
// CacheDir-relative copy of a checkout output tree that can be restored.
type cachedInput struct {
	ID       bc.InputID   `json:"id"`
	Kind     bc.InputKind `json:"kind"`
	Root     string       `json:"root"`
	Path     string       `json:"path"`
	SHA256   string       `json:"sha256,omitempty"`
	Retained string       `json:"retained,omitempty"`
}

var fingerprintCaches struct {
	sync.Mutex
	byPath map[string]*manifest.FingerprintCache
}

// fingerprints returns the process-wide digest cache for this CacheDir. It is
// loaded once and saved after every build; concurrent builds share it.
func (p *Provider) fingerprints() (*manifest.FingerprintCache, error) {
	name := filepath.Join(p.config.CacheDir, "fingerprints.json")
	fingerprintCaches.Lock()
	defer fingerprintCaches.Unlock()
	if fingerprintCaches.byPath == nil {
		fingerprintCaches.byPath = map[string]*manifest.FingerprintCache{}
	}
	if cache, ok := fingerprintCaches.byPath[name]; ok {
		return cache, nil
	}
	cache, err := manifest.LoadFingerprintCache(name)
	if err != nil {
		return nil, err
	}
	fingerprintCaches.byPath[name] = cache
	return cache, nil
}

func isBuildFile(relative string, entry fs.DirEntry) bool {
	name := entry.Name()
	if name == "pom.xml" || strings.HasPrefix(name, "mvnw") {
		return true
	}
	for _, part := range strings.Split(path.Dir(relative), "/") {
		if part == ".mvn" {
			return true
		}
	}
	return false
}

// buildFingerprint hashes the build definition of a checkout: the relative
// path and bytes of every pom.xml, everything below any .mvn directory and
// every mvnw* wrapper script, plus the toolchain paths, the Maven settings
// files Maven itself would read, and the provider version. Sources are not
// included; they are covered by the snapshot identity.
func (p *Provider) buildFingerprint(ctx context.Context, r bc.Request) (string, error) {
	var files []string
	var visited uint64
	err := filepath.WalkDir(r.Checkout.Path, func(name string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err = ctx.Err(); err != nil {
			return err
		}
		visited++
		if visited > r.Limits.MaxFiles {
			return bc.ErrLimitExceeded
		}
		if entry.IsDir() {
			if name != r.Checkout.Path && (entry.Name() == ".git" || entry.Name() == "target") {
				return filepath.SkipDir
			}
			return nil
		}
		relative, err := filepath.Rel(r.Checkout.Path, name)
		if err != nil {
			return err
		}
		relative = filepath.ToSlash(relative)
		if isBuildFile(relative, entry) {
			files = append(files, relative)
		}
		return nil
	})
	if err != nil {
		return "", err
	}
	sort.Strings(files)
	h := sha256.New()
	frame := func(kind byte, text string) {
		h.Write([]byte{kind})
		var length [8]byte
		binary.BigEndian.PutUint64(length[:], uint64(len(text)))
		h.Write(length[:])
		h.Write([]byte(text))
	}
	frame('V', "codegraph-maven-build-files-v1")
	frame('V', Version)
	frame('M', p.config.MavenExecutable)
	frame('J', p.config.JavaHome)
	// Any of them may build the outputs. Nothing is framed without them, so
	// keys from before build JDKs existed stay valid.
	for _, jdk := range p.buildJDKs {
		frame('B', strconv.Itoa(jdk.major)+"="+jdk.home)
	}
	hashFile := func(kind byte, label, name string) error {
		info, err := os.Stat(name)
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			frame(kind, label)
			frame('T', info.Mode().Type().String())
			return nil
		}
		frame(kind, label)
		if uint64(info.Size()) > r.Limits.MaxInputBytes {
			// Oversized wrapper payloads are identified by size; the observer
			// never reads them either.
			frame('S', fmt.Sprint(info.Size()))
			return nil
		}
		f, err := os.Open(name)
		if err != nil {
			return err
		}
		defer f.Close()
		n, err := io.Copy(h, io.LimitReader(&contextInput{ctx: ctx, reader: f}, int64(r.Limits.MaxInputBytes)+1))
		if err != nil {
			return err
		}
		frame('L', fmt.Sprint(n))
		return nil
	}
	for _, relative := range files {
		if err = hashFile('F', relative, filepath.Join(r.Checkout.Path, filepath.FromSlash(relative))); err != nil {
			return "", err
		}
	}
	// Maven reads these settings without being told; mirrors and profiles in
	// them change dependency resolution.
	settings := []string{filepath.Join(filepath.Dir(filepath.Dir(p.config.MavenExecutable)), "conf", "settings.xml")}
	if home, err := os.UserHomeDir(); err == nil {
		settings = append(settings, filepath.Join(home, ".m2", "settings.xml"))
	}
	for _, name := range settings {
		if err = hashFile('C', name, name); err != nil {
			return "", err
		}
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// entryPath keeps repository identifiers readable while preventing any
// traversal or collision: unsafe runes are replaced and a digest is appended.
func (p *Provider) entryPath(repositoryID, fingerprint string) string {
	safe := strings.Map(func(r rune) rune {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '.' || r == '-' || r == '_' {
			return r
		}
		return '_'
	}, repositoryID)
	if len(safe) > 64 {
		safe = safe[:64]
	}
	sum := sha256.Sum256([]byte(repositoryID))
	return filepath.Join(p.config.CacheDir, "buildcontext", safe+"-"+hex.EncodeToString(sum[:6]), fingerprint+".json")
}

// loadCached returns the cached context for this checkout, or a non-empty
// miss reason. Any failure to verify or restore inputs is a miss, never an
// error: the fresh build that follows overwrites the entry.
func (p *Provider) loadCached(ctx context.Context, r bc.Request, fingerprint string, cache *manifest.FingerprintCache) (bc.BuildContext, string, error) {
	name := p.entryPath(r.Checkout.RepositoryID, fingerprint)
	info, err := os.Stat(name)
	if errors.Is(err, os.ErrNotExist) {
		return bc.BuildContext{}, "no entry", nil
	}
	if err != nil {
		return bc.BuildContext{}, "", err
	}
	if !info.Mode().IsRegular() || uint64(info.Size()) > r.Limits.MaxOutputBytes*2 {
		return bc.BuildContext{}, "entry is not a bounded regular file", nil
	}
	body, err := os.ReadFile(name)
	if err != nil {
		return bc.BuildContext{}, "", err
	}
	var entry cacheEntry
	if err = json.Unmarshal(body, &entry); err != nil {
		return bc.BuildContext{}, "entry is unreadable", nil
	}
	if entry.Layout != cacheLayout || entry.Provider != Version || entry.Fingerprint != fingerprint || entry.Context.RepositoryID != r.Checkout.RepositoryID {
		return bc.BuildContext{}, "entry identity differs", nil
	}
	if entry.SnapshotID != r.Checkout.SnapshotID {
		return bc.BuildContext{}, "entry was built from snapshot " + entry.SnapshotID + "; compiled outputs depend on that snapshot's sources", nil
	}
	if err = entry.Context.Validate(); err != nil {
		return bc.BuildContext{}, "entry context is invalid", nil
	}
	checkoutPath, err := filepath.EvalSymlinks(r.Checkout.Path)
	if err != nil {
		return bc.BuildContext{}, "", err
	}
	checkout, err := os.OpenRoot(checkoutPath)
	if err != nil {
		return bc.BuildContext{}, "", err
	}
	defer checkout.Close()
	byID := map[bc.InputID]bc.Input{}
	for _, input := range entry.Context.Inventory.Inputs {
		byID[input.ID] = input
	}
	var restored []string
	undo := func() {
		for _, relative := range restored {
			_ = checkout.RemoveAll(filepath.FromSlash(relative))
		}
	}
	seen := map[string]bool{}
	for _, cached := range entry.Inputs {
		if err = ctx.Err(); err != nil {
			undo()
			return bc.BuildContext{}, "", err
		}
		input, ok := byID[cached.ID]
		if !ok || input.Location == nil || input.Location.Root != cached.Root || input.Location.Path != cached.Path || input.Kind != cached.Kind || input.SHA256 != cached.SHA256 {
			undo()
			return bc.BuildContext{}, "entry inputs disagree with its context", nil
		}
		root := ""
		switch cached.Root {
		case "checkout":
			root = checkoutPath
		case "java-jdk":
			root = p.config.JavaHome
		case "java-cache":
			root = p.config.CacheDir
		default:
			undo()
			return bc.BuildContext{}, "entry references unknown root " + cached.Root, nil
		}
		if cached.Kind == bc.InputSourceRoot {
			if stat, e := checkout.Stat(filepath.FromSlash(cached.Path)); e != nil || !stat.IsDir() {
				undo()
				return bc.BuildContext{}, "source root " + cached.Path + " is absent", nil
			}
			continue
		}
		digest, err := manifest.FingerprintCached(ctx, cache, root, cached.Path, cached.Kind, r.Limits)
		if errors.Is(err, os.ErrNotExist) && cached.Root == "checkout" && cached.Retained != "" && !seen[cached.Path] {
			seen[cached.Path] = true
			restored = append(restored, cached.Path)
			if err = p.restoreTree(ctx, r, checkout, cached.Path, cached.Retained, cache); err != nil {
				undo()
				if ctx.Err() != nil {
					return bc.BuildContext{}, "", ctx.Err()
				}
				return bc.BuildContext{}, "restoring " + cached.Path + " failed: " + err.Error(), nil
			}
			digest, err = manifest.FingerprintCached(ctx, cache, root, cached.Path, cached.Kind, r.Limits)
		}
		if err != nil {
			undo()
			if ctx.Err() != nil {
				return bc.BuildContext{}, "", ctx.Err()
			}
			return bc.BuildContext{}, "input " + cached.Path + " is unavailable: " + err.Error(), nil
		}
		if digest != cached.SHA256 {
			undo()
			return bc.BuildContext{}, "input " + cached.Path + " changed since the entry was built", nil
		}
	}
	// Identity is recomputed exactly as a fresh observation computes it; the
	// cached ID is only accepted when sealing reproduces it.
	result, err := bc.Seal(bc.BuildContext{RepositoryID: r.Checkout.RepositoryID, SnapshotID: r.Checkout.SnapshotID, Producer: entry.Context.Producer, Inventory: entry.Context.Inventory, Checks: entry.Context.Checks})
	if err != nil || result.ID != entry.Context.ID {
		undo()
		return bc.BuildContext{}, "entry context does not reseal to its identity", nil
	}
	if result.RecordCount() > r.Limits.MaxRecords || uint64(len(result.Diagnostics)) > r.Limits.MaxDiagnostics || uint64(len(body)) > r.Limits.MaxOutputBytes*2 {
		undo()
		return bc.BuildContext{}, "", bc.ErrLimitExceeded
	}
	return result, "", nil
}

// storeCached retains every checkout output tree the context references and
// writes the entry. It is best effort: a failure is logged and leaves the
// previous entry (if any) in place; the returned context is unaffected.
func (p *Provider) storeCached(ctx context.Context, r bc.Request, fingerprint string, build bc.BuildContext) error {
	checkoutPath, err := filepath.EvalSymlinks(r.Checkout.Path)
	if err != nil {
		return err
	}
	checkout, err := os.OpenRoot(checkoutPath)
	if err != nil {
		return err
	}
	defer checkout.Close()
	entry := cacheEntry{Layout: cacheLayout, Provider: Version, Fingerprint: fingerprint, SnapshotID: r.Checkout.SnapshotID, Context: build}
	retained := map[string]string{}
	for _, input := range build.Inventory.Inputs {
		if input.Location == nil {
			continue
		}
		cached := cachedInput{ID: input.ID, Kind: input.Kind, Root: input.Location.Root, Path: input.Location.Path, SHA256: input.SHA256}
		if input.Location.Root == "checkout" && (input.Kind == bc.InputClasses || input.Kind == bc.InputGeneratedRoot) {
			if name, ok := retained[input.SHA256]; ok {
				cached.Retained = name
			} else {
				cached.Retained, err = p.retainTree(ctx, r, checkout, input.Location.Path, input.SHA256)
				if err != nil {
					return fmt.Errorf("retain %s: %w", input.Location.Path, err)
				}
				retained[input.SHA256] = cached.Retained
			}
		}
		entry.Inputs = append(entry.Inputs, cached)
	}
	body, err := json.Marshal(entry)
	if err != nil {
		return err
	}
	name := p.entryPath(r.Checkout.RepositoryID, fingerprint)
	if err = os.MkdirAll(filepath.Dir(name), 0700); err != nil {
		return err
	}
	temp, err := os.CreateTemp(filepath.Dir(name), ".entry-*")
	if err != nil {
		return err
	}
	_, writeErr := temp.Write(body)
	syncErr := temp.Sync()
	closeErr := temp.Close()
	if err = errors.Join(writeErr, syncErr, closeErr); err != nil {
		os.Remove(temp.Name())
		return err
	}
	if err = os.Rename(temp.Name(), name); err != nil {
		os.Remove(temp.Name())
		return err
	}
	return nil
}

// retainTree copies a checkout output tree into outputs/<digest>. The copy is
// verified against the digest before it becomes visible; an existing copy is
// reused without reading the checkout. Files are copied, never hard-linked:
// a later Maven run rewrites class files in place.
func (p *Provider) retainTree(ctx context.Context, r bc.Request, checkout *os.Root, relative, digest string) (string, error) {
	if !bc.ValidPath(relative) || len(digest) != 64 {
		return "", bc.ErrInvalidInput
	}
	name := path.Join("outputs", digest)
	if info, err := os.Lstat(filepath.Join(p.config.CacheDir, filepath.FromSlash(name))); err == nil && info.IsDir() {
		return name, nil
	}
	outputs := filepath.Join(p.config.CacheDir, "outputs")
	if err := os.MkdirAll(outputs, 0700); err != nil {
		return "", err
	}
	cacheRoot, err := os.OpenRoot(p.config.CacheDir)
	if err != nil {
		return "", err
	}
	defer cacheRoot.Close()
	temp, err := os.MkdirTemp(outputs, ".retain-*")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(temp)
	tempRelative := path.Join("outputs", filepath.Base(temp))
	if err = copyTree(ctx, r.Limits, checkout, relative, cacheRoot, tempRelative, nil, p.config.CacheDir); err != nil {
		return "", err
	}
	actual, err := manifest.Fingerprint(ctx, p.config.CacheDir, tempRelative, bc.InputClasses, r.Limits)
	if err != nil {
		return "", err
	}
	if actual != digest {
		return "", fmt.Errorf("%w: retained output copy does not reproduce %s", bc.ErrIdentityMismatch, relative)
	}
	if err = cacheRoot.Rename(filepath.FromSlash(tempRelative), filepath.FromSlash(name)); err != nil {
		if info, statErr := cacheRoot.Lstat(filepath.FromSlash(name)); statErr == nil && info.IsDir() {
			return name, nil
		}
		return "", err
	}
	return name, nil
}

// restoreTree copies outputs/<digest> back to an absent checkout path. Bytes
// are hashed while copied and remembered, so the verification that follows
// is a stat walk.
func (p *Provider) restoreTree(ctx context.Context, r bc.Request, checkout *os.Root, relative, retained string, cache *manifest.FingerprintCache) error {
	if !bc.ValidPath(relative) || !bc.ValidPath(retained) || !strings.HasPrefix(retained, "outputs/") {
		return bc.ErrInvalidInput
	}
	cacheRoot, err := os.OpenRoot(p.config.CacheDir)
	if err != nil {
		return err
	}
	defer cacheRoot.Close()
	if _, err = checkout.Lstat(filepath.FromSlash(relative)); !errors.Is(err, os.ErrNotExist) {
		if err == nil {
			return fmt.Errorf("%w: restore target exists", bc.ErrInvalidInput)
		}
		return err
	}
	return copyTree(ctx, r.Limits, cacheRoot, retained, checkout, relative, cache, checkout.Name())
}

// copyTree copies directories and regular files from src/from to dst/to,
// creating parents. Anything else (symlinks, devices) is rejected, matching
// what fingerprints accept. When cache is non-nil each copied file's digest
// is remembered under dstBase.
func copyTree(ctx context.Context, limits bc.Limits, src *os.Root, from string, dst *os.Root, to string, cache *manifest.FingerprintCache, dstBase string) error {
	var files, bytes uint64
	buffer := make([]byte, 128<<10)
	return fs.WalkDir(src.FS(), from, func(name string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err = ctx.Err(); err != nil {
			return err
		}
		files++
		if files > limits.MaxFiles {
			return bc.ErrLimitExceeded
		}
		rel := strings.TrimPrefix(strings.TrimPrefix(name, from), "/")
		target := filepath.FromSlash(path.Join(to, rel))
		if uint32(strings.Count(path.Join(to, rel), "/")+1) > limits.MaxDepth {
			return bc.ErrLimitExceeded
		}
		switch {
		case entry.IsDir():
			return dst.MkdirAll(target, 0700)
		case entry.Type().IsRegular():
			in, err := src.Open(name)
			if err != nil {
				return err
			}
			defer in.Close()
			info, err := in.Stat()
			if err != nil {
				return err
			}
			if !info.Mode().IsRegular() {
				return fmt.Errorf("%w: unsupported output entry", bc.ErrInvalidInput)
			}
			if uint64(info.Size()) > limits.MaxHashBytes-bytes {
				return bc.ErrLimitExceeded
			}
			bytes += uint64(info.Size())
			out, err := dst.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
			if err != nil {
				return err
			}
			h := sha256.New()
			_, copyErr := io.CopyBuffer(io.MultiWriter(out, h), io.LimitReader(&contextInput{ctx: ctx, reader: in}, info.Size()+1), buffer)
			closeErr := out.Close()
			if err = errors.Join(copyErr, closeErr); err != nil {
				return err
			}
			if cache != nil {
				if written, e := dst.Lstat(target); e == nil && written.Size() == info.Size() {
					cache.Remember(filepath.Join(dstBase, target), written, hex.EncodeToString(h.Sum(nil)))
				}
			}
			return nil
		default:
			return fmt.Errorf("%w: unsupported output entry %s", bc.ErrInvalidInput, name)
		}
	})
}

func logCacheDecision(ctx context.Context, r bc.Request, fingerprint, reason string) {
	if reason == "" {
		slog.InfoContext(ctx, "Maven build context cache hit", "repository_id", r.Checkout.RepositoryID, "snapshot_id", r.Checkout.SnapshotID, "fingerprint", fingerprint[:16])
		return
	}
	slog.InfoContext(ctx, "Maven build context cache miss", "repository_id", r.Checkout.RepositoryID, "snapshot_id", r.Checkout.SnapshotID, "fingerprint", fingerprint[:16], "reason", reason)
}
