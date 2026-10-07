// Package environment describes the static Python analysis inputs. It never
// launches Python, installs packages, executes .pth files or reads host sys.path.
package environment

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

const Option = "python.environment"

type Dependency struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

// Project is a part of the checkout analysed as its own Python project: a
// directory with a pyproject.toml, setup.cfg or setup.py, or an execution
// environment the repository's Pyright configuration declares. Its files
// resolve imports from Root, then from Paths (where its packages are, such
// as src), then from the checkout's roots and the shared projects. A shared
// project is importable from the rest of the checkout.
type Project struct {
	Root   string   `json:"root"`
	Paths  []string `json:"paths,omitempty"`
	Shared bool     `json:"shared,omitempty"`
}

type Settings struct {
	Version        string       `json:"version"`
	VersionSource  string       `json:"version_source,omitempty"`
	VersionRequest string       `json:"version_request,omitempty"`
	RequiresPython string       `json:"requires_python,omitempty"`
	Platform       string       `json:"platform"`
	Roots          []string     `json:"roots"`
	Projects       []Project    `json:"projects,omitempty"`
	Dependencies   []Dependency `json:"dependencies,omitempty"`
	ConfigDigest   string       `json:"config_digest,omitempty"`
	AnalyzerDigest string       `json:"analyzer_digest,omitempty"`
}

func (s Settings) Validate() error {
	switch s.Version {
	case "3.10", "3.11", "3.12", "3.13":
	default:
		return fmt.Errorf("%w: unsupported Python version %q", bc.ErrInvalidInput, s.Version)
	}
	switch s.Platform {
	case "Linux", "Darwin", "Windows":
	default:
		return fmt.Errorf("%w: Python platform must be Linux, Darwin or Windows", bc.ErrInvalidInput)
	}
	for _, root := range s.Roots {
		if root != "." && (!fs.ValidPath(root) || strings.ContainsAny(root, "\\:")) {
			return fmt.Errorf("%w: Python root must be checkout-relative: %q", bc.ErrInvalidInput, root)
		}
	}
	for _, p := range s.Projects {
		for _, dir := range append([]string{p.Root}, p.Paths...) {
			if dir == "." || !fs.ValidPath(dir) || strings.ContainsAny(dir, "\\:") {
				return fmt.Errorf("%w: Python project directory must be a checkout subdirectory: %q", bc.ErrInvalidInput, dir)
			}
		}
	}
	for _, dep := range s.Dependencies {
		if !filepath.IsAbs(dep.Path) || len(dep.SHA256) != 64 {
			return fmt.Errorf("%w: dependency needs absolute path and SHA-256", bc.ErrInvalidInput)
		}
		if _, err := hex.DecodeString(dep.SHA256); err != nil {
			return err
		}
	}
	return nil
}
func Encode(s Settings) (map[string]string, error) {
	if err := s.Validate(); err != nil {
		return nil, err
	}
	data, err := json.Marshal(s)
	return map[string]string{Option: string(data)}, err
}
func Decode(options map[string]string, version string) (Settings, error) {
	s := Settings{Version: version, Platform: "Linux", Roots: []string{".", "src"}}
	if raw := options[Option]; raw != "" {
		d := json.NewDecoder(strings.NewReader(raw))
		d.DisallowUnknownFields()
		if err := d.Decode(&s); err != nil {
			return s, err
		}
		if err := d.Decode(new(any)); err != io.EOF {
			return s, fmt.Errorf("trailing Python settings")
		}
	}
	if s.Version != version {
		return s, fmt.Errorf("Python environment and source profile versions differ")
	}
	return s, s.Validate()
}

// DigestTree fingerprints paths and bytes in deterministic lexical order.
// Symlinks are rejected so an inventory cannot silently depend on other roots.
func DigestTree(ctx context.Context, root string, maxBytes uint64) (string, error) {
	budget := TreeBudget{Bytes: maxBytes, Files: 250_000, Depth: 128}
	return DigestTreeWithBudget(ctx, root, &budget)
}

// TreeBudget is shared across input roots to enforce aggregate resource limits.
type TreeBudget struct {
	Bytes, Files uint64
	Depth        uint32
}

func DigestTreeWithBudget(ctx context.Context, root string, budget *TreeBudget) (string, error) {
	h := sha256.New()
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if d.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("Python analysis input is a symlink: %s", p)
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return err
		}
		if rel != "." && uint32(strings.Count(filepath.ToSlash(rel), "/")+1) > budget.Depth {
			return fmt.Errorf("%w: Python environment depth budget", bc.ErrLimitExceeded)
		}
		if d.IsDir() {
			if d.Name() == "__pycache__" {
				return filepath.SkipDir
			}
			fmt.Fprintf(h, "dir:%d:%s:", len(filepath.ToSlash(rel)), filepath.ToSlash(rel))
			return nil
		}
		if !d.Type().IsRegular() {
			return fmt.Errorf("Python input is not a regular file: %s", p)
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if budget.Files == 0 {
			return fmt.Errorf("%w: Python environment file budget", bc.ErrLimitExceeded)
		}
		budget.Files--
		if info.Size() < 0 || uint64(info.Size()) > budget.Bytes {
			return fmt.Errorf("%w: Python environment byte budget", bc.ErrLimitExceeded)
		}
		budget.Bytes -= uint64(info.Size())
		f, err := os.Open(p)
		if err != nil {
			return err
		}
		defer f.Close()
		fmt.Fprintf(h, "%d:%s:%d:", len(filepath.ToSlash(rel)), filepath.ToSlash(rel), info.Size())
		n, err := io.Copy(h, io.LimitReader(contextReader{ctx, f}, info.Size()+1))
		if err != nil {
			return err
		}
		if n != info.Size() {
			return fmt.Errorf("Python environment changed during fingerprint")
		}
		return nil
	})
	return hex.EncodeToString(h.Sum(nil)), err
}

type contextReader struct {
	ctx context.Context
	r   io.Reader
}

func (r contextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.r.Read(p)
}

func AnalyzerPath(configured string) string {
	if configured != "" {
		return configured
	}
	if p := os.Getenv("CODEGRAPH_PYTHON_ANALYZER"); p != "" {
		return p
	}
	if _, err := os.Stat("/opt/codegraph/python/bridge.cjs"); err == nil {
		return "/opt/codegraph/python/bridge.cjs"
	}
	cwd, _ := os.Getwd()
	for p := cwd; p != ""; p = filepath.Dir(p) {
		candidate := filepath.Join(p, "apps/forge-codegraph-worker/python-analyzer/dist/bridge.cjs")
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
		if filepath.Dir(p) == p {
			break
		}
	}
	return filepath.Join(cwd, "apps/forge-codegraph-worker/python-analyzer/dist/bridge.cjs")
}
