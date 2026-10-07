// Package java is the javac-backed implementation of semantic.Resolver. One
// java process per Resolve call attributes every requested compilation
// context with the service-owned JDK's JavaCompiler API; javac is the only
// binding authority. Events are streamed back one source file at a time and
// turned into symbols and lookups in the run-local semantic workspace.
package java

import (
	"ei-aitiger-codegraph/worker/internal/scratch"

	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/semantic"
)

//go:embed compiler/BindingBridge.java
var bridgeSource []byte

// Version fingerprints the binding implementation; PolicyDigest the rules it
// applies. Both change whenever attribution or mapping semantics change.
const Version = "java-javac-binding-v10"
const Policy = "JavaCompiler analyze per compilation context inside one JVM with exact ordered classpath and pinned reactor bytecode outputs mapped to originating source-set declarations by compiler signature; source visibility limited to the current compilation set, source preferred over same-name binaries independent of timestamps, annotation processing disabled except Lombok when the set's classpath has it (the project's JAR, then the service's), no code generation except class files, in scratch, for a source set the build left without compiled output that attributed sets need, for as much of it as compiles (files with errors, and files that then fail without them, are left out); members and types Lombok generates are derived symbols of their nearest source type, whose declaration is their contributor; every declaration reference, type use, call and callable reference receives a compiler-backed binding or an explicit diagnostic/gap; a target javac attributed stands despite an error at the site unless the error makes the choice unreliable (an ambiguous reference, no applicable overload among several), a call's own errors are those before its arguments, a call javac attributed with an erroneous argument binds when no other method of its name takes that many arguments, and an error in a lambda's body leaves its functional interface bound; method declarations bind to every method javac says they override; lambdas and method references bind to the single abstract method of their attributed functional interface; namespace-only qualifiers, imports and compiler language-expression pseudo-members contribute lexical evidence without declaration lookup rows; missing required inputs and unsupported mappings block publication"

// Config contains service-owned toolchain and scratch locations. Paths are
// never serialized into semantic facts or portable declaration keys.
type Config struct {
	JavaHome string
	WorkDir  string
	CacheDir string
	// MaxHeapMiB bounds the single compiler process of a Resolve call.
	MaxHeapMiB int
	// Parallelism is the number of contexts attributed concurrently inside
	// the JVM, each with its own JavacTask. Default 1.
	Parallelism int
	// LombokJAR is a Lombok JAR that runs on JavaHome, used when a source
	// set's own Lombok cannot (Lombok supports each JDK from some release
	// on). Optional; a set without Lombok on its classpath never runs it.
	LombokJAR string
	// ContextTimeout bounds the attribution of one source set; one that
	// takes longer is retried alone, then analysed without the compiler.
	// Default 30 minutes.
	ContextTimeout time.Duration
}

type Resolver struct {
	config       Config
	javaHome     string
	jdkMajor     int // the JDK's feature release, from its release file
	lombokDigest string
	fingerprints fingerprintCache
	bridgeOnce   sync.Mutex
	bridgeDir    string
}

var _ semantic.Resolver = (*Resolver)(nil)

// New validates the JDK (bin/java and bin/javac must be executable) and the
// scratch directories. The bridge is compiled lazily on first use.
func New(c Config) (*Resolver, error) {
	for name, dir := range map[string]string{"JavaHome": c.JavaHome, "WorkDir": c.WorkDir, "CacheDir": c.CacheDir} {
		if dir == "" || !filepath.IsAbs(dir) {
			return nil, fmt.Errorf("%w: %s must be an absolute service-owned path", semantic.ErrInvalid, name)
		}
	}
	scratch.Sweep(c.WorkDir, "javac-", scratch.Stale) // compilations a killed process left
	if c.MaxHeapMiB == 0 {
		c.MaxHeapMiB = 1024
	}
	if c.Parallelism == 0 {
		c.Parallelism = 1
	}
	if c.ContextTimeout == 0 {
		c.ContextTimeout = 30 * time.Minute
	}
	if c.MaxHeapMiB < 128 || c.MaxHeapMiB > 32768 || c.Parallelism < 1 || c.Parallelism > 64 || c.ContextTimeout < time.Minute {
		return nil, fmt.Errorf("%w: compiler limits", semantic.ErrInvalid)
	}
	for _, name := range []string{"java", "javac"} {
		info, err := os.Stat(filepath.Join(c.JavaHome, "bin", name))
		if err != nil {
			return nil, err
		}
		if !info.Mode().IsRegular() || info.Mode()&0o111 == 0 {
			return nil, fmt.Errorf("%w: executable JDK tool %s required", semantic.ErrInvalid, name)
		}
	}
	home, err := filepath.EvalSymlinks(c.JavaHome)
	if err != nil {
		return nil, err
	}
	for _, dir := range []string{c.WorkDir, c.CacheDir} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, err
		}
	}
	r := &Resolver{config: c, javaHome: home, jdkMajor: jdkMajor(home), fingerprints: fingerprintCache{entries: map[fingerprintKey]string{}}}
	if c.LombokJAR != "" {
		if !filepath.IsAbs(c.LombokJAR) {
			return nil, fmt.Errorf("%w: LombokJAR must be an absolute path", semantic.ErrInvalid)
		}
		body, err := os.ReadFile(c.LombokJAR)
		if err != nil {
			return nil, fmt.Errorf("Lombok JAR: %w", err)
		}
		sum := sha256.Sum256(body)
		r.lombokDigest = hex.EncodeToString(sum[:])
	}
	return r, nil
}

func (*Resolver) Version() string { return Version }

// PolicyDigest covers the service's Lombok JAR: another one can generate
// other members.
func (r *Resolver) PolicyDigest() string {
	if r.lombokDigest == "" {
		return graph.Digest(Policy)
	}
	return graph.Digest([]string{Policy, "lombok:" + r.lombokDigest})
}

// contextJob is one compilation context of the job: a source set, its
// materialized files and the javac inputs derived from the build inventory.
type contextJob struct {
	set        bc.SourceSet
	dir        string // sources/<dir> holds this set's materialized files
	files      []semantic.SourceInput
	listPath   string
	eventsPath string
	classpath  []string
	sourcepath []string
	release    int
	target     int
	mode       string
	processors []string // Lombok JARs to try, in order
	// generate is where the bridge writes this set's class files: the build
	// left it without output and sets attributed in this run need it.
	generate string
	// generateOnly marks a set compiled only for its class files: it is not
	// attributed in this run, so its events are not read.
	generateOnly bool
	generated    bool     // its class files were written
	leftOut      []string // files left out of them, for errors
	done         bool
	processed    bool
	errors       uint64
	millis       int64
	// failure is why the compiler could not analyse the set, after a retry;
	// its files are then processed without compiler events.
	failure string
	// excluded are files javac crashed on, by bridge path, with why.
	excluded map[string]string
	// withheld counts the files not handed to javac at all (module-info).
	withheld int
}

// reset readies a context the compiler could not finish for another attempt.
func (c *contextJob) reset() {
	c.done, c.failure, c.errors, c.millis, c.generated = false, "", 0, 0, false
	for path, reason := range c.excluded {
		// What javac crashed on is found again; what was withheld stays.
		if !strings.HasPrefix(reason, "module declarations") {
			delete(c.excluded, path)
		}
	}
}

// run is the state of one Resolve call.
type run struct {
	r          *Resolver
	ctx        context.Context
	req        semantic.ResolveRequest
	w          semantic.Workspace
	build      bc.BuildContext
	limits     bc.Limits
	dir        string
	inputs     map[bc.InputID]bc.Input
	artifacts  map[bc.ArtifactID]bc.Artifact
	sets       map[bc.SourceSetID]bc.SourceSet
	contexts   []*contextJob
	attributed map[bc.SourceSetID]*contextJob
	setDirs    map[string]bc.SourceSetID // sources/<dir> → source set
	external   map[string]bc.Input       // classpath entry path → JAR/class-directory input
	outputs    map[string]bc.SourceSet   // reactor output directory → producing source set
	inputPaths map[bc.InputID]string
	verified   map[bc.InputID]bool
	origins    map[string]origin // artifact URI → pinned artifact identity
	symbols    *symbolWriter
	views      *viewCache
	keys       *keyIndex
	lombok     map[bc.SourceSetID]string // source set → its Lombok JAR, "" without
	warned     bool                      // Lombok could not run for some set
	// noOutput holds the sets the build declares without compiled output;
	// generating the compiled-only ones this run needs, generated the class
	// directories the bridge wrote for them.
	noOutput   map[bc.SourceSetID]bool
	generating map[bc.SourceSetID]*contextJob
	generated  map[bc.SourceSetID]string
	launches   int // bridge processes started
	result     semantic.ResolutionResult
}

func (r *Resolver) Resolve(ctx context.Context, req semantic.ResolveRequest, w semantic.Workspace) (semantic.ResolutionResult, error) {
	var zero semantic.ResolutionResult
	if w == nil {
		return zero, fmt.Errorf("%w: semantic workspace required", semantic.ErrInvalid)
	}
	build := w.Build()
	if build.RepositoryID != req.Run.RepositoryID || build.SnapshotID != req.CommitSHA {
		return zero, fmt.Errorf("%w: build context does not describe the requested commit", semantic.ErrInvalid)
	}
	if err := os.MkdirAll(r.config.WorkDir, 0o700); err != nil {
		return zero, err
	}
	dir, err := os.MkdirTemp(r.config.WorkDir, "javac-")
	if err != nil {
		return zero, err
	}
	defer os.RemoveAll(dir)
	if dir, err = filepath.EvalSymlinks(dir); err != nil {
		return zero, err
	}
	s, err := r.newRun(ctx, req, w, dir)
	if err != nil {
		return zero, err
	}
	if err := s.materialize(); err != nil {
		return s.result, fmt.Errorf("Java compiler materialize sources: %w", err)
	}
	var producers, live []*contextJob
	attributed := false
	for _, c := range s.contexts {
		if len(c.files) == 0 {
			c.done, c.processed = true, true
			continue
		}
		if c.generate != "" {
			producers = append(producers, c)
		} else {
			live = append(live, c)
		}
		attributed = attributed || !c.generateOnly
	}
	if !attributed {
		return s.result, nil
	}
	// Every source declaration remains represented, even when the compiler
	// reports an error; primitives are language intrinsics shared by all.
	for _, n := range []string{"byte", "short", "int", "long", "float", "double", "boolean", "char", "void"} {
		if _, err := s.symbols.addUnique(intrinsic(n)); err != nil {
			return s.result, err
		}
	}
	bridge, err := r.ensureBridge(ctx)
	if err != nil {
		return s.result, err
	}
	// Sets the build left without output are compiled first, one at a time
	// in dependency order, so each sees the class files of those before it.
	for _, c := range producers {
		if err := s.prepare(c); err != nil {
			return s.result, fmt.Errorf("Java compiler source set %s: %w", c.set.ID, err)
		}
		if err := s.runBridge(bridge, []*contextJob{c}); err != nil {
			return s.result, err
		}
		if c.generated {
			s.generated[c.set.ID] = c.generate
			s.result.CompiledOutputs = append(s.result.CompiledOutputs, string(c.set.ID))
			slog.InfoContext(ctx, "compiled the classes the build could not for the sets that need them", "source_set", c.set.ID, "left_out", len(c.leftOut))
			if len(c.leftOut) > 0 {
				shown := c.leftOut[:min(3, len(c.leftOut))]
				more := ""
				if len(c.leftOut) > len(shown) {
					more = fmt.Sprintf(" and %d more", len(c.leftOut)-len(shown))
				}
				s.warn(fmt.Sprintf("%s was compiled for the sets that need it without the files that do not compile (%s%s), so references into those are unresolved.", s.label(c.set), strings.Join(shown, ", "), more))
			}
		} else {
			slog.WarnContext(ctx, "the build and the compiler both left a source set without classes; references into it stay unresolved", "source_set", c.set.ID, "errors", c.errors)
		}
	}
	for _, c := range live {
		if err := s.prepare(c); err != nil {
			return s.result, fmt.Errorf("Java compiler source set %s: %w", c.set.ID, err)
		}
	}
	if len(live) > 0 {
		if err := s.runBridge(bridge, live); err != nil {
			return s.result, err
		}
	}
	if err := s.symbols.flush(); err != nil {
		return s.result, err
	}
	s.result.Symbols = s.symbols.written
	return s.result, nil
}

// newRun indexes the build inventory and selects the contexts of one call.
func (r *Resolver) newRun(ctx context.Context, req semantic.ResolveRequest, w semantic.Workspace, dir string) (*run, error) {
	build := w.Build()
	limits := req.BuildLimits
	if limits.Validate() != nil {
		limits = bc.DefaultLimits()
	}
	s := &run{r: r, ctx: ctx, req: req, w: w, build: build, limits: limits, dir: dir,
		inputs: map[bc.InputID]bc.Input{}, artifacts: map[bc.ArtifactID]bc.Artifact{}, sets: map[bc.SourceSetID]bc.SourceSet{},
		attributed: map[bc.SourceSetID]*contextJob{}, setDirs: map[string]bc.SourceSetID{}, external: map[string]bc.Input{}, outputs: map[string]bc.SourceSet{},
		inputPaths: map[bc.InputID]string{}, verified: map[bc.InputID]bool{}, origins: map[string]origin{}, lombok: map[bc.SourceSetID]string{},
		noOutput: map[bc.SourceSetID]bool{}, generating: map[bc.SourceSetID]*contextJob{}, generated: map[bc.SourceSetID]string{}}
	s.symbols = newSymbolWriter(ctx, w)
	s.views = newViewCache(s, 8)
	s.keys = newKeyIndex(s)
	for _, in := range build.Inventory.Inputs {
		s.inputs[in.ID] = in
	}
	for _, a := range build.Inventory.Artifacts {
		s.artifacts[a.ID] = a
	}
	for _, set := range build.Inventory.SourceSets {
		s.sets[set.ID] = set
	}
	for _, gap := range build.Inventory.MissingInputs {
		if gap.Requested == bc.GapCompiledOutput && gap.SourceSetID != "" && s.sets[gap.SourceSetID].OutputInputID == "" {
			s.noOutput[gap.SourceSetID] = true
		}
	}
	if err := s.selectContexts(); err != nil {
		return nil, err
	}
	return s, nil
}

// selectContexts picks the requested source sets (or all of them) and orders
// them so a set is attributed before every set that has its compiled output
// on the classpath. Reactor bytecode targets then map back to declarations
// already indexed in this run.
func (s *run) selectContexts() error {
	ids := s.req.Contexts
	if len(ids) == 0 {
		for _, set := range s.build.Inventory.SourceSets {
			ids = append(ids, set.ID)
		}
	}
	seen := map[bc.SourceSetID]bool{}
	var selected []bc.SourceSet
	for _, id := range ids {
		set, ok := s.sets[id]
		if !ok {
			return fmt.Errorf("%w: unknown compilation context %s", semantic.ErrInvalid, id)
		}
		if set.Language != "" && set.Language != "java" {
			return fmt.Errorf("%w: Java compiler received non-Java source set %s", semantic.ErrInvalid, id)
		}
		if !seen[id] {
			seen[id] = true
			selected = append(selected, set)
		}
	}
	// A set the build left without output that a selected set sees, directly
	// or through another such set, is compiled for its class files first;
	// one not selected is compiled only for them.
	needs := map[bc.SourceSetID]bool{}
	var need func(id bc.SourceSetID)
	need = func(id bc.SourceSetID) {
		if needs[id] || !s.noOutput[id] {
			return
		}
		needs[id] = true
		for _, entry := range s.sets[id].Classpath {
			if entry.Kind == bc.EntrySourceSet {
				need(bc.SourceSetID(entry.RefID))
			}
		}
	}
	for _, set := range selected {
		for _, entry := range set.Classpath {
			if entry.Kind == bc.EntrySourceSet {
				need(bc.SourceSetID(entry.RefID))
			}
		}
	}
	generateOnly := map[bc.SourceSetID]bool{}
	for _, set := range s.build.Inventory.SourceSets {
		if needs[set.ID] && !seen[set.ID] {
			if set.Language != "" && set.Language != "java" {
				continue
			}
			generateOnly[set.ID] = true
			seen[set.ID] = true
			selected = append(selected, set)
		}
	}
	// Stable topological order over source-set classpath dependencies; a
	// cycle keeps the requested order for the members involved.
	state := map[bc.SourceSetID]int{}
	var ordered []bc.SourceSet
	var visit func(set bc.SourceSet)
	visit = func(set bc.SourceSet) {
		if state[set.ID] != 0 {
			return
		}
		state[set.ID] = 1
		for _, entry := range append(append([]bc.PathEntry{}, set.Classpath...), set.ModulePath...) {
			if entry.Kind == bc.EntrySourceSet && seen[bc.SourceSetID(entry.RefID)] {
				visit(s.sets[bc.SourceSetID(entry.RefID)])
			}
		}
		state[set.ID] = 2
		ordered = append(ordered, set)
	}
	for _, set := range selected {
		visit(set)
	}
	for _, set := range ordered {
		c := &contextJob{set: set, dir: setDirectory(string(set.ID))}
		s.contexts = append(s.contexts, c)
		s.setDirs[c.dir] = set.ID
		if generateOnly[set.ID] {
			c.generateOnly = true
			s.generating[set.ID] = c
		} else {
			s.attributed[set.ID] = c
		}
		if needs[set.ID] {
			// Registered before any set is attributed, so the producer's
			// declarations are indexed for the bytecode its dependents see.
			c.generate = filepath.Join(s.dir, "generated", c.dir)
			if err := os.MkdirAll(c.generate, 0o700); err != nil {
				return err
			}
			s.outputs[c.generate] = set
		}
	}
	return nil
}

// prepare derives the javac inputs of one context and writes its source list.
func (s *run) prepare(c *contextJob) error {
	sort.Slice(c.files, func(i, j int) bool { return c.files[i].Source.Path < c.files[j].Source.Path })
	c.listPath = filepath.Join(s.dir, c.dir+".sources")
	c.eventsPath = filepath.Join(s.dir, c.dir+".events.jsonl")
	list, err := os.OpenFile(c.listPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	c.withheld = 0
	for _, in := range c.files {
		// A named module cannot read the classpath its dependencies are on:
		// the set is compiled as an unnamed module, without its module
		// declaration.
		if path.Base(in.Source.Path) == "module-info.java" {
			if c.excluded == nil {
				c.excluded = map[string]string{}
			}
			c.excluded[bridgePath(c.dir, in.Source.Path)] = "module declarations are not compiled: the set is analysed as an unnamed module"
			c.withheld++
			continue
		}
		if _, err = fmt.Fprintln(list, filepath.Join(s.dir, filepath.FromSlash(bridgePath(c.dir, in.Source.Path)))); err != nil {
			list.Close()
			return err
		}
	}
	if err = list.Close(); err != nil {
		return err
	}
	if c.classpath, c.sourcepath, err = s.paths(c.set); err != nil {
		return err
	}
	// Lombok adds the members its annotations ask for, which sources call:
	// run the set's own Lombok, then the service's when that one cannot run
	// on this JDK.
	if jar, err := s.lombokJAR(c.set); err != nil {
		return err
	} else if jar != "" {
		c.processors = append(c.processors, jar)
		if s.r.config.LombokJAR != "" && s.r.config.LombokJAR != jar {
			c.processors = append(c.processors, s.r.config.LombokJAR)
		}
	}
	set := c.set
	release := set.TargetRelease
	if release == 0 {
		release, _ = strconv.Atoi(set.LanguageVersion)
	}
	// javac supports releases from 8 up to its own: older code compiles as
	// 8, newer as the worker's JDK.
	if release < 8 {
		release = 8
	}
	if s.r.jdkMajor > 0 && release > s.r.jdkMajor {
		slog.WarnContext(s.ctx, "a source set targets a newer Java than the worker's JDK; compiling it at the JDK's release", "source_set", c.set.ID, "release", release, "jdk", s.r.jdkMajor)
		release = s.r.jdkMajor
	}
	mode := set.LanguageOptions["java.compiler.mode"]
	if mode == "" {
		mode = "release"
	}
	if mode != "release" && mode != "source-target" {
		return fmt.Errorf("%w: unsupported Java compiler mode %q", semantic.ErrInvalid, mode)
	}
	target := release
	if mode == "source-target" && set.LanguageOptions["java.compiler.target"] != "" {
		target, err = strconv.Atoi(set.LanguageOptions["java.compiler.target"])
		if err != nil || target < release {
			target = release
		}
		if s.r.jdkMajor > 0 && target > s.r.jdkMajor {
			target = s.r.jdkMajor
		}
	}
	c.release, c.mode, c.target = release, mode, target
	return nil
}

// processContext consumes one finished context: every file's events are
// resolved as soon as that file's last event has been read, then discarded.
func (s *run) processContext(c *contextJob) error {
	if c.processed || c.generateOnly {
		// A set compiled for its class files only is not affected.
		c.processed = true
		return nil
	}
	started := time.Now()
	remaining := make(map[string]semantic.SourceInput, len(c.files))
	for _, in := range c.files {
		remaining[bridgePath(c.dir, in.Source.Path)] = in
	}
	failure := ""
	if c.failure != "" {
		failure = "analysis_limitation: the compiler could not analyse this source set: " + c.failure
		s.warn(fmt.Sprintf("The compiler could not analyse %s (%s), so its references are unresolved.", s.label(c.set), c.failure))
		s.result.Degraded = append(s.result.Degraded, string(c.set.ID))
	} else {
		var outside []string
		err := streamEvents(s.ctx, c.eventsPath, func(fe *fileEvents) error {
			if fe.path == "" {
				return nil // diagnostics without a source position
			}
			in, ok := remaining[fe.path]
			if !ok {
				// Not a file of this context, or one already resolved: its
				// events cannot be joined to syntax, so they are left out.
				outside = append(outside, fe.path)
				return nil
			}
			delete(remaining, fe.path)
			return s.processFile(c, in, fe)
		})
		if len(outside) > 0 {
			slog.WarnContext(s.ctx, "compiler events outside the context's files were left out", "source_set", c.set.ID, "paths", outside)
		}
		if err != nil {
			if s.ctx.Err() != nil || errors.Is(err, semantic.ErrIntegrity) || !errors.Is(err, errCompilerStream) {
				return fmt.Errorf("Java compiler events %s: %w", c.set.ID, err)
			}
			// The compiler's output ends in the middle: what was read is
			// resolved; the rest goes without compiler events.
			failure = "analysis_limitation: the compiler's output for this source set was unreadable: " + err.Error()
			s.warn(fmt.Sprintf("The compiler's output for %s was unreadable (%v), so some of its references are unresolved.", s.label(c.set), err))
			s.result.Degraded = append(s.result.Degraded, string(c.set.ID))
		}
	}
	// Files javac produced no events for (for example an empty compilation
	// unit, or one it crashed on) still get their source symbols and, when
	// affected, their lookups.
	var rest []string
	for path := range remaining {
		rest = append(rest, path)
	}
	sort.Strings(rest)
	var left []string
	for _, path := range rest {
		fe := newFileEvents(path)
		fe.failure = failure
		if reason, ok := c.excluded[path]; ok && failure == "" {
			fe.failure = "analysis_limitation: " + reason
			if !strings.HasPrefix(reason, "module declarations") {
				left = append(left, remaining[path].Source.Path)
			}
		}
		if err := s.processFile(c, remaining[path], fe); err != nil {
			return err
		}
	}
	if len(left) > 0 {
		shown := left
		if len(shown) > 5 {
			shown = shown[:5]
		}
		more := ""
		if len(left) > len(shown) {
			more = fmt.Sprintf(" and %d more", len(left)-len(shown))
		}
		s.warn(fmt.Sprintf("javac could not compile %s%s in %s, so their references are unresolved.", strings.Join(shown, ", "), more, s.label(c.set)))
	}
	c.processed = true
	slog.InfoContext(s.ctx, "Java resolution completed", "source_set", c.set.ID, "source_files", len(c.files), "resolved", s.result.Resolved, "unresolved", s.result.Unresolved, "ambiguous", s.result.Ambiguous, "unsupported", s.result.Unsupported, "seconds", time.Since(started).Seconds())
	return nil
}

// warn adds a warning to the run, once.
func (s *run) warn(text string) {
	for _, w := range s.result.Warnings {
		if w == text {
			return
		}
	}
	s.result.Warnings = append(s.result.Warnings, text)
}

// label names a source set for people: "<module> <set>".
func (s *run) label(set bc.SourceSet) string {
	for _, m := range s.build.Inventory.Modules {
		if m.ID == set.ModuleID {
			return strings.TrimSpace(m.Name + " " + set.Name)
		}
	}
	return string(set.ID)
}

// processFile writes the source symbols of one file and, for an affected
// file, its lookups.
func (s *run) processFile(c *contextJob, in semantic.SourceInput, fe *fileEvents) error {
	if err := s.ctx.Err(); err != nil {
		return err
	}
	view, err := s.views.pin(c.set.ID, in)
	if err != nil {
		return err
	}
	defer s.views.unpin()
	index := s.keys.indexes(c.set.ID)
	for i := range view.decls {
		d := &view.decls[i]
		sym := view.sourceSymbol(d)
		event, found := fe.declarationFor(d)
		if found && event.SignatureValid == "true" && d.Name != "" {
			sym.Key = compilerKey(d, event)
		}
		if found && event.SignatureValid == "true" && index {
			s.keys.add(c.set.ID, event, view, d)
		}
		if err := s.symbols.add(sym); err != nil {
			return err
		}
	}
	if !in.Affected {
		return nil
	}
	if view.file == nil {
		// Skipped by the parse stage: nothing to bind.
		s.result.Skipped++
		return nil
	}
	lookups, err := s.resolveFile(view, fe)
	if err != nil {
		return fmt.Errorf("Java resolution %s: %w", in.Source.Path, err)
	}
	for _, l := range lookups {
		switch l.Status {
		case semantic.LookupResolved:
			s.result.Resolved++
		case semantic.LookupUnresolved:
			s.result.Unresolved++
		case semantic.LookupAmbiguous:
			s.result.Ambiguous++
		case semantic.LookupUnsupported:
			s.result.Unsupported++
		}
	}
	return s.w.PutLookups(s.ctx, in.Source.FileID, lookups)
}

// jdkMajor reads a JDK's feature release from its release file; 0 when it
// cannot, and then releases are not clamped.
func jdkMajor(home string) int {
	body, err := os.ReadFile(filepath.Join(home, "release"))
	if err != nil {
		return 0
	}
	for _, line := range strings.Split(string(body), "\n") {
		if value, ok := strings.CutPrefix(line, "JAVA_VERSION="); ok {
			version := strings.Trim(strings.TrimSpace(value), "\"")
			major, _, _ := strings.Cut(version, ".")
			if major == "1" {
				_, rest, _ := strings.Cut(version, ".")
				major, _, _ = strings.Cut(rest, ".")
			}
			n, _ := strconv.Atoi(major)
			return n
		}
	}
	return 0
}

// lombokJAR is the verified path of the Lombok JAR on a source set's
// classpath, or "" when it has none.
func (s *run) lombokJAR(set bc.SourceSet) (string, error) {
	if jar, ok := s.lombok[set.ID]; ok {
		return jar, nil
	}
	jar := ""
	for _, entry := range set.Classpath {
		if entry.Kind != bc.EntryArtifact {
			continue
		}
		a, ok := s.artifacts[bc.ArtifactID(entry.RefID)]
		if !ok || a.Coordinates.Group != "org.projectlombok" || a.Coordinates.Name != "lombok" || a.Coordinates.Classifier != "" {
			continue
		}
		path, err := s.inputPath(s.inputs[a.BinaryInputID])
		if err != nil {
			return "", err
		}
		jar = path
		break
	}
	s.lombok[set.ID] = jar
	return jar, nil
}

// lombokFailed records that no Lombok JAR could run for a context: calls to
// the members Lombok generates stay unresolved there.
func (s *run) lombokFailed(c *contextJob, failures []string) {
	slog.WarnContext(s.ctx, "Lombok could not run; calls to the members it generates stay unresolved", "source_set", c.set.ID, "failures", failures)
	if s.warned {
		return
	}
	s.warned = true
	warning := "Lombok could not run on the service JDK, so calls to the members it generates are unresolved"
	if s.r.config.LombokJAR == "" {
		warning += "; configure lombok_jar with a Lombok that supports it"
	}
	if len(failures) > 0 {
		// "<jar>: <reason>"; the JAR is a content-addressed copy.
		_, reason, _ := strings.Cut(failures[0], ": ")
		warning += ": " + reason
	}
	s.result.Warnings = append(s.result.Warnings, warning+".")
}

func setDirectory(id string) string { return graph.Digest(id)[len("sha256:"):] }

// bridgePath is the materialized, root-relative slash path javac reports.
func bridgePath(setDir, path string) string { return "sources/" + setDir + "/" + path }

// errNotFound reports whether a workspace read found nothing.
func errNotFound(err error) bool { return errors.Is(err, semantic.ErrNotFound) }
