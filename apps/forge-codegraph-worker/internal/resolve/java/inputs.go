package java

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
)

// materialize copies every file of each attributed context into
// <dir>/sources/<setDigest>/<path>, so javac sees exactly the discovered
// variants of that set and nothing else on its source path.
func (s *run) materialize() error {
	files, err := s.w.Files(s.ctx)
	if err != nil {
		return err
	}
	for _, in := range files {
		c, ok := s.attributed[bc.SourceSetID(in.Source.SourceSetID)]
		if !ok {
			if c, ok = s.generating[bc.SourceSetID(in.Source.SourceSetID)]; !ok {
				continue
			}
		}
		if in.Source.Language != "java" {
			return fmt.Errorf("%w: Java compiler received %s source %s", semantic.ErrInvalid, in.Source.Language, in.Source.Path)
		}
		if err := s.ctx.Err(); err != nil {
			return err
		}
		content, err := s.w.SourceBytes(s.ctx, in)
		if err != nil {
			return fmt.Errorf("%s: %w", in.Source.Path, err)
		}
		path := filepath.Join(s.dir, filepath.FromSlash(bridgePath(c.dir, in.Source.Path)))
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return err
		}
		if err := os.WriteFile(path, content, 0o600); err != nil {
			return err
		}
		c.files = append(c.files, in)
	}
	return nil
}

// root maps a logical input root to its service-owned directory. Only the
// checkout, the configured JDK and the dependency cache are ever mounted.
func (s *run) root(name string) (string, error) {
	switch name {
	case "checkout":
		if s.req.CheckoutPath == "" {
			return "", fmt.Errorf("%w: checkout path required for checkout-rooted build inputs", semantic.ErrInvalid)
		}
		return s.req.CheckoutPath, nil
	case "java-jdk":
		return s.r.config.JavaHome, nil
	case "java-cache":
		return s.r.config.CacheDir, nil
	}
	return "", fmt.Errorf("compiler input root %q has no service-owned mount", name)
}

// paths derives the ordered classpath and the materialized sourcepath of a
// source set, verifying that every binary input exists and matches the
// inventory's recorded fingerprint.
func (s *run) paths(set bc.SourceSet) ([]string, []string, error) {
	var classpath, sourcepath []string
	for _, jdk := range s.build.Inventory.JDKs {
		if jdk.ID != set.JDKID {
			continue
		}
		input := s.inputs[jdk.HomeInputID]
		if input.Location == nil {
			// No build pinned a JDK (the set is analysed without its
			// build): the worker's compiles it.
			break
		}
		path, err := s.inputPath(input)
		if err != nil {
			return nil, nil, err
		}
		if path != s.r.javaHome {
			return nil, nil, fmt.Errorf("compiler JDK does not match source set %s pinned toolchain", set.ID)
		}
	}
	for _, inputID := range append(append([]bc.InputID{}, set.SourceRootIDs...), set.GeneratedRootIDs...) {
		input := s.inputs[inputID]
		if input.Location == nil {
			continue
		}
		if input.Location.Root != "checkout" {
			return nil, nil, fmt.Errorf("%w: source bytes outside checkout inventory", semantic.ErrInvalid)
		}
		sourcepath = append(sourcepath, filepath.Join(s.dir, "sources", setDirectory(string(set.ID)), filepath.FromSlash(input.Location.Path)))
	}
	// The set compiles as an unnamed module (its module-info is withheld),
	// so its module path is part of its classpath.
	var skipped []string
	for _, entry := range append(append([]bc.PathEntry{}, set.Classpath...), set.ModulePath...) {
		switch entry.Kind {
		case bc.EntrySourceSet:
			other, ok := s.sets[bc.SourceSetID(entry.RefID)]
			if !ok {
				return nil, nil, fmt.Errorf("%w: unknown classpath source set %s", semantic.ErrInvalid, entry.RefID)
			}
			if other.OutputInputID == "" {
				if !s.noOutput[other.ID] {
					return nil, nil, fmt.Errorf("%w: visible source set %s has no pinned compiled output", semantic.ErrIntegrity, other.ID)
				}
				// The build could not compile it: its class files from this
				// run, or nothing, and lookups into it stay unresolved.
				if dir, ok := s.generated[other.ID]; ok {
					classpath = append(classpath, dir)
				}
				continue
			}
			output := s.inputs[other.OutputInputID]
			if output.Kind != bc.InputClasses {
				return nil, nil, fmt.Errorf("%w: source-set output must be a class directory", semantic.ErrInvalid)
			}
			path, err := s.inputPath(output)
			if err != nil {
				if s.ctx.Err() != nil {
					return nil, nil, s.ctx.Err()
				}
				skipped = append(skipped, fmt.Sprintf("%s output: %v", other.ID, err))
				continue
			}
			classpath = append(classpath, path)
			if prior, exists := s.outputs[path]; exists && prior.ID != other.ID {
				// Two sets claim one directory: its bytecode cannot be traced
				// to either, so lookups into it stay external or unresolved.
				slog.WarnContext(s.ctx, "compiled output shared by two source sets is not mapped to either", "output", path, "sets", []bc.SourceSetID{prior.ID, other.ID})
				delete(s.outputs, path)
				continue
			}
			s.outputs[path] = other
		case bc.EntryArtifact:
			a, ok := s.artifacts[bc.ArtifactID(entry.RefID)]
			if !ok {
				return nil, nil, fmt.Errorf("%w: unknown classpath artifact %s", semantic.ErrInvalid, entry.RefID)
			}
			input := s.inputs[a.BinaryInputID]
			path, err := s.inputPath(input)
			if err != nil {
				if s.ctx.Err() != nil {
					return nil, nil, s.ctx.Err()
				}
				// Not downloaded, gone, or changed since the build: lookups
				// into it stay unresolved.
				skipped = append(skipped, fmt.Sprintf("%s: %v", a.Coordinates.Name, err))
				continue
			}
			classpath = append(classpath, path)
			s.external[path] = input
		case bc.EntryMissing:
			skipped = append(skipped, "missing: "+entry.RefID)
		default:
			return nil, nil, fmt.Errorf("%w: classpath entry kind %q", semantic.ErrInvalid, entry.Kind)
		}
	}
	if len(skipped) > 0 {
		slog.WarnContext(s.ctx, "classpath entries the build could not provide were left out; lookups into them stay unresolved", "source_set", set.ID, "entries", skipped)
		s.warn(fmt.Sprintf("%d classpath entries of %s were unavailable, so references into them are unresolved.", len(skipped), s.label(set)))
	}
	return classpath, sourcepath, nil
}

// inputPath resolves a binary input to its real path, verifying its
// fingerprint at most once per Resolve.
func (s *run) inputPath(input bc.Input) (string, error) {
	if path, ok := s.inputPaths[input.ID]; ok {
		return path, nil
	}
	if input.Location == nil {
		return "", fmt.Errorf("required compiler input %s is unavailable", input.ID)
	}
	root, err := s.root(input.Location.Root)
	if err != nil {
		return "", err
	}
	relative := input.Location.Path
	if !s.verified[input.ID] {
		actual, err := s.r.fingerprints.fingerprint(s.ctx, root, relative, input.Kind, s.limits)
		if err != nil {
			return "", err
		}
		if input.SHA256 == "" || actual != input.SHA256 {
			return "", fmt.Errorf("%w: compiler input %s fingerprint mismatch", semantic.ErrIntegrity, input.ID)
		}
		s.verified[input.ID] = true
	}
	path := filepath.Join(root, filepath.FromSlash(relative))
	rel, err := filepath.Rel(root, path)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return "", fmt.Errorf("%w: compiler input %s escapes its root", semantic.ErrInvalid, input.ID)
	}
	if path, err = filepath.EvalSymlinks(path); err != nil {
		return "", err
	}
	s.inputPaths[input.ID] = path
	return path, nil
}

// fingerprintCache memoizes input digests by (path, size, mtime) across
// Resolve calls, so an unchanged JDK or JAR is hashed once per process.
type fingerprintCache struct {
	mu      sync.Mutex
	entries map[fingerprintKey]string
}

type fingerprintKey struct {
	path  string
	size  int64
	mtime int64
}

func (c *fingerprintCache) fingerprint(ctx context.Context, root, relative string, kind bc.InputKind, limits bc.Limits) (string, error) {
	path := filepath.Join(root, filepath.FromSlash(relative))
	info, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	key := fingerprintKey{path: path, size: info.Size(), mtime: info.ModTime().UnixNano()}
	c.mu.Lock()
	sum, ok := c.entries[key]
	c.mu.Unlock()
	if ok {
		return sum, nil
	}
	if sum, err = manifest.Fingerprint(ctx, root, relative, kind, limits); err != nil {
		return "", err
	}
	c.mu.Lock()
	c.entries[key] = sum
	c.mu.Unlock()
	return sum, nil
}
