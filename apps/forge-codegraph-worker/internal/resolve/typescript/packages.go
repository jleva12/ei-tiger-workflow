package typescript

import (
	"os"
	"path/filepath"
	"strings"
)

// linkPackages makes the packages installed for the checkout's projects
// visible beside their sources under root, the way a package manager lays
// them out: every node_modules of an installation gets its place under the
// project's directory, with an entry per package linking to what was
// installed. A workspace package's entry links to its sources under root,
// not to the installation's copy of its manifest, so the compiler reads the
// workspace package as written. Without installations the compiler sees no
// node_modules, and what packages provide stays named by the syntax tier.
func (s *run) linkPackages(root string) error {
	seen := map[string]bool{}
	for _, settings := range s.settings {
		for _, in := range settings.Installs {
			if seen[in.Dir] {
				continue
			}
			seen[in.Dir] = true
			project := filepath.Join(root, filepath.FromSlash(in.Dir))
			if !within(root, project) {
				continue
			}
			err := filepath.WalkDir(in.Path, func(p string, d os.DirEntry, err error) error {
				if err != nil || !d.IsDir() {
					return nil
				}
				if d.Name() != "node_modules" {
					return nil
				}
				rel, err := filepath.Rel(in.Path, p)
				if err != nil {
					return filepath.SkipDir
				}
				if err := linkModules(p, filepath.Join(project, rel), in.Path, project); err != nil {
					return err
				}
				return filepath.SkipDir // what is inside is linked, not walked
			})
			if err != nil {
				return err
			}
		}
	}
	return nil
}

// linkModules gives the directory at under root an entry for every package
// of the installed node_modules dir; a scope directory is one level deeper.
func linkModules(dir, at, install, project string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	if err := os.MkdirAll(at, 0o700); err != nil {
		return err
	}
	for _, e := range entries {
		name := e.Name()
		if name == ".bin" || name == ".cache" || strings.HasPrefix(name, ".package-lock") {
			continue
		}
		source := filepath.Join(dir, name)
		if strings.HasPrefix(name, "@") && e.IsDir() {
			if err := linkModules(source, filepath.Join(at, name), install, project); err != nil {
				return err
			}
			continue
		}
		target := source
		if e.Type()&os.ModeSymlink != 0 {
			// A workspace package: the package manager linked it to its
			// directory in the project, which the analysis has as sources.
			if resolved, err := filepath.EvalSymlinks(source); err == nil {
				if rel, err := filepath.Rel(install, resolved); err == nil && within(install, resolved) && !strings.Contains(filepath.ToSlash(rel), "node_modules/") {
					target = filepath.Join(project, rel)
				} else {
					target = resolved
				}
			}
		}
		link := filepath.Join(at, name)
		if _, err := os.Lstat(link); err == nil {
			continue // the checkout has this path itself
		}
		if err := os.Symlink(target, link); err != nil {
			return err
		}
	}
	return nil
}
