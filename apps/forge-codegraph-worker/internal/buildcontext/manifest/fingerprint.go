package manifest

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"ei-aitiger-codegraph/pkg/buildcontext"
)

var errUnsupportedInput = errors.New("unsupported input filesystem type")

type filesystem struct {
	ctx          context.Context
	limits       buildcontext.Limits
	files, bytes uint64
	buffer       []byte
	// cache and rootDir are optional; together they let fileDigest reuse a
	// digest for a regular file whose size and modification time are unchanged.
	cache   *FingerprintCache
	rootDir string
}

func (s *filesystem) visit() error {
	if err := s.ctx.Err(); err != nil {
		return err
	}
	if s.files >= s.limits.MaxFiles {
		return fmt.Errorf("%w: filesystem inspections", buildcontext.ErrLimitExceeded)
	}
	s.files++
	return nil
}
func (s *filesystem) inspect(root *os.Root, name string) (os.FileInfo, error) {
	if !buildcontext.ValidPath(name) {
		return nil, fmt.Errorf("%w: invalid input path", buildcontext.ErrInvalidInput)
	}
	parts := strings.Split(name, "/")
	if uint32(len(parts)) > s.limits.MaxDepth {
		return nil, fmt.Errorf("%w: input path depth", buildcontext.ErrLimitExceeded)
	}
	var info os.FileInfo
	for i := range parts {
		if err := s.visit(); err != nil {
			return nil, err
		}
		var err error
		info, err = root.Lstat(strings.Join(parts[:i+1], "/"))
		if err != nil {
			return nil, err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return nil, errUnsupportedInput
		}
		if i < len(parts)-1 && !info.IsDir() {
			return nil, errUnsupportedInput
		}
	}
	return info, nil
}

// Fingerprint computes the same pin used by the provider. It is useful to an
// explicit manifest exporter. SHA-256 for JAR files; ei-tree-sha256-v1 for JDK,
// generated-source and class directories. A source_root has no tree digest.
// It performs only bounded reads beneath rootDirectory, without following links.
func Fingerprint(ctx context.Context, rootDirectory, relativePath string, kind buildcontext.InputKind, limits buildcontext.Limits) (string, error) {
	return FingerprintCached(ctx, nil, rootDirectory, relativePath, kind, limits)
}

// FingerprintCached is Fingerprint with a shared digest cache. A regular file
// whose absolute path, size and modification time match a remembered entry is
// not read again; every other file is hashed and remembered. The result is
// identical to Fingerprint, and byte budgets are charged as if every file were
// read, so limits behave the same on a warm and a cold cache. A nil cache
// disables reuse.
func FingerprintCached(ctx context.Context, cache *FingerprintCache, rootDirectory, relativePath string, kind buildcontext.InputKind, limits buildcontext.Limits) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	if err := limits.Validate(); err != nil {
		return "", err
	}
	root, err := os.OpenRoot(rootDirectory)
	if err != nil {
		return "", err
	}
	defer root.Close()
	s := filesystem{ctx: ctx, limits: limits}
	if cache != nil {
		absolute, err := filepath.Abs(rootDirectory)
		if err != nil {
			return "", err
		}
		s.cache, s.rootDir = cache, absolute
	}
	return s.fingerprint(root, relativePath, kind)
}

func (s *filesystem) fingerprint(root *os.Root, name string, kind buildcontext.InputKind) (string, error) {
	info, err := s.inspect(root, name)
	if err != nil {
		return "", err
	}
	if kind == buildcontext.InputJAR {
		if !info.Mode().IsRegular() {
			return "", errUnsupportedInput
		}
		return s.fileDigest(root, name, info)
	}
	if kind != buildcontext.InputSourceRoot && kind != buildcontext.InputGeneratedRoot && kind != buildcontext.InputClasses && kind != buildcontext.InputJDK {
		return "", fmt.Errorf("%w: unsupported fingerprint kind", buildcontext.ErrInvalidInput)
	}
	if !info.IsDir() {
		return "", errUnsupportedInput
	}
	if kind == buildcontext.InputSourceRoot {
		return "", nil
	}
	h := sha256.New()
	h.Write([]byte("ei-tree-sha256-v1\n"))
	if err := s.directory(root, name, "", 1, h); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func frame(h hash.Hash, kind byte, name string) {
	h.Write([]byte{kind})
	var length [8]byte
	binary.BigEndian.PutUint64(length[:], uint64(len(name)))
	h.Write(length[:])
	h.Write([]byte(name))
}

func (s *filesystem) directory(root *os.Root, name, relative string, depth uint32, h hash.Hash) error {
	if err := s.ctx.Err(); err != nil {
		return err
	}
	if depth > s.limits.MaxDepth {
		return fmt.Errorf("%w: directory depth", buildcontext.ErrLimitExceeded)
	}
	frame(h, 'D', relative)
	dir, err := root.Open(name)
	if err != nil {
		return err
	}
	// Read in batches and account entries before retaining names. Sorting a
	// directory is bounded by MaxFiles rather than a repository-wide walk slice.
	var names []string
	for {
		entries, readErr := dir.ReadDir(256)
		for _, entry := range entries {
			if err := s.visit(); err != nil {
				dir.Close()
				return err
			}
			names = append(names, entry.Name())
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			dir.Close()
			return readErr
		}
	}
	if err := dir.Close(); err != nil {
		return err
	}
	sort.Strings(names)
	for _, child := range names {
		if err := s.ctx.Err(); err != nil {
			return err
		}
		full, rel := path.Join(name, child), path.Join(relative, child)
		info, err := root.Lstat(full)
		if err != nil {
			return err
		}
		switch {
		case info.IsDir():
			if err := s.directory(root, full, rel, depth+1, h); err != nil {
				return err
			}
		case info.Mode().IsRegular():
			value, err := s.fileDigest(root, full, info)
			if err != nil {
				return err
			}
			raw, _ := hex.DecodeString(value)
			frame(h, 'F', rel)
			h.Write(raw)
		default:
			return errUnsupportedInput
		}
	}
	return nil
}

// fileDigest hashes one regular file described by info (from Lstat, so a
// remembered digest is returned without even opening the file). The open
// file is inspected again so a path swapped in between is never trusted.
func (s *filesystem) fileDigest(root *os.Root, name string, info os.FileInfo) (string, error) {
	if !info.Mode().IsRegular() {
		return "", errUnsupportedInput
	}
	if uint64(info.Size()) > s.limits.MaxHashBytes-s.bytes {
		return "", fmt.Errorf("%w: fingerprint bytes", buildcontext.ErrLimitExceeded)
	}
	key := ""
	if s.cache != nil {
		key = filepath.Join(s.rootDir, filepath.FromSlash(name))
		if digest, ok := s.cache.Lookup(key, info); ok {
			s.bytes += uint64(info.Size())
			return digest, nil
		}
	}
	f, err := root.Open(name)
	if err != nil {
		return "", err
	}
	defer f.Close()
	if info, err = f.Stat(); err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", errUnsupportedInput
	}
	if uint64(info.Size()) > s.limits.MaxHashBytes-s.bytes {
		return "", fmt.Errorf("%w: fingerprint bytes", buildcontext.ErrLimitExceeded)
	}
	h := sha256.New()
	if s.buffer == nil {
		s.buffer = make([]byte, 32<<10)
	}
	buf := s.buffer
	for {
		if err := s.ctx.Err(); err != nil {
			return "", err
		}
		n, err := f.Read(buf)
		if uint64(n) > s.limits.MaxHashBytes-s.bytes {
			return "", fmt.Errorf("%w: fingerprint bytes", buildcontext.ErrLimitExceeded)
		}
		s.bytes += uint64(n)
		h.Write(buf[:n])
		if err == io.EOF {
			break
		}
		if err != nil {
			return "", err
		}
	}
	if err := s.ctx.Err(); err != nil {
		return "", err
	}
	digest := hex.EncodeToString(h.Sum(nil))
	if s.cache != nil {
		// Remember only when the file is unchanged after reading; a concurrent
		// writer would otherwise pin a digest to a (size, mtime) it never had.
		if after, err := f.Stat(); err == nil && after.Size() == info.Size() && after.ModTime().Equal(info.ModTime()) {
			s.cache.Remember(key, info, digest)
		}
	}
	return digest, nil
}

// FingerprintCache remembers the SHA-256 of regular files keyed by absolute
// path, size and modification time, so a dependency JAR or a JDK tree is read
// once per machine rather than once per build. It trusts an unchanged
// (size, mtime) pair the way build tools and git's index do. Entries are kept
// in memory; Save merges them into the backing file, dropping remembered
// files that no longer exist or changed, so the file cannot grow unboundedly.
// A nil *FingerprintCache is a valid empty cache that remembers nothing.
type FingerprintCache struct {
	path    string
	mu      sync.Mutex
	entries map[string]fingerprintEntry
	touched map[string]bool
	dirty   bool
}

type fingerprintEntry struct {
	Size    int64  `json:"size"`
	ModTime int64  `json:"mtime_ns"`
	SHA256  string `json:"sha256"`
}

type fingerprintFile struct {
	Version int                         `json:"version"`
	Entries map[string]fingerprintEntry `json:"entries"`
}

const (
	fingerprintCacheVersion = 1
	maxFingerprintEntries   = 500_000
	maxFingerprintFileBytes = 256 << 20
)

// LoadFingerprintCache reads path, tolerating an absent or unreadable file:
// a cache that starts empty only costs one more read per file. Save writes
// back to the same path.
func LoadFingerprintCache(path string) (*FingerprintCache, error) {
	if !filepath.IsAbs(path) {
		return nil, fmt.Errorf("%w: fingerprint cache path must be absolute", buildcontext.ErrInvalidInput)
	}
	c := &FingerprintCache{path: path, entries: map[string]fingerprintEntry{}, touched: map[string]bool{}}
	c.entries = readFingerprintFile(path)
	return c, nil
}

func readFingerprintFile(path string) map[string]fingerprintEntry {
	entries := map[string]fingerprintEntry{}
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() > maxFingerprintFileBytes {
		return entries
	}
	body, err := os.ReadFile(path)
	if err != nil {
		return entries
	}
	var file fingerprintFile
	if err = json.Unmarshal(body, &file); err != nil || file.Version != fingerprintCacheVersion {
		return entries
	}
	for key, entry := range file.Entries {
		if filepath.IsAbs(key) && entry.Size >= 0 && len(entry.SHA256) == 64 {
			entries[key] = entry
		}
	}
	return entries
}

// Lookup returns the remembered digest of an absolute path whose current
// size and modification time equal the remembered ones.
func (c *FingerprintCache) Lookup(absolutePath string, info os.FileInfo) (string, bool) {
	if c == nil || info == nil || !info.Mode().IsRegular() {
		return "", false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.entries[absolutePath]
	if !ok || entry.Size != info.Size() || entry.ModTime != info.ModTime().UnixNano() {
		return "", false
	}
	c.touched[absolutePath] = true
	return entry.SHA256, true
}

// Remember records the digest of a regular file at its current size and
// modification time. Callers that hash bytes while copying a file can seed
// the cache so the copy is never read a second time.
func (c *FingerprintCache) Remember(absolutePath string, info os.FileInfo, digest string) {
	if c == nil || info == nil || !info.Mode().IsRegular() || !filepath.IsAbs(absolutePath) || len(digest) != 64 {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	entry := fingerprintEntry{Size: info.Size(), ModTime: info.ModTime().UnixNano(), SHA256: digest}
	if previous, ok := c.entries[absolutePath]; !ok || previous != entry {
		c.dirty = true
	}
	c.entries[absolutePath] = entry
	c.touched[absolutePath] = true
}

// Len reports the number of remembered files.
func (c *FingerprintCache) Len() int {
	if c == nil {
		return 0
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.entries)
}

// Save merges this cache into its file. Entries used or added since the last
// Save win; every other entry, including ones written by another process, is
// kept only while its file still has the remembered size and modification
// time. The file is replaced atomically, so a concurrent Save loses at most
// the other writer's newest entries, never the file's integrity.
func (c *FingerprintCache) Save() error {
	if c == nil {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.dirty && len(c.touched) == 0 {
		return nil
	}
	merged := readFingerprintFile(c.path)
	for key, entry := range c.entries {
		if c.touched[key] {
			merged[key] = entry
		} else if _, ok := merged[key]; !ok {
			merged[key] = entry
		}
	}
	for key, entry := range merged {
		if c.touched[key] {
			continue
		}
		info, err := os.Lstat(key)
		if err != nil || !info.Mode().IsRegular() || info.Size() != entry.Size || info.ModTime().UnixNano() != entry.ModTime {
			delete(merged, key)
		}
	}
	if len(merged) > maxFingerprintEntries {
		for key := range merged {
			if !c.touched[key] {
				delete(merged, key)
			}
			if len(merged) <= maxFingerprintEntries {
				break
			}
		}
	}
	body, err := json.Marshal(fingerprintFile{Version: fingerprintCacheVersion, Entries: merged})
	if err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(c.path), 0700); err != nil {
		return err
	}
	temp, err := os.CreateTemp(filepath.Dir(c.path), ".fingerprints-*")
	if err != nil {
		return err
	}
	_, writeErr := temp.Write(body)
	closeErr := temp.Close()
	if err = errors.Join(writeErr, closeErr); err != nil {
		os.Remove(temp.Name())
		return err
	}
	if err = os.Rename(temp.Name(), c.path); err != nil {
		os.Remove(temp.Name())
		return err
	}
	c.entries = merged
	c.touched = map[string]bool{}
	c.dirty = false
	return nil
}
