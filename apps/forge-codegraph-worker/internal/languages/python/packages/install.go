// Package packages installs the third-party packages a Python checkout
// declares, so Pyright can follow its code into them. Only wheels are
// installed, so no package code (setup.py, a build backend) ever runs; only
// the operator's index is used; installations are cached by what they were
// asked for; and a package that cannot be installed is left out and named.
package packages

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"ei-aitiger-codegraph/worker/internal/scratch"
)

// format versions the cached installation layout and marker.
const format = "1"

// marker records a finished installation inside its directory.
const marker = ".codegraph-installed.json"

// retryAfter is how long an installation that could not finish (the index
// unreachable, out of time) is not tried again.
const retryAfter = 6 * time.Hour

// DefaultExclude are machine learning stacks of gigabytes of native code
// whose Python API is better named than installed; code using them still
// resolves to their dotted names.
var DefaultExclude = []string{"torch", "torchvision", "torchaudio", "tensorflow", "tensorflow-cpu", "tensorflow-gpu", "tensorflow-intel", "tensorflow-macos", "tf-nightly", "jax", "jaxlib", "triton", "mxnet", "paddlepaddle", "paddlepaddle-gpu", "cupy", "tensorrt", "onnxruntime-gpu"}

type Config struct {
	UV       string // the uv executable
	IndexURL string // empty for uv's default index (PyPI)
	CacheDir string // absolute; installations and uv's download cache
	MaxBytes int64  // an installation larger than this, after pruning, is not used
	Timeout  time.Duration
	Exclude  []string // package names never installed
}

type Request struct {
	Version      string   // Python version analysed, as 3.12
	Platform     string   // Linux, Darwin or Windows
	Requirements []string // PEP 508 requirements from the index
	Pins         []string // name==version from lockfiles, applied as constraints
}

type Failure struct {
	Requirement string `json:"requirement"`
	Reason      string `json:"reason"`
}

type Result struct {
	Dir       string    `json:"-"`
	Digest    string    `json:"digest"`
	Installed []string  `json:"installed"`
	Failed    []Failure `json:"failed,omitempty"`
	// Unpinned is set when the lockfiles' versions could not be installed
	// together and the newest compatible versions were installed instead.
	Unpinned bool `json:"unpinned,omitempty"`
	// Retry is set when a failure may not recur (the index could not be
	// reached): the installation is used, and repeated on the next run.
	Retry bool `json:"retry,omitempty"`
}

// Install returns the directory the requested packages are installed in,
// installing them unless an earlier run did. An error means nothing could be
// installed; a package that could not is a Failure of the Result.
func Install(ctx context.Context, c Config, r Request) (Result, error) {
	if len(r.Requirements) == 0 {
		return Result{}, nil
	}
	platform, ok := map[string]string{"Linux": "x86_64-manylinux_2_28", "Darwin": "aarch64-apple-darwin", "Windows": "x86_64-pc-windows-msvc"}[r.Platform]
	if !ok {
		return Result{}, fmt.Errorf("no wheels are chosen for platform %q", r.Platform)
	}
	if !filepath.IsAbs(c.CacheDir) {
		return Result{}, errors.New("the package cache directory must be absolute")
	}
	if err := os.MkdirAll(c.CacheDir, 0o700); err != nil {
		return Result{}, err
	}
	scratch.Sweep(c.CacheDir, "tmp-", 24*time.Hour)
	scratch.Sweep(c.CacheDir, "site-", 30*24*time.Hour)
	scratch.Sweep(c.CacheDir, "failed-", retryAfter)
	boundDownloads(filepath.Join(c.CacheDir, "uv-cache"), 4*c.MaxBytes)
	requirements := append([]string{}, r.Requirements...)
	pins := append([]string{}, r.Pins...)
	exclude := append([]string{}, c.Exclude...)
	sort.Strings(requirements)
	sort.Strings(pins)
	sort.Strings(exclude)
	identity, _ := json.Marshal(map[string]any{"format": format, "index": c.IndexURL, "exclude": exclude, "version": r.Version, "platform": platform, "requirements": requirements, "pins": pins})
	sum := sha256.Sum256(identity)
	key := hex.EncodeToString(sum[:])[:40]
	// An installation is never changed once finished, because a run
	// fingerprints it and analyses it later: a repeated installation is a
	// new directory, and the ones it replaces are removed a day later.
	previous, found := latest(c.CacheDir, key)
	if found && !previous.Retry {
		now := time.Now()
		_ = os.Chtimes(previous.Dir, now, now)
		return previous, nil
	}
	// An installation that could not finish is not repeated on every run:
	// until retryAfter passes, the last one (or none) is used.
	failed := filepath.Join(c.CacheDir, "failed-"+key)
	if info, err := os.Stat(failed); err == nil && time.Since(info.ModTime()) < retryAfter {
		if found {
			return previous, nil
		}
		why, _ := os.ReadFile(failed)
		return Result{}, fmt.Errorf("%s (tried %s ago; tried again %s after that)", why, time.Since(info.ModTime()).Round(time.Minute), retryAfter)
	}
	remember := func(why string) { _ = os.WriteFile(failed, []byte(why), 0o600) }
	work, err := os.MkdirTemp(c.CacheDir, "tmp-"+key+"-")
	if err != nil {
		return Result{}, err
	}
	defer os.RemoveAll(work)
	if c.Timeout <= 0 {
		c.Timeout = 10 * time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, c.Timeout)
	defer cancel()
	target := filepath.Join(work, "site")
	write := func(name string, lines []string) (string, error) {
		p := filepath.Join(work, name)
		return p, os.WriteFile(p, []byte(strings.Join(lines, "\n")+"\n"), 0o600)
	}
	all, err := write("requirements.txt", requirements)
	if err != nil {
		return Result{}, err
	}
	constraints, err := write("constraints.txt", pins)
	if err != nil {
		return Result{}, err
	}
	// A package overridden with a marker that is never true is left out of
	// the resolution entirely.
	var never []string
	for _, name := range exclude {
		never = append(never, name+` ; python_version < "0"`)
	}
	overrides, err := write("overrides.txt", never)
	if err != nil {
		return Result{}, err
	}
	install := func(requirements string, pinned bool) (string, error) {
		args := []string{"pip", "install", "--no-config", "--quiet", "--target", target, "--python-version", r.Version,
			"--python-platform", platform, "--only-binary", ":all:", "--overrides", overrides, "-r", requirements}
		if pinned && len(pins) > 0 {
			args = append(args, "--constraints", constraints)
		}
		if c.IndexURL != "" {
			args = append(args, "--index-url", c.IndexURL)
		}
		cmd := exec.CommandContext(ctx, c.UV, args...)
		cmd.Dir = work
		cmd.Env = append(os.Environ(), "UV_CACHE_DIR="+filepath.Join(c.CacheDir, "uv-cache"), "UV_NO_PROGRESS=1", "UV_PYTHON_DOWNLOADS=never", "NO_COLOR=1")
		var stderr bytes.Buffer
		cmd.Stdout, cmd.Stderr = &stderr, &stderr
		if err := cmd.Run(); err != nil {
			if ctx.Err() != nil {
				return "", ctx.Err()
			}
			return reason(stderr.String(), err), errors.New("install failed")
		}
		return "", nil
	}
	result := Result{}
	why, err := install(all, true)
	if err != nil && ctx.Err() == nil && len(pins) > 0 {
		result.Unpinned = true
		why, err = install(all, false)
	}
	if err != nil {
		if ctx.Err() != nil {
			remember(fmt.Sprintf("installing packages took longer than %s", c.Timeout))
			if found {
				return previous, nil
			}
			return Result{}, fmt.Errorf("installing packages took longer than %s", c.Timeout)
		}
		if transient(why) {
			remember("the package index could not be reached: " + why)
			if found {
				return previous, nil
			}
			return Result{}, fmt.Errorf("the package index could not be reached: %s", why)
		}
		// Together they do not install: each alone, so one missing package
		// or one without wheels does not cost the others.
		result.Unpinned = len(pins) > 0
		for _, req := range requirements {
			one, err := write("one.txt", []string{req})
			if err != nil {
				return Result{}, err
			}
			why, err := install(one, false)
			if err == nil {
				continue
			}
			if ctx.Err() != nil {
				result.Failed = append(result.Failed, Failure{Requirement: req, Reason: "the installation ran out of time"})
				result.Retry = true
				continue
			}
			result.Retry = result.Retry || transient(why)
			result.Failed = append(result.Failed, Failure{Requirement: req, Reason: why})
		}
	}
	if result.Retry {
		remember("some packages could not be installed for a reason that may pass")
	} else {
		_ = os.Remove(failed)
	}
	if _, err := os.Stat(target); err != nil {
		if len(result.Failed) > 0 {
			return result, nil
		}
		return Result{}, err
	}
	size, err := prune(target)
	if err != nil {
		return Result{}, err
	}
	if c.MaxBytes > 0 && size > c.MaxBytes {
		return Result{}, fmt.Errorf("the packages take %d MiB, more than the %d MiB allowed", size>>20, c.MaxBytes>>20)
	}
	result.Installed, err = distributions(target)
	if err != nil {
		return Result{}, err
	}
	digest := sha256.Sum256([]byte(key + "\n" + strings.Join(result.Installed, "\n")))
	result.Digest = hex.EncodeToString(digest[:])
	data, err := json.Marshal(result)
	if err != nil {
		return Result{}, err
	}
	if err := os.WriteFile(filepath.Join(target, marker), data, 0o600); err != nil {
		return Result{}, err
	}
	// The same packages installed by another worker first are the same
	// directory; either copy will do.
	dir := filepath.Join(c.CacheDir, "site-"+key+"-"+result.Digest[:16])
	if err := os.Rename(target, dir); err != nil {
		existing, ok := read(dir)
		if !ok {
			return Result{}, err
		}
		result = existing
	}
	result.Dir = dir
	now := time.Now()
	_ = os.Chtimes(dir, now, now)
	entries, _ := os.ReadDir(c.CacheDir)
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "site-"+key+"-") && e.Name() != filepath.Base(dir) {
			if info, err := e.Info(); err == nil && time.Since(info.ModTime()) > 24*time.Hour {
				_ = os.RemoveAll(filepath.Join(c.CacheDir, e.Name()))
			}
		}
	}
	return result, nil
}

// boundDownloads empties uv's download cache once it grows past limit; it is
// measured at most once a day, since walking it costs more than an install
// that hits it.
func boundDownloads(dir string, limit int64) {
	stamp := filepath.Join(dir, ".codegraph-measured")
	if info, err := os.Stat(stamp); limit <= 0 || err == nil && time.Since(info.ModTime()) < 24*time.Hour {
		return
	}
	var size int64
	_ = filepath.WalkDir(dir, func(_ string, d fs.DirEntry, err error) error {
		if err == nil && d.Type().IsRegular() {
			if info, err := d.Info(); err == nil {
				size += info.Size()
			}
		}
		return nil
	})
	if size > limit {
		_ = os.RemoveAll(dir)
	}
	if os.MkdirAll(dir, 0o700) == nil {
		_ = os.WriteFile(stamp, nil, 0o600)
	}
}

// latest is the most recently used finished installation of a request.
func latest(cache, key string) (Result, bool) {
	entries, err := os.ReadDir(cache)
	if err != nil {
		return Result{}, false
	}
	var best Result
	var bestTime time.Time
	found := false
	for _, e := range entries {
		if !e.IsDir() || !strings.HasPrefix(e.Name(), "site-"+key+"-") {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		if r, ok := read(filepath.Join(cache, e.Name())); ok && (!found || info.ModTime().After(bestTime)) {
			best, bestTime, found = r, info.ModTime(), true
		}
	}
	return best, found
}

// read reads a finished installation's record.
func read(dir string) (Result, bool) {
	data, err := os.ReadFile(filepath.Join(dir, marker))
	if err != nil {
		return Result{}, false
	}
	var r Result
	if json.Unmarshal(data, &r) != nil || len(r.Digest) != 64 {
		return Result{}, false
	}
	r.Dir = dir
	return r, true
}

// prune keeps what Pyright reads (sources, stubs, py.typed markers, .pth
// search paths and distribution metadata) and removes native libraries,
// data and scripts. It returns the bytes kept.
func prune(root string) (int64, error) {
	var kept int64
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if d.Name() == "__pycache__" {
				if err := os.RemoveAll(p); err != nil {
					return err
				}
				return filepath.SkipDir
			}
			return nil
		}
		rel, _ := filepath.Rel(root, p)
		keep := d.Type().IsRegular() && (strings.HasSuffix(p, ".py") || strings.HasSuffix(p, ".pyi") || d.Name() == "py.typed" ||
			(strings.HasSuffix(p, ".pth") && !strings.Contains(rel, string(filepath.Separator))) ||
			strings.Contains(filepath.Dir(rel), ".dist-info"))
		if !keep {
			return os.Remove(p)
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		kept += info.Size()
		return nil
	})
	return kept, err
}

// distributions lists what is installed, as name==version, from the
// distributions' metadata directories.
func distributions(root string) ([]string, error) {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, e := range entries {
		base, ok := strings.CutSuffix(e.Name(), ".dist-info")
		if !ok || !e.IsDir() {
			continue
		}
		if name, version, ok := strings.Cut(base, "-"); ok {
			out = append(out, strings.ToLower(name)+"=="+version)
		}
	}
	sort.Strings(out)
	return out, nil
}

// reason is the line of an installer's output that says why it failed.
func reason(output string, err error) string {
	var best string
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(strings.TrimLeft(line, "×╰─▶│ "))
		if line == "" || strings.HasPrefix(line, "hint:") {
			continue
		}
		if best == "" || strings.Contains(line, "Because") || strings.Contains(line, "not found") || strings.Contains(line, "no wheels") || strings.Contains(line, "Failed to") {
			best = line
		}
	}
	if best == "" {
		best = err.Error()
	}
	if len(best) > 300 {
		best = best[:300] + "…"
	}
	return best
}

// transient reports a failure that says nothing about the packages: the
// index could not be reached or answered with a server error.
func transient(why string) bool {
	lower := strings.ToLower(why)
	for _, s := range []string{"failed to fetch", "error sending request", "timed out", "timeout", "connection", "dns", "network", "503", "502", "500 internal", "tls", "certificate"} {
		if strings.Contains(lower, s) {
			return true
		}
	}
	return false
}
