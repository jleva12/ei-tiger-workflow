package project

import (
	"encoding/json"
	"os"
	"path"
	"sort"
	"strings"

	tsresolve "ei-aitiger-codegraph/worker/internal/resolve/typescript"
)

type packageFile struct {
	Name       string          `json:"name"`
	Workspaces json.RawMessage `json:"workspaces"`
	Main       string          `json:"main"`
	Module     string          `json:"module"`
	Source     string          `json:"source"`
	Types      string          `json:"types"`
	Typings    string          `json:"typings"`
	Exports    json.RawMessage `json:"exports"`
}

const maxWorkspaceDirs = 2000

// workspaces reads the root package.json workspaces and pnpm-workspace.yaml
// patterns and returns every matching package that declares a name.
func (s *scanner) workspaces() []tsresolve.Package {
	var patterns []string
	if data, ok := s.read(packageName); ok {
		var root packageFile
		if json.Unmarshal(data, &root) == nil {
			patterns = append(patterns, workspacePatterns(root.Workspaces)...)
		}
	}
	if data, ok := s.read(pnpmWorkspaces); ok {
		patterns = append(patterns, pnpmPatterns(string(data))...)
	}
	dirs := map[string]bool{}
	for _, pattern := range patterns {
		pattern = strings.TrimSuffix(strings.TrimPrefix(pattern, "./"), "/")
		if pattern == "" || strings.HasPrefix(pattern, "!") || strings.Contains(pattern, "..") {
			continue
		}
		s.expand(".", strings.Split(pattern, "/"), dirs)
		if len(dirs) > maxWorkspaceDirs {
			break
		}
	}
	sorted := make([]string, 0, len(dirs))
	for dir := range dirs {
		sorted = append(sorted, dir)
	}
	sort.Strings(sorted)
	seen := map[string]bool{}
	var out []tsresolve.Package
	for _, dir := range sorted {
		data, ok := s.read(path.Join(dir, packageName))
		if !ok {
			continue
		}
		var pkg packageFile
		if json.Unmarshal(data, &pkg) != nil || pkg.Name == "" || seen[pkg.Name] {
			continue
		}
		seen[pkg.Name] = true
		out = append(out, tsresolve.Package{Name: pkg.Name, Dir: dir, Entries: entries(dir, pkg)})
	}
	return out
}

func workspacePatterns(raw json.RawMessage) []string {
	if len(raw) == 0 {
		return nil
	}
	var list []string
	if json.Unmarshal(raw, &list) == nil {
		return list
	}
	var object struct {
		Packages []string `json:"packages"`
	}
	if json.Unmarshal(raw, &object) == nil {
		return object.Packages
	}
	return nil
}

// pnpmPatterns reads the `packages:` list of a pnpm-workspace.yaml without a
// YAML parser: the file is a flat list of quoted or bare globs.
func pnpmPatterns(text string) []string {
	var out []string
	inList := false
	for _, line := range strings.Split(text, "\n") {
		trimmed := strings.TrimSpace(line)
		switch {
		case strings.HasPrefix(trimmed, "packages:"):
			inList = true
		case inList && strings.HasPrefix(trimmed, "- "):
			item := strings.TrimSpace(strings.TrimPrefix(trimmed, "- "))
			item = strings.Trim(item, `"'`)
			if item != "" {
				out = append(out, item)
			}
		case inList && trimmed != "" && !strings.HasPrefix(trimmed, "#"):
			inList = false
		}
	}
	return out
}

// expand matches one glob, segment by segment, against the directories of
// the checkout; * matches one directory level, ** any depth.
func (s *scanner) expand(dir string, segments []string, out map[string]bool) {
	if len(out) > maxWorkspaceDirs {
		return
	}
	if len(segments) == 0 {
		if s.isDir(dir) {
			out[dir] = true
		}
		return
	}
	segment, rest := segments[0], segments[1:]
	switch {
	case segment == "**":
		s.expand(dir, rest, out)
		for _, child := range s.subdirs(dir) {
			s.expand(child, segments, out)
		}
	case strings.ContainsAny(segment, "*?["):
		for _, child := range s.subdirs(dir) {
			if ok, _ := path.Match(segment, path.Base(child)); ok {
				s.expand(child, rest, out)
			}
		}
	default:
		if joined, ok := cleanJoin(dir, segment); ok {
			s.expand(joined, rest, out)
		}
	}
}

func (s *scanner) subdirs(dir string) []string {
	f, err := s.root.Open(dir)
	if err != nil {
		return nil
	}
	defer f.Close()
	entries, err := f.ReadDir(-1)
	if err != nil {
		return nil
	}
	var out []string
	for _, entry := range entries {
		name := entry.Name()
		if skipped[name] || strings.HasPrefix(name, ".") {
			continue
		}
		child := path.Join(dir, name)
		if info, err := s.root.Lstat(child); err == nil && info.IsDir() && info.Mode()&os.ModeSymlink == 0 {
			out = append(out, child)
		}
	}
	sort.Strings(out)
	return out
}

// entryConditions are the export conditions read, in preference order.
var entryConditions = []string{"source", "import", "module", "default", "require", "types", "node", "browser"}

// entries lists a package's entry files, checkout-relative and in
// preference order, from its exports map and legacy entry fields.
func entries(dir string, pkg packageFile) []string {
	var written []string
	if len(pkg.Exports) > 0 {
		written = append(written, exportEntries(pkg.Exports, 0)...)
	}
	written = append(written, pkg.Source, pkg.Module, pkg.Main, pkg.Types, pkg.Typings)
	seen := map[string]bool{}
	var out []string
	for _, entry := range written {
		if entry == "" {
			continue
		}
		joined, ok := cleanJoin(dir, entry)
		if !ok || seen[joined] {
			continue
		}
		seen[joined] = true
		out = append(out, joined)
		if len(out) == 8 {
			break
		}
	}
	return out
}

// exportEntries reads the "." entry of an exports map: a string, an array
// of fallbacks, or a conditions object whose values may nest.
func exportEntries(raw json.RawMessage, depth int) []string {
	if depth > 4 {
		return nil
	}
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return []string{text}
	}
	var list []json.RawMessage
	if json.Unmarshal(raw, &list) == nil {
		var out []string
		for _, item := range list {
			out = append(out, exportEntries(item, depth+1)...)
		}
		return out
	}
	var object map[string]json.RawMessage
	if json.Unmarshal(raw, &object) != nil {
		return nil
	}
	if dot, ok := object["."]; ok {
		return exportEntries(dot, depth+1)
	}
	var out []string
	for _, condition := range entryConditions {
		if value, ok := object[condition]; ok {
			out = append(out, exportEntries(value, depth+1)...)
		}
	}
	return out
}
