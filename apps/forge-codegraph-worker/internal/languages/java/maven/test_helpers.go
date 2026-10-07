package maven

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

// Select only exact reactor GAVs consumed as tests artifacts. Inherited versions
// and active profiles have already been expanded by Maven's effective model.
func (p *Provider) declaredTestProjects(ctx context.Context, r bc.Request, model string) ([]string, error) {
	projects, err := readProjects(ctx, model, r.Limits, p.config.MaxModelBytes)
	if err != nil {
		return nil, err
	}
	type gav struct{ group, name, version string }
	needed := map[gav]bool{}
	for _, project := range projects {
		for _, dep := range project.Dependencies {
			if dep.Classifier == "tests" || (dep.Type == "test-jar" && dep.Classifier == "") {
				needed[gav{dep.Group, dep.Name, dep.Version}] = true
			}
		}
	}
	var out []string
	for _, project := range projects {
		if needed[gav{project.Group, project.Name, project.Version}] {
			out = append(out, project.Group+":"+project.Name)
		}
	}
	return out, nil
}

// Detect consumed reactor tests classifiers from Maven's actual expanded paths,
// including transitive test-helper dependencies. Maven receives only exact
// effective reactor GA selectors; dependency bytes never supply command text.
func (p *Provider) consumedTestProjects(ctx context.Context, r bc.Request, model string) ([]string, error) {
	projects, err := readProjects(ctx, model, r.Limits, p.config.MaxModelBytes)
	if err != nil {
		return nil, err
	}
	observer := observer{ctx: ctx, request: r, config: p.config, projectDirs: map[string]string{}}
	if err = observer.locateProjects(); err != nil {
		return nil, err
	}
	candidates := map[string]string{}
	for _, project := range projects {
		coordinate := project.Group + ":" + project.Name
		path := filepath.Join(p.config.CacheDir, "repository", filepath.FromSlash(strings.ReplaceAll(project.Group, ".", "/")), project.Name, project.Version, project.Name+"-"+project.Version+"-tests.jar")
		candidates[path] = coordinate
	}
	needed := map[string]bool{}
	for _, project := range projects {
		_, dir, err := observer.module(project)
		if err != nil {
			return nil, err
		}
		for _, scope := range []string{"compile", "test"} {
			if err = ctx.Err(); err != nil {
				return nil, err
			}
			name := filepath.Join(r.Checkout.Path, dir, "target", "codegraph-"+scope+"-classpath.txt")
			info, err := os.Stat(name)
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			if err != nil {
				return nil, err
			}
			if !info.Mode().IsRegular() || uint64(info.Size()) > r.Limits.MaxInputBytes {
				return nil, bc.ErrLimitExceeded
			}
			body, err := os.ReadFile(name)
			if err != nil {
				return nil, err
			}
			for _, path := range filepath.SplitList(strings.TrimSpace(string(body))) {
				if ga, ok := candidates[filepath.Clean(path)]; ok {
					needed[ga] = true
				}
			}
		}
	}
	var out []string
	for _, project := range projects {
		ga := project.Group + ":" + project.Name
		if needed[ga] {
			out = append(out, ga)
		}
	}
	return out, nil
}
