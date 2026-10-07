// Package project discovers the TypeScript project configuration of a
// checkout without running anything: the path mappings of every
// tsconfig/jsconfig and the workspace packages importable by name. It
// describes the checkout as one repository-wide TypeScript source set whose
// language options carry that configuration for the syntax-tier resolver,
// so a configuration change re-resolves the set like any build input.
package project

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/buildcontext/syntax"
	"ei-aitiger-codegraph/worker/internal/languages/typescript/packages"
	tsresolve "ei-aitiger-codegraph/worker/internal/resolve/typescript"
)

// Version changes when discovery reads other files or writes other options.
const Version = "1.2.0"

// Language is the source-set language the provider describes.
const Language = "typescript"

// Config is the TypeScript language level of the profile and the source
// selection an operator adds to it.
type Config struct {
	Version string
	// Excludes are extra source globs to skip; Includes restrict the walk.
	Excludes []string
	Includes []string
	// DefaultExcludes applies the built-in build-output exclusions.
	DefaultExcludes bool
	// Compiler fingerprints the compiler tier's bridge; empty without it.
	// The project files the compiler reads are fingerprinted with it.
	Compiler string
	// Install installs the npm projects' packages for the compiler tier;
	// nil leaves node_modules out.
	Install *packages.Config
}

// DefaultExcludes are the conventional build-output and dependency
// directories a TypeScript checkout commits or leaves behind; they are
// generated from sources already in the graph and never bound.
var DefaultExcludes = []string{"**/node_modules/**", "**/target/**", "**/dist/**", "**/build/**", "**/out/**", "**/.next/**", "**/.nuxt/**", "**/.output/**", "**/coverage/**", "**/.cache/**"}

type Provider struct {
	cfg Config
}

var _ bc.Provider = (*Provider)(nil)

func New(cfg Config) (*Provider, error) {
	switch cfg.Version {
	case "4", "5":
	default:
		return nil, bc.ErrInvalidInput
	}
	for _, pattern := range append(append([]string(nil), cfg.Excludes...), cfg.Includes...) {
		if !bc.ValidSourcePattern(pattern) {
			return nil, fmt.Errorf("%w: invalid source pattern %q", bc.ErrInvalidInput, pattern)
		}
	}
	return &Provider{cfg: cfg}, nil
}

// Build scans the checkout and returns the repository-wide profile with the
// discovered configuration, or bc.ErrNoBuild when the checkout has neither
// a tsconfig/jsconfig nor a package.json, in which case the plain syntax
// fallback describes it equally well.
func (p *Provider) Build(ctx context.Context, r bc.Request) (bc.BuildContext, error) {
	if err := ctx.Err(); err != nil {
		return bc.BuildContext{}, err
	}
	if err := r.Validate(); err != nil {
		return bc.BuildContext{}, err
	}
	if p == nil {
		return bc.BuildContext{}, bc.ErrInvalidInput
	}
	root, err := os.OpenRoot(r.Checkout.Path)
	if err != nil {
		return bc.BuildContext{}, err
	}
	defer root.Close()
	settings, found, sc, err := scan(ctx, root, r.Limits)
	if err != nil {
		return bc.BuildContext{}, err
	}
	if !found {
		return bc.BuildContext{}, bc.ErrNoBuild
	}
	excludes := Excludes(settings, p.cfg)
	if p.cfg.Compiler != "" {
		settings.Compiler = sc.fingerprint(p.cfg.Compiler)
	}
	var notes []string
	if p.cfg.Install != nil {
		for _, project := range sc.npmProjects(excludes) {
			result, err := packages.Install(ctx, *p.cfg.Install, project)
			label := "the npm project at " + project.Dir
			if project.Dir == "." {
				label = "the checkout's npm project"
			}
			switch {
			case err != nil && ctx.Err() != nil:
				return bc.BuildContext{}, ctx.Err()
			case err != nil:
				notes = append(notes, fmt.Sprintf("the packages of %s were not installed (%v); code using them resolves to their names only", label, err))
				continue
			}
			settings.Installs = append(settings.Installs, tsresolve.Install{Dir: project.Dir, Path: result.Dir, SHA256: result.Digest})
			if len(result.Failed) > 0 {
				var failed []string
				for _, f := range result.Failed[:min(len(result.Failed), 8)] {
					failed = append(failed, f.Package+" ("+f.Reason+")")
				}
				more := ""
				if len(result.Failed) > len(failed) {
					more = fmt.Sprintf(" and %d more", len(result.Failed)-len(failed))
				}
				notes = append(notes, fmt.Sprintf("%d packages of %s were left out and resolve to their names only: %s%s", len(result.Failed), label, strings.Join(failed, "; "), more))
			}
		}
	}
	options, err := tsresolve.EncodeSettings(settings)
	if err != nil {
		return bc.BuildContext{}, err
	}
	provider, err := syntax.NewProfiles(syntax.Profile{Language: Language, Version: p.cfg.Version, Roots: []string{"."}, Settings: options, Excludes: excludes, Includes: append([]string(nil), p.cfg.Includes...)})
	if err != nil {
		return bc.BuildContext{}, err
	}
	c, err := provider.WithProducer("typescript-project", Version).Build(ctx, r)
	if err != nil || len(notes) == 0 {
		return c, err
	}
	for i, n := range notes {
		c.Inventory.MissingInputs = append(c.Inventory.MissingInputs, bc.MissingInput{ID: bc.GapID(fmt.Sprintf("typescript-packages-%d", i+1)), Requested: bc.GapConfiguration, Reason: gapText("TypeScript: " + n + ".")})
	}
	return bc.Seal(c)
}

// fingerprint digests the bridge's fingerprint with every project file the
// compiler reads: tsconfig and jsconfig files with their variants, and
// package.json files.
func (s *scanner) fingerprint(bridge string) string {
	h := sha256.New()
	fmt.Fprintf(h, "bridge:%s\n", bridge)
	files := append(append(append([]string(nil), s.configs...), s.variants...), s.pkgs...)
	sort.Strings(files)
	for _, name := range files {
		data, _ := s.read(name)
		fmt.Fprintf(h, "%d:%s:%d:", len(name), name, len(data))
		h.Write(data)
	}
	return hex.EncodeToString(h.Sum(nil))
}

// gapText is a single bounded line.
func gapText(text string) string {
	text = strings.Join(strings.Fields(strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return ' '
		}
		return r
	}, strings.ToValidUTF8(text, "?"))), " ")
	if len(text) > 2048 {
		cut := 2045
		for cut > 0 && !utf8.RuneStart(text[cut]) {
			cut--
		}
		text = text[:cut] + "..."
	}
	return text
}

// Excludes are the source globs the walker skips for a TypeScript profile:
// the default build-output directories (unless disabled), every project's
// output directory, and the operator's own patterns. `node_modules` is
// always skipped.
func Excludes(settings tsresolve.Settings, cfg Config) []string {
	seen := map[string]bool{}
	var out []string
	add := func(pattern string) {
		if pattern != "" && !seen[pattern] && bc.ValidSourcePattern(pattern) {
			seen[pattern] = true
			out = append(out, pattern)
		}
	}
	add("**/node_modules/**")
	if cfg.DefaultExcludes {
		for _, pattern := range DefaultExcludes {
			add(pattern)
		}
	}
	for _, project := range settings.Projects {
		if project.OutDir == "" || project.OutDir == "." || strings.HasPrefix(project.OutDir, "..") {
			continue
		}
		add(path.Join(project.OutDir, "**"))
	}
	for _, pattern := range cfg.Excludes {
		add(pattern)
	}
	sort.Strings(out)
	return out
}
