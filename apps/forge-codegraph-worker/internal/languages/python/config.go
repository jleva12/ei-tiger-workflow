// Package python composes Python discovery, Tree-sitter syntax extraction and
// Pyright semantic resolution through the language-neutral worker registry.
package python

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
	"ei-aitiger-codegraph/worker/internal/buildcontext/syntax"
	"ei-aitiger-codegraph/worker/internal/languages"
	"ei-aitiger-codegraph/worker/internal/languages/python/environment"
	"ei-aitiger-codegraph/worker/internal/languages/python/packages"
	"ei-aitiger-codegraph/worker/internal/languages/python/project"
	pyparser "ei-aitiger-codegraph/worker/internal/parser/python"
	pyresolve "ei-aitiger-codegraph/worker/internal/resolve/python"
)

type Config struct {
	Version        string        `json:"version,omitempty"`
	Platform       string        `json:"platform,omitempty"`
	Roots          []string      `json:"roots,omitempty"`
	Dependencies   []string      `json:"dependencies,omitempty"`
	Install        InstallConfig `json:"install,omitempty"`
	Includes       []string      `json:"includes,omitempty"`
	Excludes       []string      `json:"excludes,omitempty"`
	NodePath       string        `json:"node_path,omitempty"`
	AnalyzerPath   string        `json:"analyzer_path,omitempty"`
	MaxHeapMiB     int           `json:"max_heap_mib,omitempty"`
	TimeoutSeconds int           `json:"timeout_seconds,omitempty"`
}

// InstallConfig installs the third-party packages the checkout declares,
// from wheels of the operator's index, for the analysis to follow. It is on
// when uv is found, unless enabled is false.
type InstallConfig struct {
	Enabled        *bool    `json:"enabled,omitempty"`
	UVPath         string   `json:"uv_path,omitempty"`
	IndexURL       string   `json:"index_url,omitempty"`
	CacheDir       string   `json:"cache_dir,omitempty"`
	MaxMiB         int      `json:"max_mib,omitempty"`
	TimeoutSeconds int      `json:"timeout_seconds,omitempty"`
	Exclude        []string `json:"exclude,omitempty"`
}

// installer checks the install settings. It returns nil when installation
// is off: disabled, not in auto discovery, or uv not found when it was not
// asked for explicitly.
func installer(c InstallConfig, mode string) (*packages.Config, error) {
	explicit := c.Enabled != nil && *c.Enabled
	if c.Enabled != nil && !*c.Enabled {
		return nil, nil
	}
	if mode == "syntax" || mode == "manifest" {
		if explicit {
			return nil, fmt.Errorf("%w: installing Python packages requires auto discovery", bc.ErrInvalidInput)
		}
		return nil, nil
	}
	name := c.UVPath
	if name == "" {
		name = "uv"
	}
	uv, err := exec.LookPath(name)
	if err != nil {
		if explicit || c.UVPath != "" {
			return nil, fmt.Errorf("%w: Python package installation needs uv (%v); install it, set install.uv_path, or set install.enabled to false", bc.ErrInvalidInput, err)
		}
		slog.Info("the packages Python checkouts declare are not installed for the analysis: uv is not on PATH; code using them resolves to their names only")
		return nil, nil
	}
	cache := c.CacheDir
	if cache == "" {
		base := os.Getenv("CODEGRAPH_WORK_DIR")
		if base == "" {
			if dir, err := os.UserCacheDir(); err == nil {
				base = filepath.Join(dir, "codegraph")
			}
		}
		if base == "" {
			return nil, fmt.Errorf("%w: set install.cache_dir for Python packages", bc.ErrInvalidInput)
		}
		cache = filepath.Join(base, "python-packages")
	}
	if cache, err = filepath.Abs(cache); err != nil {
		return nil, err
	}
	if c.IndexURL != "" {
		u, err := url.Parse(c.IndexURL)
		if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
			return nil, fmt.Errorf("%w: install.index_url must be an http(s) URL", bc.ErrInvalidInput)
		}
	}
	maxMiB, timeout := c.MaxMiB, c.TimeoutSeconds
	if maxMiB == 0 {
		maxMiB = 4096
	}
	if timeout == 0 {
		timeout = 900
	}
	if maxMiB < 64 || maxMiB > 1<<16 || timeout < 1 || timeout > 7200 {
		return nil, fmt.Errorf("%w: Python package installation budget outside supported range", bc.ErrInvalidInput)
	}
	exclude := append([]string{}, packages.DefaultExclude...)
	for _, name := range c.Exclude {
		if !packageName.MatchString(name) {
			return nil, fmt.Errorf("%w: install.exclude entry %q is not a package name", bc.ErrInvalidInput, name)
		}
		exclude = append(exclude, strings.ToLower(name))
	}
	return &packages.Config{UV: uv, IndexURL: c.IndexURL, CacheDir: cache, MaxBytes: int64(maxMiB) << 20, Timeout: time.Duration(timeout) * time.Second, Exclude: exclude}, nil
}

var packageName = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$`)

func DefaultConfig() Config {
	return Config{Version: "3.12", Platform: "Linux", MaxHeapMiB: pyresolve.DefaultMaxHeapMiB, TimeoutSeconds: 300}
}
func Adapter() languages.Adapter {
	return languages.Adapter{Parser: pyparser.Registration(), Configure: configure}
}
func configure(raw json.RawMessage, mode string) (languages.Configured, error) {
	c := DefaultConfig()
	if len(raw) > 0 {
		if strings.TrimSpace(string(raw)) == "null" {
			return languages.Configured{}, bc.ErrInvalidInput
		}
		d := json.NewDecoder(strings.NewReader(string(raw)))
		d.DisallowUnknownFields()
		if err := d.Decode(&c); err != nil {
			return languages.Configured{}, err
		}
		if err := d.Decode(new(any)); err != io.EOF {
			return languages.Configured{}, bc.ErrInvalidInput
		}
	}
	settings := environment.Settings{Version: c.Version, Platform: c.Platform, Roots: c.Roots}
	if len(settings.Roots) == 0 {
		settings.Roots = []string{".", "src"}
	}
	options, err := environment.Encode(settings)
	if err != nil {
		return languages.Configured{}, err
	}
	for _, pattern := range append(append([]string{}, c.Includes...), c.Excludes...) {
		if !bc.ValidSourcePattern(pattern) {
			return languages.Configured{}, fmt.Errorf("%w: invalid Python source glob", bc.ErrInvalidInput)
		}
	}
	if c.MaxHeapMiB < 128 || c.MaxHeapMiB > 16384 || c.TimeoutSeconds < 1 || c.TimeoutSeconds > 3600 {
		return languages.Configured{}, fmt.Errorf("%w: Python analyzer budget outside supported range", bc.ErrInvalidInput)
	}
	if (mode == "syntax" || mode == "manifest") && len(c.Dependencies) > 0 {
		return languages.Configured{}, fmt.Errorf("%w: Python dependency settings require auto discovery; in manifest mode put fingerprinted dependencies in python.environment", bc.ErrInvalidInput)
	}
	install, err := installer(c.Install, mode)
	if err != nil {
		return languages.Configured{}, err
	}
	pc := project.Config{Version: c.Version, Platform: c.Platform, AnalyzerPath: c.AnalyzerPath, Roots: c.Roots, Dependencies: c.Dependencies, Install: install, Includes: c.Includes, Excludes: c.Excludes}
	var supplied map[string]json.RawMessage
	_ = json.Unmarshal(raw, &supplied)
	_, pc.VersionExplicit = supplied["version"]
	_, pc.PlatformExplicit = supplied["platform"]
	return languages.Configured{
		SyntaxProfile: syntax.Profile{Language: "python", Version: c.Version, Roots: []string{"."}, Settings: options, Includes: c.Includes, Excludes: append(append([]string{}, project.DefaultExcludes...), c.Excludes...)},
		Discover:      func(string, manifest.Config) (bc.Provider, error) { return project.Provider{Config: pc}, nil },
		Resolver:      pyresolve.New(pyresolve.Config{NodePath: c.NodePath, AnalyzerPath: c.AnalyzerPath, MaxHeapMiB: c.MaxHeapMiB, Timeout: time.Duration(c.TimeoutSeconds) * time.Second}),
	}, nil
}
