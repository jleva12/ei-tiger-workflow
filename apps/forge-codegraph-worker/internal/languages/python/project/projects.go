package project

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"sort"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/languages/python/environment"
	"github.com/pelletier/go-toml/v2"
)

// A monorepo keeps several Python projects in one checkout (apps/api,
// packages/common). Each is analysed as it runs: its imports resolve from its
// own directory first, then from the other projects, the way path
// dependencies installed in development mode are found.

// maxProjects bounds how many nested projects get an environment of their
// own; files of the rest resolve in the checkout's.
const maxProjects = 256

// projectManifests mark a directory as a Python project.
var projectManifests = []string{"pyproject.toml", "setup.cfg", "setup.py"}

// fixtureDirs hold sample projects the checkout's own code does not import:
// test fixtures, examples, documentation. Their files still resolve within
// their project, but it is not shared, so a fixture named like a real
// package cannot shadow that package.
var fixtureDirs = map[string]bool{"test": true, "tests": true, "testing": true, "testdata": true, "test_data": true,
	"fixtures": true, "__fixtures__": true, "example": true, "examples": true, "sample": true, "samples": true, "docs": true}

// discoverProjects finds the Python projects nested in the checkout and the
// directories their packages are in. Every manifest read is written to h, so
// a layout change invalidates the context.
func discoverProjects(ctx context.Context, root *os.Root, limits bc.Limits, excludes []string, h io.Writer) ([]environment.Project, []string, error) {
	var dirs, notes []string
	visited := uint64(0)
	err := fs.WalkDir(root.FS(), ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			if p == "." {
				return err
			}
			return nil // an unreadable directory holds no project we can analyse
		}
		if visited++; visited > limits.MaxFiles {
			notes = append(notes, fmt.Sprintf("the checkout has more than %d entries, so nested projects past them were not looked for", limits.MaxFiles))
			return fs.SkipAll
		}
		if !d.IsDir() {
			return nil
		}
		if p == "." {
			return nil
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if strings.HasPrefix(d.Name(), ".") || skippedDir(p, excludes) || uint32(strings.Count(p, "/")+1) > limits.MaxDepth {
			return fs.SkipDir
		}
		for _, name := range projectManifests {
			if info, err := fs.Stat(root.FS(), p+"/"+name); err == nil && info.Mode().IsRegular() {
				dirs = append(dirs, p)
				break
			}
		}
		return nil
	})
	if err != nil {
		return nil, nil, err
	}
	sort.Strings(dirs)
	if len(dirs) > maxProjects {
		notes = append(notes, fmt.Sprintf("the checkout has %d Python projects; the files of those past the first %d are analysed with the checkout's imports", len(dirs), maxProjects))
		dirs = dirs[:maxProjects]
	}
	projects := make([]environment.Project, 0, len(dirs))
	for _, dir := range dirs {
		layout, layoutNotes, err := packageDirs(root, dir, limits, h)
		if err != nil {
			return nil, nil, err
		}
		notes = append(notes, layoutNotes...)
		projects = append(projects, environment.Project{Root: dir, Paths: layout, Shared: !fixture(dir)})
	}
	return projects, notes, nil
}

// skippedDir reports a directory whose files the source excludes leave out:
// tool caches, virtual environments, installed packages.
func skippedDir(dir string, excludes []string) bool {
	for _, pattern := range excludes {
		if bc.MatchSourcePattern(pattern, dir+"/_") {
			return true
		}
	}
	return false
}

func fixture(dir string) bool {
	for _, part := range strings.Split(dir, "/") {
		if fixtureDirs[strings.ToLower(part)] {
			return true
		}
	}
	return false
}

// packageDirs are the directories under a project its packages are in, as
// its build configuration places them (Poetry, setuptools, Hatch, PDM,
// maturin), or its src directory when it has one and says nothing.
func packageDirs(root *os.Root, dir string, limits bc.Limits, h io.Writer) ([]string, []string, error) {
	var found, notes []string
	manifests := map[string][]byte{}
	for _, name := range projectManifests {
		file := dir + "/" + name
		data, readable, err := readManifest(root, file, limits, h)
		if err != nil {
			return nil, nil, err
		}
		if !readable {
			if data != nil {
				notes = append(notes, fmt.Sprintf("%s is larger than %d bytes and was not read", file, limits.MaxInputBytes))
			}
			continue
		}
		manifests[name] = data
	}
	if data := manifests["pyproject.toml"]; data != nil {
		var cfg struct {
			Tool struct {
				Poetry struct {
					Packages []struct {
						From string `toml:"from"`
					} `toml:"packages"`
				} `toml:"poetry"`
				Setuptools struct {
					PackageDir map[string]string `toml:"package-dir"`
					Packages   any               `toml:"packages"`
				} `toml:"setuptools"`
				Hatch struct {
					Build struct {
						Targets struct {
							Wheel struct {
								Packages []string `toml:"packages"`
							} `toml:"wheel"`
						} `toml:"targets"`
					} `toml:"build"`
				} `toml:"hatch"`
				PDM struct {
					Build struct {
						PackageDir string `toml:"package-dir"`
					} `toml:"build"`
				} `toml:"pdm"`
				Maturin struct {
					PythonSource string `toml:"python-source"`
				} `toml:"maturin"`
			} `toml:"tool"`
		}
		if err := toml.Unmarshal(data, &cfg); err != nil {
			notes = append(notes, fmt.Sprintf("%s/pyproject.toml could not be read (%v), so where its packages are was guessed", dir, err))
		} else {
			for _, p := range cfg.Tool.Poetry.Packages {
				found = append(found, p.From)
			}
			found = append(found, cfg.Tool.Setuptools.PackageDir[""])
			if find, ok := cfg.Tool.Setuptools.Packages.(map[string]any); ok {
				if options, ok := find["find"].(map[string]any); ok {
					if where, ok := options["where"].([]any); ok {
						for _, w := range where {
							if s, ok := w.(string); ok {
								found = append(found, s)
							}
						}
					}
				}
			}
			for _, pkg := range cfg.Tool.Hatch.Build.Targets.Wheel.Packages {
				found = append(found, path.Dir(pkg))
			}
			found = append(found, cfg.Tool.PDM.Build.PackageDir, cfg.Tool.Maturin.PythonSource)
		}
	}
	if data := manifests["setup.cfg"]; data != nil {
		found = append(found, setupCfgPackageDir(string(data)))
	}
	var dirs []string
	seen := map[string]bool{}
	for _, rel := range found {
		clean := path.Clean(strings.ReplaceAll(strings.TrimSpace(rel), "\\", "/"))
		if rel == "" || clean == "." || clean == ".." || strings.HasPrefix(clean, "../") || path.IsAbs(clean) || strings.ContainsAny(clean, ":") || !fs.ValidPath(clean) {
			continue
		}
		p := dir + "/" + clean
		if seen[p] {
			continue
		}
		if info, err := fs.Stat(root.FS(), p); err == nil && info.IsDir() {
			seen[p] = true
			dirs = append(dirs, p)
		}
	}
	if len(dirs) == 0 {
		if info, err := fs.Stat(root.FS(), dir+"/src"); err == nil && info.IsDir() {
			dirs = append(dirs, dir+"/src")
		}
	}
	return dirs, notes, nil
}

// readManifest writes a project manifest's name and bytes to h. It returns
// the bytes when they are within the input budget; a larger file is hashed
// as it streams and returned as an empty, unreadable manifest.
func readManifest(root *os.Root, name string, limits bc.Limits, h io.Writer) ([]byte, bool, error) {
	f, err := root.Open(name)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, nil // unreadable: not a manifest this analysis can use
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return nil, false, nil
	}
	fmt.Fprintf(h, "project:%d:%s:%d:", len(name), name, info.Size())
	if uint64(info.Size()) > limits.MaxInputBytes {
		if _, err := io.Copy(h, io.LimitReader(f, info.Size())); err != nil {
			return nil, false, err
		}
		return []byte{}, false, nil
	}
	data, err := io.ReadAll(io.LimitReader(f, info.Size()))
	if err != nil {
		return nil, false, err
	}
	h.Write(data)
	return data, true, nil
}

// setupCfgPackageDir reads the root package directory of setup.cfg's
// [options] package_dir, as in "package_dir = =src" or on an indented line
// of its own.
func setupCfgPackageDir(text string) string {
	section, inValue := "", false
	for _, line := range strings.Split(text, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "[") && strings.HasSuffix(trimmed, "]") {
			section, inValue = strings.ToLower(strings.Trim(trimmed, "[]")), false
			continue
		}
		if section != "options" || trimmed == "" || strings.HasPrefix(trimmed, "#") || strings.HasPrefix(trimmed, ";") {
			continue
		}
		indented := line != strings.TrimLeft(line, " \t")
		value := ""
		if !indented {
			key, rest, ok := strings.Cut(trimmed, "=")
			inValue = ok && strings.TrimSpace(key) == "package_dir"
			if !inValue {
				continue
			}
			value = strings.TrimSpace(rest)
		} else if inValue {
			value = trimmed
		}
		// Each entry maps a package to a directory; the empty package is
		// the root one.
		if key, dir, ok := strings.Cut(value, "="); ok && strings.TrimSpace(key) == "" {
			return strings.TrimSpace(dir)
		}
	}
	return ""
}

// declaredEnvironments reads the executionEnvironments of the repository's
// Pyright configuration: each root's files resolve from it, then from its
// extraPaths. The extraPaths of an environment of the whole checkout (root
// ".") are returned as roots of the checkout's own.
func declaredEnvironments(raw any) ([]environment.Project, []string, []string) {
	entries, ok := raw.([]any)
	if !ok {
		return nil, nil, []string{"executionEnvironments is not a list and was not used"}
	}
	var projects []environment.Project
	var roots, notes []string
	valid := func(p string) (string, bool) {
		clean := path.Clean(strings.ReplaceAll(p, "\\", "/"))
		return clean, p != "" && clean != "." && clean != ".." && !strings.HasPrefix(clean, "../") && !path.IsAbs(clean) && !strings.ContainsAny(clean, ":") && fs.ValidPath(clean)
	}
	seen := map[string]bool{}
	for _, entry := range entries {
		env, _ := entry.(map[string]any)
		root, _ := env["root"].(string)
		whole := root != "" && path.Clean(strings.ReplaceAll(root, "\\", "/")) == "."
		clean, ok := valid(root)
		if (!ok && !whole) || seen[clean] {
			notes = append(notes, fmt.Sprintf("the executionEnvironments entry %v has no root inside the checkout and was not used", entry))
			continue
		}
		seen[clean] = true
		project := environment.Project{Root: clean}
		extra, _ := env["extraPaths"].([]any)
		for _, e := range extra {
			s, _ := e.(string)
			if p, ok := valid(s); ok {
				project.Paths = append(project.Paths, p)
			} else {
				notes = append(notes, fmt.Sprintf("the extraPaths entry %v of the %s environment is not a path inside the checkout and was not used", e, clean))
			}
		}
		if whole {
			roots = append(roots, project.Paths...)
			continue
		}
		projects = append(projects, project)
	}
	if len(projects) > maxProjects {
		notes = append(notes, fmt.Sprintf("the %d executionEnvironments past the first %d were not used", len(projects)-maxProjects, maxProjects))
		projects = projects[:maxProjects]
	}
	return projects, roots, notes
}
