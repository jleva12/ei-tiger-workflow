// Package typescript owns the TypeScript and JavaScript adapter settings
// and composes its parser, syntax profile, project discovery provider and
// syntax-tier resolver. The provider describes the checkout as one
// repository-wide source set carrying tsconfig path mappings and workspace
// packages; without any project configuration the syntax fallback covers
// the files instead.
package typescript

import (
	"bytes"
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
	"ei-aitiger-codegraph/worker/internal/languages/typescript/packages"
	"ei-aitiger-codegraph/worker/internal/languages/typescript/project"
	tsparser "ei-aitiger-codegraph/worker/internal/parser/typescript"
	tsresolve "ei-aitiger-codegraph/worker/internal/resolve/typescript"
)

// Config is decoded only by this adapter. Version is the TypeScript
// language level the syntax profile parses under (4 or 5); JavaScript files
// share it.
type Config struct {
	Version string `json:"version,omitempty"`
	// Excludes are extra source globs the walk skips (`dev/browser/**`);
	// Includes restrict it to matching files. DefaultExcludes, true unless
	// set to false, applies the built-in build-output list
	// (project.DefaultExcludes); node_modules is always skipped.
	Excludes        []string `json:"excludes,omitempty"`
	Includes        []string `json:"includes,omitempty"`
	DefaultExcludes *bool    `json:"default_excludes,omitempty"`
	// Compiler runs the TypeScript compiler over the sites the syntax tier
	// bound; Install installs the npm projects' packages for it.
	Compiler CompilerConfig `json:"compiler,omitempty"`
	Install  InstallConfig  `json:"install,omitempty"`
}

// CompilerConfig is the compiler tier: on when node and the built bridge
// are found, unless enabled is false.
type CompilerConfig struct {
	Enabled        *bool  `json:"enabled,omitempty"`
	NodePath       string `json:"node_path,omitempty"`
	AnalyzerPath   string `json:"analyzer_path,omitempty"`
	MaxHeapMiB     int    `json:"max_heap_mib,omitempty"`
	TimeoutSeconds int    `json:"timeout_seconds,omitempty"`
}

// InstallConfig installs the packages of the checkout's npm projects, with
// every lifecycle script off, for the compiler to read: on with the
// compiler when npm is found, unless enabled is false.
type InstallConfig struct {
	Enabled        *bool    `json:"enabled,omitempty"`
	NPMPath        string   `json:"npm_path,omitempty"`
	Registry       string   `json:"registry,omitempty"`
	CacheDir       string   `json:"cache_dir,omitempty"`
	MaxMiB         int      `json:"max_mib,omitempty"`
	TimeoutSeconds int      `json:"timeout_seconds,omitempty"`
	Exclude        []string `json:"exclude,omitempty"`
}

var packageName = regexp.MustCompile(`^(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$`)

// compiler checks the compiler settings; nil turns the tier off.
func compiler(c CompilerConfig, mode string) (*tsresolve.Compiler, string, error) {
	explicit := c.Enabled != nil && *c.Enabled
	if c.Enabled != nil && !*c.Enabled {
		return nil, "", nil
	}
	fail := func(format string, args ...any) (*tsresolve.Compiler, string, error) {
		if explicit {
			return nil, "", fmt.Errorf("%w: the TypeScript compiler tier "+format, append([]any{bc.ErrInvalidInput}, args...)...)
		}
		slog.Info("TypeScript is bound by syntax alone: " + fmt.Sprintf(format, args...))
		return nil, "", nil
	}
	if mode == "syntax" || mode == "manifest" {
		return fail("runs in auto discovery only")
	}
	node := c.NodePath
	if node == "" {
		node = "node"
	}
	nodePath, err := exec.LookPath(node)
	if err != nil {
		return fail("needs node (%v)", err)
	}
	bridge := tsresolve.AnalyzerPath(c.AnalyzerPath)
	if info, err := os.Stat(bridge); err != nil || !info.Mode().IsRegular() {
		return fail("needs its bridge at %s (npm ci && npm run build in typescript-analyzer)", bridge)
	}
	heap, timeout := c.MaxHeapMiB, c.TimeoutSeconds
	if heap == 0 {
		heap = tsresolve.DefaultCompilerHeapMiB
	}
	if timeout == 0 {
		timeout = 900
	}
	if heap < 256 || heap > 32768 || timeout < 1 || timeout > 7200 {
		return nil, "", fmt.Errorf("%w: TypeScript compiler budget outside supported range", bc.ErrInvalidInput)
	}
	digest, err := tsresolve.BridgeDigest(bridge)
	if err != nil {
		return nil, "", err
	}
	return &tsresolve.Compiler{NodePath: nodePath, AnalyzerPath: bridge, MaxHeapMiB: heap, Timeout: time.Duration(timeout) * time.Second}, digest, nil
}

// installer checks the install settings; nil installs nothing.
func installer(c InstallConfig, withCompiler bool) (*packages.Config, error) {
	explicit := c.Enabled != nil && *c.Enabled
	if c.Enabled != nil && !*c.Enabled {
		return nil, nil
	}
	if !withCompiler {
		if explicit {
			return nil, fmt.Errorf("%w: installing TypeScript packages needs the compiler tier", bc.ErrInvalidInput)
		}
		return nil, nil
	}
	name := c.NPMPath
	if name == "" {
		name = "npm"
	}
	npm, err := exec.LookPath(name)
	if err != nil {
		if explicit || c.NPMPath != "" {
			return nil, fmt.Errorf("%w: installing TypeScript packages needs npm (%v)", bc.ErrInvalidInput, err)
		}
		slog.Info("the packages of TypeScript checkouts are not installed for the analysis: npm is not on PATH")
		return nil, nil
	}
	version, err := exec.Command(npm, "--version").Output()
	if err != nil {
		return nil, fmt.Errorf("%w: npm --version: %v", bc.ErrInvalidInput, err)
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
			return nil, fmt.Errorf("%w: set install.cache_dir for TypeScript packages", bc.ErrInvalidInput)
		}
		cache = filepath.Join(base, "typescript-packages")
	}
	if cache, err = filepath.Abs(cache); err != nil {
		return nil, err
	}
	if c.Registry != "" {
		u, err := url.Parse(c.Registry)
		if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
			return nil, fmt.Errorf("%w: install.registry must be an http(s) URL", bc.ErrInvalidInput)
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
		return nil, fmt.Errorf("%w: TypeScript package installation budget outside supported range", bc.ErrInvalidInput)
	}
	for _, name := range c.Exclude {
		if !packageName.MatchString(name) {
			return nil, fmt.Errorf("%w: install.exclude entry %q is not a package name", bc.ErrInvalidInput, name)
		}
	}
	return &packages.Config{NPM: npm, NPMVersion: strings.TrimSpace(string(version)), Registry: c.Registry, CacheDir: cache, MaxBytes: int64(maxMiB) << 20, Timeout: time.Duration(timeout) * time.Second, Exclude: append([]string(nil), c.Exclude...)}, nil
}

func DefaultConfig() Config { return Config{Version: "5"} }

func (c Config) Validate() error {
	switch c.Version {
	case "4", "5":
	default:
		return fmt.Errorf("%w: version must be 4 or 5", bc.ErrInvalidInput)
	}
	for _, pattern := range append(append([]string(nil), c.Excludes...), c.Includes...) {
		if !bc.ValidSourcePattern(pattern) {
			return fmt.Errorf("%w: invalid source pattern %q (relative glob required)", bc.ErrInvalidInput, pattern)
		}
	}
	return nil
}

// projectConfig is the selection the discovery provider applies.
func (c Config) projectConfig() project.Config {
	return project.Config{Version: c.Version, Excludes: c.Excludes, Includes: c.Includes, DefaultExcludes: c.DefaultExcludes == nil || *c.DefaultExcludes}
}

func Adapter() languages.Adapter {
	return languages.Adapter{Parser: tsparser.Registration(), Configure: configure}
}

func configure(raw json.RawMessage, mode string) (languages.Configured, error) {
	cfg := DefaultConfig()
	if len(raw) > 0 {
		raw = bytes.TrimSpace(raw)
		if len(raw) == 0 || raw[0] != '{' {
			return languages.Configured{}, fmt.Errorf("%w: TypeScript settings must be an object", bc.ErrInvalidInput)
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&cfg); err != nil {
			return languages.Configured{}, fmt.Errorf("%w: invalid TypeScript language configuration: %v", bc.ErrInvalidInput, err)
		}
		if err := decoder.Decode(new(any)); err != io.EOF {
			return languages.Configured{}, fmt.Errorf("%w: trailing TypeScript configuration data", bc.ErrInvalidInput)
		}
	}
	if cfg.Version == "" {
		cfg.Version = DefaultConfig().Version
	}
	if err := cfg.Validate(); err != nil {
		return languages.Configured{}, err
	}
	tier, digest, err := compiler(cfg.Compiler, mode)
	if err != nil {
		return languages.Configured{}, err
	}
	install, err := installer(cfg.Install, tier != nil)
	if err != nil {
		return languages.Configured{}, err
	}
	resolver := tsresolve.New()
	if tier != nil {
		resolver = tsresolve.NewWithCompiler(*tier)
	}
	discovery := cfg.projectConfig()
	discovery.Compiler, discovery.Install = digest, install
	return languages.Configured{
		Resolver:      resolver,
		SyntaxProfile: syntax.Profile{Language: tsparser.Language, Version: cfg.Version, Roots: []string{"."}, Excludes: project.Excludes(tsresolve.Settings{}, cfg.projectConfig()), Includes: append([]string(nil), cfg.Includes...)},
		// Project discovery reads tsconfig/jsconfig path mappings and
		// workspace packages in every build mode; with the compiler tier it
		// installs the npm projects' packages, with every script off.
		Discover: func(string, manifest.Config) (bc.Provider, error) {
			return project.New(discovery)
		},
	}, nil
}
