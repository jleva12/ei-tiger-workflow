package maven

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

// Maven permits an added source root to contain no generated Java files and
// leave the directory absent. After the complete lifecycle succeeds, retain an
// empty directory for that declared compiler input so its empty content has a
// verifiable fingerprint. This never fabricates generated files or repairs a
// failed build, and read-only BuildObserved still reports absent inputs.
func (p *Provider) materializeDeclaredRoots(ctx context.Context, request bc.Request, model string) error {
	projects, err := readProjects(ctx, model, request.Limits, p.config.MaxModelBytes)
	if err != nil {
		return err
	}
	checkout, err := filepath.EvalSymlinks(request.Checkout.Path)
	if err != nil {
		return err
	}
	root, err := os.OpenRoot(checkout)
	if err != nil {
		return err
	}
	defer root.Close()
	request.Checkout.Path = checkout
	observer := observer{ctx: ctx, request: request, config: p.config, projectDirs: map[string]string{}}
	if err = observer.locateProjects(); err != nil {
		return err
	}
	for _, project := range projects {
		_, dir, err := observer.module(project)
		if err != nil {
			return err
		}
		for _, plugin := range project.Build.Plugins {
			if plugin.Name != "build-helper-maven-plugin" {
				continue
			}
			for _, execution := range plugin.Executions {
				for _, goal := range execution.Goals {
					if goal != "add-source" && goal != "add-test-source" {
						continue
					}
					for _, name := range execution.Configuration.Sources {
						if err = ctx.Err(); err != nil {
							return err
						}
						relative, err := observer.projectRelative(dir, name)
						if err != nil {
							return err
						}
						if !bc.ValidPath(filepath.ToSlash(relative)) {
							return fmt.Errorf("%w: generated root escapes owned checkout", bc.ErrInvalidInput)
						}
						relative = filepath.FromSlash(relative)
						part := ""
						for _, segment := range strings.Split(relative, string(filepath.Separator)) {
							part = filepath.Join(part, segment)
							info, err := root.Lstat(part)
							if err == nil && (!info.IsDir() || info.Mode()&os.ModeSymlink != 0) {
								return fmt.Errorf("%w: generated root contains a symlink or non-directory", bc.ErrInvalidInput)
							}
							if err != nil && !errors.Is(err, os.ErrNotExist) {
								return err
							}
							if errors.Is(err, os.ErrNotExist) {
								if err = root.Mkdir(part, 0700); err != nil {
									return err
								}
							}
						}
					}
				}
			}
		}
	}
	return nil
}
