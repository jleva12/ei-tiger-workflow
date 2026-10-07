// Package discover inventories Java build declarations without executing builds.
// It is intentionally conservative: declarations are not an effective compiler
// classpath, and unsupported/dynamic settings remain explicit inventory gaps.
package discover

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
)

const Version = "1.0.0"

type Config struct {
	// Mode is auto (default), maven, or gradle. Auto prefers an existing manifest.
	Mode     string
	Manifest manifest.Config
	// FallbackRelease is an extraction profile, never a discovered build fact.
	// Zero defaults to 21. Every use produces a source-set-specific diagnostic.
	FallbackRelease int
}

type Provider struct {
	mode            string
	manifest        *manifest.Provider
	manifestPath    string
	requireManifest bool
	fallback        int
}

var _ bc.Provider = (*Provider)(nil)

func New(c Config) (*Provider, error) {
	if c.Mode == "" {
		c.Mode = "auto"
	}
	if c.Mode != "auto" && c.Mode != "maven" && c.Mode != "gradle" {
		return nil, fmt.Errorf("%w: discovery mode", bc.ErrInvalidInput)
	}
	if c.FallbackRelease == 0 {
		c.FallbackRelease = 21
	}
	switch c.FallbackRelease {
	case 8, 11, 17, 21:
	default:
		return nil, fmt.Errorf("%w: fallback release must be 8, 11, 17, or 21", bc.ErrInvalidInput)
	}
	m, err := manifest.New(c.Manifest)
	if err != nil {
		return nil, err
	}
	p := &Provider{mode: c.Mode, manifest: m, manifestPath: c.Manifest.ManifestPath, fallback: c.FallbackRelease}
	if p.manifestPath == "" {
		p.manifestPath = manifest.DefaultPath
	}
	p.requireManifest = p.manifestPath != manifest.DefaultPath || len(c.Manifest.Roots) > 0
	if c.Mode != "auto" && p.requireManifest {
		return nil, fmt.Errorf("%w: manifest settings require auto or manifest mode", bc.ErrInvalidInput)
	}
	return p, nil
}

type scan struct {
	ctx          context.Context
	r            bc.Request
	root         *os.Root
	files, bytes uint64
	dirs         map[string]bool
	javaDirs     map[string]bool
	markers      map[string]bool
	reads        map[string][]byte
	in           bc.Inventory
	checks       []bc.InputCheck
	seenInputs   map[bc.InputID]bool
	seenJDKs     map[bc.JDKID]bool
	seenGaps     map[bc.GapID]bool
	fallback     int
}

func (p *Provider) Build(ctx context.Context, r bc.Request) (bc.BuildContext, error) {
	var zero bc.BuildContext
	if err := ctx.Err(); err != nil {
		return zero, err
	}
	if err := r.Validate(); err != nil {
		return zero, err
	}
	if p == nil || p.manifest == nil {
		return zero, bc.ErrInvalidInput
	}
	root, err := os.OpenRoot(r.Checkout.Path)
	if err != nil {
		return zero, err
	}
	defer root.Close()
	s := &scan{ctx: ctx, r: r, root: root, dirs: map[string]bool{}, javaDirs: map[string]bool{}, markers: map[string]bool{}, reads: map[string][]byte{}, seenInputs: map[bc.InputID]bool{}, seenJDKs: map[bc.JDKID]bool{}, seenGaps: map[bc.GapID]bool{}, fallback: p.fallback}
	if p.mode == "auto" {
		_, err := s.inspect(p.manifestPath)
		if err == nil || p.requireManifest {
			return p.manifest.Build(ctx, r)
		}
		if !errors.Is(err, os.ErrNotExist) {
			return zero, err
		}
	}
	if err := s.walk(".", 0); err != nil {
		return zero, err
	}
	hasMaven, hasGradle := s.markers["pom.xml"], s.hasGradle(".")
	if p.mode == "auto" && hasMaven && hasGradle {
		return zero, fmt.Errorf("%w: both Maven and Gradle root builds exist; choose CODEGRAPH_BUILD_MODE=maven or gradle", bc.ErrInvalidInput)
	}
	mode := p.mode
	if mode == "auto" {
		if hasMaven {
			mode = "maven"
		} else if hasGradle {
			mode = "gradle"
		}
	}
	switch mode {
	case "maven":
		err = s.maven()
	case "gradle":
		err = s.gradle()
	default:
		// A monorepo may keep independent builds below the repository root.
		maven, gradle := false, false
		byDirectory := map[string]string{}
		for _, name := range sortedKeys(s.markers) {
			if buildFixturePath(name) {
				continue
			}
			kind := ""
			if path.Base(name) == "pom.xml" {
				kind = "maven"
				maven = true
			}
			if isGradleMarker(path.Base(name)) {
				kind = "gradle"
				gradle = true
			}
			if kind == "" {
				continue
			}
			dir := path.Dir(name)
			if previous := byDirectory[dir]; previous != "" && previous != kind {
				return zero, fmt.Errorf("%w: ambiguous Maven/Gradle build in %s; choose CODEGRAPH_BUILD_MODE", bc.ErrInvalidInput, dir)
			}
			byDirectory[dir] = kind
		}
		if maven {
			mode = "maven"
			err = s.maven()
		}
		if err == nil && gradle {
			mode = "gradle"
			err = s.gradle()
		}
		if maven && gradle {
			mode = "mixed"
		}
	}
	if err != nil {
		return zero, err
	}
	if len(s.in.SourceSets) == 0 {
		id := bc.ModuleID("unconfigured")
		s.in.Modules = append(s.in.Modules, bc.Module{ID: id, Name: "Unconfigured Java sources", Directory: "."})
		s.addSet(id, "unconfigured", bc.SourceSetCustom, "", []string{"."})
		s.gap("no-java-build", "Java build configuration", "No Java compilation source sets could be discovered; repository-wide extraction has unknown build visibility", "", "")
	}
	s.unmappedJava()
	// Static discovery cannot establish resolved dependency order, transitive
	// mediation, module-path partitioning or generated inputs from plugin execution.
	s.gap("unevaluated-build", "effective compiler environment", "Build files were inspected without executing Maven/Gradle; active profiles, plugins, generated sources, resolved transitive dependencies and compiler path order are not verified", "", "")
	if err := s.budget(); err != nil {
		return zero, err
	}
	digest, err := s.in.Digest()
	if err != nil {
		return zero, err
	}
	c, err := bc.Seal(bc.BuildContext{RepositoryID: r.Checkout.RepositoryID, SnapshotID: r.Checkout.SnapshotID, Producer: bc.Producer{Name: "automatic-" + mode + "-declarations", Version: Version, InputSHA256: digest}, Inventory: s.in, Checks: s.checks})
	if err != nil {
		return zero, err
	}
	data, err := json.Marshal(c)
	if err != nil {
		return zero, err
	}
	if c.RecordCount() > r.Limits.MaxRecords || uint64(len(c.Diagnostics)) > r.Limits.MaxDiagnostics || uint64(len(data)) > r.Limits.MaxOutputBytes {
		return zero, bc.ErrLimitExceeded
	}
	if err := ctx.Err(); err != nil {
		return zero, err
	}
	return c, nil
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
func stable(prefix, value string) string {
	sum := sha256.Sum256([]byte(value))
	return prefix + ":" + hex.EncodeToString(sum[:16])
}
func (s *scan) budget() error {
	if err := s.ctx.Err(); err != nil {
		return err
	}
	if s.in.RecordCount()+uint64(len(s.checks)+len(s.in.MissingInputs)) > s.r.Limits.MaxRecords || uint64(len(s.in.MissingInputs)) > s.r.Limits.MaxDiagnostics {
		return bc.ErrLimitExceeded
	}
	return nil
}
func (s *scan) gap(key, requested, reason string, module bc.ModuleID, set bc.SourceSetID) bc.PathEntry {
	id := bc.GapID(stable("gap", key))
	if !s.seenGaps[id] {
		s.seenGaps[id] = true
		s.in.MissingInputs = append(s.in.MissingInputs, bc.MissingInput{ID: id, Requested: requested, Reason: reason, ModuleID: module, SourceSetID: set})
	}
	return bc.PathEntry{Kind: bc.EntryMissing, RefID: string(id)}
}

// inspect rejects symlinks in every component, including missing-leaf paths.
func (s *scan) inspect(name string) (os.FileInfo, error) {
	if !bc.ValidPath(name) {
		return nil, fmt.Errorf("%w: checkout-relative path required", bc.ErrInvalidInput)
	}
	current := "."
	var info os.FileInfo
	for _, part := range strings.Split(name, "/") {
		if err := s.ctx.Err(); err != nil {
			return nil, err
		}
		current = path.Join(current, part)
		var err error
		info, err = s.root.Lstat(current)
		if err != nil {
			return nil, err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return nil, fmt.Errorf("%w: symbolic link in build input %s", bc.ErrInvalidInput, current)
		}
	}
	return info, nil
}

func (s *scan) read(name string) ([]byte, error) {
	if data, ok := s.reads[name]; ok {
		return data, nil
	}
	info, err := s.inspect(name)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("%w: build file is not regular: %s", bc.ErrInvalidInput, name)
	}
	remaining := s.r.Limits.MaxInputBytes - s.bytes
	if uint64(info.Size()) > remaining {
		return nil, fmt.Errorf("%w: aggregate build configuration bytes", bc.ErrLimitExceeded)
	}
	f, err := s.root.Open(name)
	if err != nil {
		return nil, err
	}
	data, readErr := io.ReadAll(io.LimitReader(f, int64(remaining)+1))
	closeErr := f.Close()
	if err := errors.Join(readErr, closeErr); err != nil {
		return nil, err
	}
	s.bytes += uint64(len(data))
	if s.bytes > s.r.Limits.MaxInputBytes {
		return nil, bc.ErrLimitExceeded
	}
	if !utf8.Valid(data) {
		return nil, fmt.Errorf("%w: build file must be UTF-8: %s", bc.ErrInvalidInput, name)
	}
	if err := s.ctx.Err(); err != nil {
		return nil, err
	}
	s.reads[name] = data
	return data, nil
}

func (s *scan) walk(dir string, depth uint32) error {
	if depth > s.r.Limits.MaxDepth {
		return fmt.Errorf("%w: build discovery depth", bc.ErrLimitExceeded)
	}
	if err := s.budget(); err != nil {
		return err
	}
	s.dirs[dir] = true
	f, err := s.root.Open(dir)
	if err != nil {
		return err
	}
	defer f.Close()
	for {
		entries, err := f.ReadDir(256)
		if err != nil && err != io.EOF {
			return err
		}
		for _, e := range entries {
			s.files++
			if s.files > s.r.Limits.MaxFiles {
				return fmt.Errorf("%w: build discovery entries", bc.ErrLimitExceeded)
			}
			if err := s.ctx.Err(); err != nil {
				return err
			}
			name := path.Join(dir, e.Name())
			if !bc.ValidPath(name) {
				return fmt.Errorf("%w: unrepresentable checkout path", bc.ErrInvalidInput)
			}
			if e.Type()&os.ModeSymlink != 0 {
				if e.Name() == "pom.xml" || isGradleMarker(e.Name()) || e.Name() == "gradle.properties" {
					return fmt.Errorf("%w: symbolic link build file %s", bc.ErrInvalidInput, name)
				}
				continue
			}
			if e.IsDir() {
				switch e.Name() {
				case ".git", ".gradle", ".idea", "node_modules", ".codegraph-work":
					continue
				}
				if err := s.walk(name, depth+1); err != nil {
					return err
				}
			} else if e.Type().IsRegular() {
				if strings.HasSuffix(e.Name(), ".java") {
					s.javaDirs[dir] = true
				}
				switch e.Name() {
				case "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradle.properties":
					s.markers[name] = true
				}
			}
		}
		if err == io.EOF {
			return nil
		}
	}
}

// Make files outside selected roots visible without claiming those files have
// a known compiler environment or silently assigning them to another module.
func (s *scan) unmappedJava() {
	roots := map[string]bool{}
	for _, input := range s.in.Inputs {
		if input.Kind == bc.InputSourceRoot && input.Location != nil {
			roots[input.Location.Path] = true
		}
	}
	count := 0
	var examples []string
	for _, dir := range sortedKeys(s.javaDirs) {
		covered := false
		for p := dir; ; p = path.Dir(p) {
			if roots[p] {
				covered = true
				break
			}
			if p == "." {
				break
			}
		}
		if !covered {
			count++
			if len(examples) < 3 && len(dir) < 512 {
				examples = append(examples, dir)
			}
		}
	}
	if count > 0 {
		s.gap("unmapped-java", "Java files outside discovered source roots", fmt.Sprintf("%d directories contain Java files outside discovered source roots; those files are not scheduled for parsing. Examples: %s", count, strings.Join(examples, ", ")), "", "")
	}
}

func releaseNumber(raw string) (int, bool) {
	raw = strings.TrimSpace(raw)
	raw = strings.TrimPrefix(raw, "1.")
	n, err := strconv.Atoi(raw)
	return n, err == nil && n >= 8 && n <= 999
}

func (s *scan) addSet(module bc.ModuleID, name string, kind bc.SourceSetKind, release string, roots []string) int {
	id := bc.SourceSetID(stable("set", string(module)+"/"+name))
	n, known := releaseNumber(release)
	if !known {
		n = s.fallback
		s.gap(string(id)+"/release", "Java release", fmt.Sprintf("Java release was not established; Java %d is used only as the extraction fallback profile", n), module, id)
	}
	jdk := bc.JDKID(fmt.Sprintf("unknown-jdk-%d", n))
	input := bc.InputID(jdk)
	if !s.seenJDKs[jdk] {
		s.seenJDKs[jdk] = true
		s.in.JDKs = append(s.in.JDKs, bc.JDK{ID: jdk, HomeInputID: input, Vendor: "unknown", Version: "unknown", Major: n})
		s.in.Inputs = append(s.in.Inputs, bc.Input{ID: input, Kind: bc.InputJDK, UnavailableReason: "JDK installation, version and bytes were not inventoried during read-only build discovery"})
		s.checks = append(s.checks, bc.InputCheck{InputID: input, Status: bc.Missing})
	}
	set := bc.SourceSet{ID: id, ModuleID: module, Name: name, Kind: kind, JDKID: jdk, TargetRelease: n}
	for _, dir := range roots {
		input := bc.InputID(stable("source", dir))
		if !s.seenInputs[input] {
			s.seenInputs[input] = true
			s.in.Inputs = append(s.in.Inputs, bc.Input{ID: input, Kind: bc.InputSourceRoot, Location: &bc.Location{Root: "checkout", Path: dir}})
			status := bc.Missing
			if s.dirs[dir] {
				status = bc.Available
			}
			s.checks = append(s.checks, bc.InputCheck{InputID: input, Status: status})
		}
		if !contains(set.SourceRootIDs, input) {
			set.SourceRootIDs = append(set.SourceRootIDs, input)
		}
	}
	set.Classpath = append(set.Classpath, s.gap(string(id)+"/classpath", "effective compile classpath", "Compiler classpath and module path have not been resolved; declared dependencies are inventory evidence, not an ordered effective path", module, id))
	s.in.SourceSets = append(s.in.SourceSets, set)
	return len(s.in.SourceSets) - 1
}

func contains[T comparable](xs []T, x T) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

// sourcePath resolves declarative relative roots and rejects paths outside the
// checkout, expressions and machine-specific absolute paths.
func sourcePath(dir, raw string) (string, bool) {
	if strings.ContainsAny(raw, "$\\\x00") || raw == "" || path.IsAbs(raw) {
		return "", false
	}
	p := path.Join(dir, raw)
	return p, bc.ValidPath(p)
}
