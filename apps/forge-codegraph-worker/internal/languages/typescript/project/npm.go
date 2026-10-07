package project

import (
	"encoding/json"
	"io"
	"path"
	"sort"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/languages/typescript/packages"
)

// npm projects. A checkout holds one or more npm projects: a package.json
// that is not a workspace package of another, with the package.json files
// of its own workspace packages and its lockfile. Each is installed on its
// own for the compiler tier, the way its developers install it.

// maxProjects bounds how many projects of one checkout are installed.
const maxProjects = 16

// maxLockBytes bounds the lockfile read with a project.
const maxLockBytes = 64 << 20

// sampleDirs hold packages that are samples of something rather than parts
// of the checkout: fixtures, mocks, generator templates.
var sampleDirs = map[string]bool{"fixtures": true, "__fixtures__": true, "testdata": true, "__mocks__": true, "template": true, "templates": true}

func sample(dir string) bool {
	for _, part := range strings.Split(dir, "/") {
		if strings.HasPrefix(part, ".") && part != "." || sampleDirs[part] {
			return true
		}
	}
	return false
}

// npmProjects lists the checkout's npm projects that declare dependencies,
// shallowest first, leaving out directories the source excludes cover.
func (s *scanner) npmProjects(excludes []string) []packages.Project {
	pkgs := append([]string(nil), s.pkgs...)
	sort.Slice(pkgs, func(i, j int) bool {
		di, dj := strings.Count(pkgs[i], "/"), strings.Count(pkgs[j], "/")
		return di < dj || di == dj && pkgs[i] < pkgs[j]
	})
	members := map[string]bool{}
	var out []packages.Project
	for _, file := range pkgs {
		dir := path.Dir(file)
		if members[dir] || sample(dir) || excludedDir(dir, excludes) {
			continue
		}
		data, ok := s.read(file)
		if !ok {
			continue
		}
		var manifest packageFile
		if json.Unmarshal(data, &manifest) != nil {
			continue
		}
		project := packages.Project{Dir: dir, Files: map[string][]byte{"package.json": data}}
		patterns := workspacePatterns(manifest.Workspaces)
		if yaml, ok := s.read(path.Join(dir, pnpmWorkspaces)); ok {
			patterns = append(patterns, pnpmPatterns(string(yaml))...)
			// Its catalogs version the workspace's dependencies.
			project.Files[pnpmWorkspaces] = yaml
		}
		found := map[string]bool{}
		for _, pattern := range patterns {
			pattern = strings.TrimSuffix(strings.TrimPrefix(pattern, "./"), "/")
			if pattern == "" || strings.HasPrefix(pattern, "!") || strings.Contains(pattern, "..") {
				continue
			}
			s.expand(dir, strings.Split(pattern, "/"), found)
		}
		for member := range found {
			if member == dir {
				continue
			}
			members[member] = true
			if data, ok := s.read(path.Join(member, packageName)); ok {
				rel := strings.TrimPrefix(member, dir+"/")
				if dir == "." {
					rel = member
				}
				project.Files[rel+"/"+packageName] = data
			}
		}
		for _, lock := range []string{"package-lock.json", "npm-shrinkwrap.json"} {
			if data, ok := s.readLarge(path.Join(dir, lock), maxLockBytes); ok {
				project.Files[lock] = data
				break
			}
		}
		if !declaresDependencies(project.Files) {
			continue
		}
		out = append(out, project)
		if len(out) == maxProjects {
			break
		}
	}
	return out
}

func excludedDir(dir string, excludes []string) bool {
	if dir == "." {
		return false
	}
	for _, pattern := range excludes {
		if bc.MatchSourcePattern(pattern, dir+"/package.json") {
			return true
		}
	}
	return false
}

func declaresDependencies(files map[string][]byte) bool {
	for name, data := range files {
		if path.Base(name) != packageName {
			continue
		}
		var manifest map[string]json.RawMessage
		if json.Unmarshal(data, &manifest) != nil {
			continue
		}
		for _, section := range []string{"dependencies", "devDependencies", "optionalDependencies"} {
			var deps map[string]any
			if json.Unmarshal(manifest[section], &deps) == nil && len(deps) > 0 {
				return true
			}
		}
	}
	return false
}

// readLarge reads a file of at most limit bytes.
func (s *scanner) readLarge(name string, limit int64) ([]byte, bool) {
	f, err := s.root.Open(name)
	if err != nil {
		return nil, false
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil || int64(len(data)) > limit {
		return nil, false
	}
	return data, true
}
