// Package java owns Java application settings and build-provider composition.
package java

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/buildcontext/discover"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
	"ei-aitiger-codegraph/worker/internal/buildcontext/syntax"
	"ei-aitiger-codegraph/worker/internal/languages"
	javamaven "ei-aitiger-codegraph/worker/internal/languages/java/maven"
	javaparser "ei-aitiger-codegraph/worker/internal/parser/java"
	javaresolver "ei-aitiger-codegraph/worker/internal/resolve/java"
)

// Config is decoded only by the Java adapter. JavaHome is required: javac is
// the only binding authority. Release is an explicit syntax profile;
// FallbackRelease is used only when build discovery cannot infer one.
// BuildJavaHomes maps JDK major versions ("8") to older JDKs Maven may build
// with in maven-resolved mode; attribution always uses JavaHome. LombokJAR is
// a Lombok that runs on JavaHome, for source sets whose own Lombok does not.
type Config struct {
	Release         int               `json:"release"`
	FallbackRelease int               `json:"fallback_release"`
	JavaHome        string            `json:"java_home"`
	BuildJavaHomes  map[string]string `json:"build_java_homes,omitempty"`
	MavenExecutable string            `json:"maven_executable,omitempty"`
	CacheDir        string            `json:"cache_dir"`
	WorkDir         string            `json:"work_dir"`
	MaxHeapMiB      int               `json:"max_heap_mib,omitempty"`
	Parallelism     int               `json:"parallelism,omitempty"`
	LombokJAR       string            `json:"lombok_jar,omitempty"`
}

func DefaultConfig() Config { return Config{FallbackRelease: 21} }

func (c Config) Validate(mode string) error {
	if !supported(c.FallbackRelease) {
		return fmt.Errorf("%w: fallback_release must be 8, 11, 17 or 21", bc.ErrInvalidInput)
	}
	if mode == "syntax" {
		if !supported(c.Release) {
			return fmt.Errorf("%w: syntax mode requires release 8, 11, 17 or 21", bc.ErrInvalidInput)
		}
	} else if c.Release != 0 {
		return fmt.Errorf("%w: release is only valid in syntax mode; build metadata selects the source-set release", bc.ErrInvalidInput)
	}
	if c.JavaHome == "" || !filepath.IsAbs(c.JavaHome) {
		return fmt.Errorf("%w: java_home must be an absolute JDK path", bc.ErrInvalidInput)
	}
	for _, dir := range []string{c.CacheDir, c.WorkDir} {
		if dir == "" || !filepath.IsAbs(dir) {
			return fmt.Errorf("%w: cache_dir and work_dir must be absolute", bc.ErrInvalidInput)
		}
	}
	if _, err := c.buildJavaHomes(); err != nil {
		return err
	}
	if c.LombokJAR != "" && !filepath.IsAbs(c.LombokJAR) {
		return fmt.Errorf("%w: lombok_jar must be an absolute path", bc.ErrInvalidInput)
	}
	return nil
}

// buildJavaHomes returns BuildJavaHomes keyed by major version.
func (c Config) buildJavaHomes() (map[int]string, error) {
	if len(c.BuildJavaHomes) == 0 {
		return nil, nil
	}
	homes := make(map[int]string, len(c.BuildJavaHomes))
	for key, home := range c.BuildJavaHomes {
		major, err := strconv.Atoi(key)
		if err != nil || major < 8 || strconv.Itoa(major) != key {
			return nil, fmt.Errorf("%w: build_java_homes keys must be JDK major versions of 8 or later, not %q", bc.ErrInvalidInput, key)
		}
		if !filepath.IsAbs(home) {
			return nil, fmt.Errorf("%w: build_java_homes[%s] must be an absolute JDK path", bc.ErrInvalidInput, key)
		}
		homes[major] = home
	}
	return homes, nil
}
func supported(release int) bool {
	switch release {
	case 8, 11, 17, 21:
		return true
	default:
		return false
	}
}

func Adapter() languages.Adapter {
	return languages.Adapter{Parser: javaparser.Registration(), Configure: configure}
}

func configure(raw json.RawMessage, mode string) (languages.Configured, error) {
	cfg := DefaultConfig()
	if len(raw) > 0 {
		raw = bytes.TrimSpace(raw)
		if len(raw) == 0 || raw[0] != '{' {
			return languages.Configured{}, fmt.Errorf("%w: Java settings must be an object", bc.ErrInvalidInput)
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&cfg); err != nil {
			return languages.Configured{}, fmt.Errorf("%w: invalid Java language configuration: %v", bc.ErrInvalidInput, err)
		}
		if err := decoder.Decode(new(any)); err != io.EOF {
			return languages.Configured{}, fmt.Errorf("%w: trailing Java configuration data", bc.ErrInvalidInput)
		}
	}
	if err := cfg.Validate(mode); err != nil {
		return languages.Configured{}, err
	}
	for _, dir := range []string{cfg.CacheDir, cfg.WorkDir} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return languages.Configured{}, err
		}
	}
	resolver, err := javaresolver.New(javaresolver.Config{JavaHome: cfg.JavaHome, WorkDir: cfg.WorkDir, CacheDir: cfg.CacheDir, MaxHeapMiB: cfg.MaxHeapMiB, Parallelism: cfg.Parallelism, LombokJAR: cfg.LombokJAR})
	if err != nil {
		return languages.Configured{}, err
	}
	// The syntax profile is the explicit scan in syntax mode and the
	// repository-wide fallback when a build mode finds no Java build; outside
	// syntax mode its release is the fallback release.
	release := cfg.Release
	if release == 0 {
		release = cfg.FallbackRelease
	}
	return languages.Configured{
		Resolver:      resolver,
		SyntaxProfile: syntax.Profile{Language: "java", Version: strconv.Itoa(release), Roots: []string{"."}},
		Discover: func(mode string, inputs manifest.Config) (bc.Provider, error) {
			if mode == "maven-resolved" {
				if inputs.ManifestPath != "" && inputs.ManifestPath != manifest.DefaultPath || len(inputs.Roots) != 0 {
					return nil, fmt.Errorf("%w: resolved Maven inputs use Java-owned roots", bc.ErrInvalidInput)
				}
				// Validate accepted them.
				buildHomes, _ := cfg.buildJavaHomes()
				return javamaven.New(javamaven.Config{JavaHome: cfg.JavaHome, BuildJavaHomes: buildHomes, MavenExecutable: cfg.MavenExecutable, CacheDir: cfg.CacheDir, WorkDir: cfg.WorkDir})
			}
			return discover.New(discover.Config{Mode: mode, Manifest: inputs, FallbackRelease: cfg.FallbackRelease})
		},
	}, nil
}
