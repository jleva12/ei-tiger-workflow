package project

import (
	"encoding/json"
	"path"
	"strings"

	tsresolve "ei-aitiger-codegraph/worker/internal/resolve/typescript"
)

type tsconfigFile struct {
	Extends         json.RawMessage `json:"extends"`
	CompilerOptions struct {
		BaseURL string              `json:"baseUrl"`
		Paths   map[string][]string `json:"paths"`
		OutDir  string              `json:"outDir"`
	} `json:"compilerOptions"`
}

// effective is a tsconfig's options after its extends chain: baseUrl and
// outDir as checkout-relative directories, paths as written together with
// the directory of the file that declared them.
type effective struct {
	baseURL   string
	paths     map[string][]string
	pathsBase string
	outDir    string
}

func (e effective) over(base effective) effective {
	out := base
	if e.baseURL != "" {
		out.baseURL = e.baseURL
	}
	if e.paths != nil {
		out.paths, out.pathsBase = e.paths, e.pathsBase
	}
	if e.outDir != "" {
		out.outDir = e.outDir
	}
	return out
}

// project reads one tsconfig/jsconfig with its extends chain into a
// resolver project; ok is false for an unreadable or malformed file or one
// that configures nothing the resolver uses.
func (s *scanner) project(file string) (tsresolve.Project, bool) {
	e, ok := s.effective(file, map[string]bool{}, 0)
	if !ok {
		return tsresolve.Project{}, false
	}
	p := tsresolve.Project{Dir: path.Dir(file), BaseURL: e.baseURL, OutDir: e.outDir}
	if len(e.paths) > 0 {
		base := e.baseURL
		if base == "" {
			base = e.pathsBase
		}
		p.Paths = map[string][]string{}
		for pattern, targets := range e.paths {
			if pattern == "" || strings.Count(pattern, "*") > 1 {
				continue
			}
			for _, target := range targets {
				if strings.Count(target, "*") > 1 {
					continue
				}
				if joined, ok := cleanJoin(base, target); ok {
					p.Paths[pattern] = append(p.Paths[pattern], joined)
				}
			}
			if len(p.Paths[pattern]) == 0 {
				delete(p.Paths, pattern)
			}
		}
		if len(p.Paths) == 0 {
			p.Paths = nil
		}
	}
	return p, p.BaseURL != "" || len(p.Paths) > 0 || p.OutDir != ""
}

func (s *scanner) effective(file string, visited map[string]bool, depth int) (effective, bool) {
	if visited[file] || depth > 8 {
		return effective{}, false
	}
	visited[file] = true
	data, ok := s.read(file)
	if !ok {
		return effective{}, false
	}
	var cfg tsconfigFile
	if err := json.Unmarshal(stripJSONC(data), &cfg); err != nil {
		return effective{}, false
	}
	dir := path.Dir(file)
	var out effective
	for _, parent := range extendsList(cfg.Extends) {
		target, ok := s.extendsTarget(dir, parent)
		if !ok {
			continue
		}
		if base, ok := s.effective(target, visited, depth+1); ok {
			out = base.over(out)
		}
	}
	var own effective
	if cfg.CompilerOptions.BaseURL != "" {
		own.baseURL, _ = cleanJoin(dir, cfg.CompilerOptions.BaseURL)
	}
	if cfg.CompilerOptions.Paths != nil {
		own.paths, own.pathsBase = cfg.CompilerOptions.Paths, dir
	}
	if cfg.CompilerOptions.OutDir != "" {
		own.outDir, _ = cleanJoin(dir, cfg.CompilerOptions.OutDir)
	}
	return own.over(out), true
}

func extendsList(raw json.RawMessage) []string {
	if len(raw) == 0 {
		return nil
	}
	var one string
	if json.Unmarshal(raw, &one) == nil {
		return []string{one}
	}
	var many []string
	if json.Unmarshal(raw, &many) == nil {
		return many
	}
	return nil
}

// extendsTarget locates an extended configuration: a relative path (with
// .json or /tsconfig.json completion) or a package under a node_modules
// directory of the file's directory or an ancestor.
func (s *scanner) extendsTarget(dir, spec string) (string, bool) {
	if spec == "" {
		return "", false
	}
	if spec == "." || spec == ".." || strings.HasPrefix(spec, "./") || strings.HasPrefix(spec, "../") || strings.HasPrefix(spec, "/") {
		joined, ok := cleanJoin(dir, spec)
		if !ok {
			return "", false
		}
		return s.completeConfig(joined)
	}
	for current := dir; ; current = path.Dir(current) {
		if joined, ok := cleanJoin(current, "node_modules/"+spec); ok {
			if found, ok := s.completeConfig(joined); ok {
				return found, true
			}
		}
		if current == "." || current == "/" {
			return "", false
		}
	}
}

func (s *scanner) completeConfig(name string) (string, bool) {
	for _, candidate := range []string{name, name + ".json", path.Join(name, tsconfigName)} {
		if s.isFile(candidate) {
			return candidate, true
		}
	}
	return "", false
}
