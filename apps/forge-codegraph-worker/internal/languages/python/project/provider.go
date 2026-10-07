package project

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"
	"unicode"
	"unicode/utf8"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/buildcontext/syntax"
	"ei-aitiger-codegraph/worker/internal/jsonc"
	"ei-aitiger-codegraph/worker/internal/languages/python/environment"
	"ei-aitiger-codegraph/worker/internal/languages/python/packages"
	"github.com/pelletier/go-toml/v2"
)

var DefaultExcludes = []string{"**/.venv/**", "**/venv/**", "**/site-packages/**", "**/__pycache__/**", "**/node_modules/**", "**/.git/**", "**/build/**", "**/dist/**", "**/.tox/**", "**/.mypy_cache/**", "**/.pytest_cache/**"}

type Config struct {
	Version, Platform, AnalyzerPath         string
	Roots, Dependencies, Includes, Excludes []string
	VersionExplicit, PlatformExplicit       bool
	// Install installs the packages the checkout declares; nil leaves them
	// to the dependency directories.
	Install *packages.Config
}
type Provider struct{ Config Config }

func (p Provider) Build(ctx context.Context, r bc.Request) (bc.BuildContext, error) {
	if err := r.Validate(); err != nil {
		return bc.BuildContext{}, err
	}
	s := environment.Settings{Version: p.Config.Version, Platform: p.Config.Platform, Roots: append([]string{}, p.Config.Roots...)}
	if len(s.Roots) == 0 {
		s.Roots = []string{".", "src"}
	}
	root, err := os.OpenRoot(r.Checkout.Path)
	if err != nil {
		return bc.BuildContext{}, err
	}
	defer root.Close()
	h := sha256.New()
	var notes []string
	note := func(format string, args ...any) { notes = append(notes, fmt.Sprintf(format, args...)) }
	var projectOptions map[string]any
	var requiresPython, pythonVersionFile string
	pyrightSource := "pyproject.toml [tool.pyright].pythonVersion"
	for _, name := range []string{"pyproject.toml", "pyrightconfig.json", "uv.lock", "poetry.lock", "pdm.lock", "requirements.txt", "requirements-dev.txt", "setup.cfg", "setup.py", ".python-version"} {
		f, err := root.Open(name)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return bc.BuildContext{}, err
		}
		info, err := f.Stat()
		if err != nil || !info.Mode().IsRegular() {
			f.Close()
			if err != nil {
				return bc.BuildContext{}, err
			}
			continue
		}
		// Every file is fingerprinted; only the ones that configure the
		// analysis are read, and only within the input budget. A lock file
		// of any size is hashed as it streams.
		read := name == "pyproject.toml" || name == "pyrightconfig.json" || name == ".python-version"
		fmt.Fprintf(h, "%d:%s:%d:", len(name), name, info.Size())
		if !read || uint64(info.Size()) > r.Limits.MaxInputBytes {
			_, err = io.Copy(h, io.LimitReader(f, info.Size()))
			f.Close()
			if err != nil {
				return bc.BuildContext{}, err
			}
			if read {
				note("%s is larger than %d bytes and was not read", name, r.Limits.MaxInputBytes)
			}
			continue
		}
		data, readErr := io.ReadAll(io.LimitReader(f, info.Size()))
		f.Close()
		if readErr != nil {
			return bc.BuildContext{}, readErr
		}
		h.Write(data)
		if name == "pyproject.toml" {
			var cfg struct {
				Project struct {
					RequiresPython *string `toml:"requires-python"`
				} `toml:"project"`
				Tool struct {
					Pyright map[string]any `toml:"pyright"`
				} `toml:"tool"`
			}
			if err := toml.Unmarshal(data, &cfg); err != nil {
				note("pyproject.toml could not be read (%v), so its Python settings were not used", err)
				continue
			}
			projectOptions = cfg.Tool.Pyright
			if cfg.Project.RequiresPython != nil {
				requiresPython = *cfg.Project.RequiresPython
			}
		}
		if name == "pyrightconfig.json" {
			// A JSON configuration replaces tool.pyright as a whole. Pyright
			// reads it with comments and trailing commas, and so does this.
			var options map[string]any
			if err := json.Unmarshal(jsonc.Strip(data), &options); err != nil {
				note("pyrightconfig.json could not be read (%v), so its settings were not used", err)
				continue
			}
			projectOptions = options
			pyrightSource = "pyrightconfig.json pythonVersion"
		}
		if name == ".python-version" {
			pythonVersionFile = string(data)
		}
	}
	selection, versionNotes, err := discoverVersion(p.Config, projectOptions, pyrightSource, pythonVersionFile, requiresPython)
	if err != nil {
		return bc.BuildContext{}, err
	}
	notes = append(notes, versionNotes...)
	s.Version, s.VersionSource, s.VersionRequest = selection.version, selection.source, selection.request
	s.RequiresPython = requiresPython
	if value, ok := projectOptions["pythonPlatform"].(string); ok && !p.Config.PlatformExplicit {
		switch value {
		case "Linux", "Darwin", "Windows":
			s.Platform = value
		default:
			note("pythonPlatform %q is not one platform (Linux, Darwin or Windows); %s is analysed", value, s.Platform)
		}
	}
	if raw, ok := projectOptions["extraPaths"]; ok && len(p.Config.Roots) == 0 {
		entries, _ := raw.([]any)
		if entries == nil {
			note("extraPaths is not a list of paths and was not used")
		}
		for _, entry := range entries {
			extra, ok := entry.(string)
			clean := path.Clean(strings.ReplaceAll(extra, "\\", "/"))
			if !ok || extra == "" || clean == ".." || strings.HasPrefix(clean, "../") || path.IsAbs(clean) || strings.ContainsAny(clean, ":") || !fs.ValidPath(clean) {
				note("extraPaths entry %v is not a path inside the checkout and was not used", entry)
				continue
			}
			s.Roots = append(s.Roots, clean)
		}
	}
	// The executionEnvironments of the repository's Pyright configuration say
	// where each part's imports resolve. Without them, the projects nested in
	// the checkout are found by their manifests.
	if raw, ok := projectOptions["executionEnvironments"]; ok && raw != nil {
		if envs, isList := raw.([]any); !isList || len(envs) > 0 {
			projects, roots, envNotes := declaredEnvironments(raw)
			s.Projects = projects
			if len(p.Config.Roots) == 0 {
				s.Roots = append(s.Roots, roots...)
			}
			notes = append(notes, envNotes...)
		}
	}
	if s.Projects == nil {
		projects, projectNotes, err := discoverProjects(ctx, root, r.Limits, append(append([]string{}, DefaultExcludes...), p.Config.Excludes...), h)
		if err != nil {
			return bc.BuildContext{}, err
		}
		s.Projects = projects
		notes = append(notes, projectNotes...)
	}
	var installed string
	if p.Config.Install != nil {
		var dirs []string
		for _, project := range s.Projects {
			if project.Shared {
				dirs = append(dirs, project.Root)
			}
		}
		declared, err := declaredPackages(root, dirs, r.Limits, h)
		if err != nil {
			return bc.BuildContext{}, err
		}
		notes = append(notes, declared.Notes...)
		result, err := packages.Install(ctx, *p.Config.Install, packages.Request{Version: s.Version, Platform: s.Platform, Requirements: declared.Requirements, Pins: declared.Pins})
		switch {
		case err != nil && ctx.Err() != nil:
			return bc.BuildContext{}, ctx.Err()
		case err != nil:
			note("the packages the checkout declares were not installed (%v); code using them resolves to their names only", err)
		default:
			installed = result.Dir
			if len(result.Failed) > 0 {
				var failed []string
				for _, f := range result.Failed[:min(len(result.Failed), 8)] {
					failed = append(failed, f.Requirement+" ("+f.Reason+")")
				}
				more := ""
				if len(result.Failed) > len(failed) {
					more = fmt.Sprintf(" and %d more", len(result.Failed)-len(failed))
				}
				note("%d of the packages the checkout declares could not be installed and resolve to their names only: %s%s", len(result.Failed), strings.Join(failed, "; "), more)
			}
		}
	}
	s.ConfigDigest = hex.EncodeToString(h.Sum(nil))
	budget := environment.TreeBudget{Bytes: r.Limits.MaxHashBytes, Files: r.Limits.MaxFiles, Depth: r.Limits.MaxDepth}
	for _, dir := range p.Config.Dependencies {
		if !filepath.IsAbs(dir) {
			return bc.BuildContext{}, fmt.Errorf("%w: dependency directories must be absolute", bc.ErrInvalidInput)
		}
		digest, err := environment.DigestTreeWithBudget(ctx, dir, &budget)
		if err != nil {
			return bc.BuildContext{}, err
		}
		s.Dependencies = append(s.Dependencies, environment.Dependency{Path: dir, SHA256: digest})
	}
	// Installed packages come after the operator's directories, whose
	// stubs are deliberate. They are fingerprinted like those, so the
	// resolver can tell they did not change in between.
	if installed != "" {
		digest, err := environment.DigestTreeWithBudget(ctx, installed, &budget)
		if err != nil {
			if ctx.Err() != nil {
				return bc.BuildContext{}, ctx.Err()
			}
			note("the installed packages could not be fingerprinted (%v) and were not used", err)
		} else {
			s.Dependencies = append(s.Dependencies, environment.Dependency{Path: installed, SHA256: digest})
		}
	}
	bridge := environment.AnalyzerPath(p.Config.AnalyzerPath)
	if _, err := os.Stat(bridge); err == nil {
		s.AnalyzerDigest, err = environment.DigestTreeWithBudget(ctx, filepath.Dir(bridge), &budget)
		if err != nil {
			return bc.BuildContext{}, err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return bc.BuildContext{}, err
	}
	settings, err := environment.Encode(s)
	if err != nil {
		return bc.BuildContext{}, err
	}
	provider, err := syntax.NewProfiles(syntax.Profile{Language: "python", Version: s.Version, Roots: []string{"."}, Settings: settings, Excludes: append(append([]string{}, DefaultExcludes...), p.Config.Excludes...), Includes: p.Config.Includes})
	if err != nil {
		return bc.BuildContext{}, err
	}
	// Syntax provider intentionally marks build knowledge incomplete. A static
	// source/stub inventory does not certify that an application can execute.
	c, err := provider.WithProducer("python-environment", "1.1.0").Build(ctx, r)
	if err != nil || len(notes) == 0 {
		return c, err
	}
	for i, n := range notes {
		c.Inventory.MissingInputs = append(c.Inventory.MissingInputs, bc.MissingInput{ID: bc.GapID(fmt.Sprintf("python-configuration-%d", i+1)), Requested: bc.GapConfiguration, Reason: gapText("Python: " + n + ".")})
	}
	return bc.Seal(c)
}

// gapText is text a gap can carry: one line, no control characters, at
// most 2 KB.
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
