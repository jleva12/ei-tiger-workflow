package project

import (
	"context"
	"io"
	"os"
	"path"
	"sort"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	tsresolve "ei-aitiger-codegraph/worker/internal/resolve/typescript"
)

// Configuration files the scan reads.
const (
	tsconfigName   = "tsconfig.json"
	jsconfigName   = "jsconfig.json"
	packageName    = "package.json"
	pnpmWorkspaces = "pnpm-workspace.yaml"
	maxDepth       = 24
)

// skipped directories are never scanned for configuration.
var skipped = map[string]bool{".git": true, "node_modules": true}

type scanner struct {
	ctx      context.Context
	root     *os.Root
	limits   bc.Limits
	entries  uint64
	configs  []string // tsconfig/jsconfig files, checkout-relative
	variants []string // tsconfig.*.json/jsconfig.*.json files, which projects reference or extend
	pkgs     []string // package.json files, checkout-relative
}

// Scan reads the checkout's TypeScript configuration. found reports whether
// any tsconfig/jsconfig or package.json exists outside node_modules.
func Scan(ctx context.Context, root *os.Root, limits bc.Limits) (tsresolve.Settings, bool, error) {
	settings, found, _, err := scan(ctx, root, limits)
	return settings, found, err
}

func scan(ctx context.Context, root *os.Root, limits bc.Limits) (tsresolve.Settings, bool, *scanner, error) {
	s := &scanner{ctx: ctx, root: root, limits: limits}
	if err := s.walk(".", 0); err != nil {
		return tsresolve.Settings{}, false, nil, err
	}
	sort.Strings(s.configs)
	sort.Strings(s.pkgs)
	found := len(s.configs) > 0 || len(s.pkgs) > 0
	var settings tsresolve.Settings
	for _, file := range s.configs {
		if project, ok := s.project(file); ok {
			settings.Projects = append(settings.Projects, project)
		}
	}
	settings.Projects = dedupe(settings.Projects)
	settings.Packages = s.workspaces()
	settings.Normalize()
	return settings, found, s, nil
}

// dedupe drops the mappings a project only inherits from the nearest
// ancestor project (a package tsconfig that extends the root's), keeping the
// project when its output directory is its own; a project left with nothing
// of its own is dropped. Resolution is unchanged because the resolver defers
// to the nearest ancestor with mappings.
func dedupe(projects []tsresolve.Project) []tsresolve.Project {
	sort.Slice(projects, func(i, j int) bool { return projects[i].Dir < projects[j].Dir })
	out := make([]tsresolve.Project, 0, len(projects))
	for _, p := range projects {
		var ancestor *tsresolve.Project
		for i := range out {
			candidate := &out[i]
			if candidate.Dir == p.Dir || (candidate.Dir != "." && !strings.HasPrefix(p.Dir, candidate.Dir+"/")) {
				continue
			}
			if candidate.BaseURL == "" && len(candidate.Paths) == 0 && candidate.OutDir == "" {
				continue
			}
			if ancestor == nil || len(candidate.Dir) > len(ancestor.Dir) {
				ancestor = candidate
			}
		}
		if ancestor != nil {
			if samePaths(p.Paths, ancestor.Paths) {
				p.Paths = nil
			}
			if p.BaseURL == ancestor.BaseURL {
				p.BaseURL = ""
			}
			if p.OutDir == ancestor.OutDir {
				p.OutDir = ""
			}
		}
		if p.BaseURL == "" && len(p.Paths) == 0 && p.OutDir == "" {
			continue
		}
		out = append(out, p)
	}
	return out
}

func samePaths(a, b map[string][]string) bool {
	if len(a) != len(b) {
		return false
	}
	for pattern, targets := range a {
		other, ok := b[pattern]
		if !ok || len(other) != len(targets) {
			return false
		}
		for i := range targets {
			if targets[i] != other[i] {
				return false
			}
		}
	}
	return true
}

func (s *scanner) walk(dir string, depth int) error {
	if err := s.ctx.Err(); err != nil {
		return err
	}
	if depth > maxDepth {
		return nil
	}
	f, err := s.root.Open(dir)
	if err != nil {
		return err
	}
	defer f.Close()
	for {
		entries, readErr := f.ReadDir(256)
		for _, entry := range entries {
			s.entries++
			if s.limits.MaxFiles > 0 && s.entries > s.limits.MaxFiles {
				return bc.ErrLimitExceeded
			}
			name := path.Join(dir, entry.Name())
			info, err := s.root.Lstat(name)
			if err != nil {
				return err
			}
			switch {
			case info.Mode()&os.ModeSymlink != 0:
			case info.IsDir():
				if skipped[entry.Name()] {
					continue
				}
				if err := s.walk(name, depth+1); err != nil {
					return err
				}
			case entry.Name() == tsconfigName || entry.Name() == jsconfigName:
				s.configs = append(s.configs, name)
			case (strings.HasPrefix(entry.Name(), "tsconfig.") || strings.HasPrefix(entry.Name(), "jsconfig.")) && strings.HasSuffix(entry.Name(), ".json"):
				s.variants = append(s.variants, name)
			case entry.Name() == packageName:
				s.pkgs = append(s.pkgs, name)
			}
		}
		if readErr == io.EOF {
			return nil
		}
		if readErr != nil {
			return readErr
		}
	}
}

// read returns a file's bytes when it exists and fits the input budget.
func (s *scanner) read(name string) ([]byte, bool) {
	f, err := s.root.Open(name)
	if err != nil {
		return nil, false
	}
	defer f.Close()
	limit := int64(s.limits.MaxInputBytes)
	if limit <= 0 {
		limit = 4 << 20
	}
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil || int64(len(data)) > limit {
		return nil, false
	}
	return data, true
}

func (s *scanner) isDir(name string) bool {
	info, err := s.root.Stat(name)
	return err == nil && info.IsDir()
}

func (s *scanner) isFile(name string) bool {
	info, err := s.root.Stat(name)
	return err == nil && info.Mode().IsRegular()
}

// cleanJoin joins a checkout-relative directory with a written relative
// path and rejects results that escape the checkout.
func cleanJoin(dir, rel string) (string, bool) {
	rel = strings.TrimPrefix(strings.ReplaceAll(rel, "\\", "/"), "/")
	joined := path.Clean(path.Join(dir, rel))
	if joined == ".." || strings.HasPrefix(joined, "../") {
		return "", false
	}
	return joined, true
}
