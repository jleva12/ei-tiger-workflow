// Package typescript is the syntax-tier implementation of semantic.Resolver
// for TypeScript and JavaScript. It binds names from the parsed IR alone:
// declarations, syntactic scopes, imports resolved against the source set's
// files, declared and constructed types followed one member at a time. It
// never fabricates a target: a name it cannot bind is an unresolved lookup
// with an explicit cause, and every binding it proves carries syntax
// provenance so readers can tell it from a compiler-attributed one.
package typescript

import (
	"context"
	"errors"
	"fmt"
	"sort"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	tsparser "ei-aitiger-codegraph/worker/internal/parser/typescript"
)

// Language is the source-set language this resolver binds.
const Language = "typescript"

// Version fingerprints the binding implementation; Policy the rules it
// applies. Both change whenever binding semantics change.
const Version = "typescript-syntax-binding-v4"
const Policy = "Syntax-tier binding over parsed IR without a type checker: a name binds to the innermost enclosing declaration of that name along the syntactic scope chain (members are reachable only through a receiver), then to an import binding whose specifier is resolved against the discovered files relative to the importer, through the nearest tsconfig's path mappings and baseUrl, or by workspace package name and subpath, with extension and index-file completion, and whose export is followed through re-exports, barrel files and forwarded bindings with a cycle guard, then to a known JavaScript, DOM or Node platform name as an intrinsic; test-framework and UMD globals, package imports and members reached through them are values outside the repository and stay so through every member access, and bind to external symbols named by the package's canonical specifier and the path of exports, members and calls taken from it (react#useState, axios#create().get; a namespace or default import is the module value itself), as members of the platform library bind to intrinsics named the same way (console.log, JSON.parse().data), and a member a class does not declare binds to its package base's (react#Component.setState); require() declarators are import bindings; member accesses bind through this, static class references, namespace imports and declared, constructed, awaited or returned types followed one member at a time (a function without a declared return type returns its expression body or the first of its own return values that has a type; literals, array literals, string concatenations, arithmetic, comparisons, fallbacks and conditionals have the types they spell), with the members and every base of classes, interfaces and object-type aliases (an alias over an intersection or union extends its named constituents and takes its object constituents' members), utility types Readonly, Partial, Required, NonNullable, Awaited, Omit and Pick denoting their argument, arrays typing their elements, callbacks of array, set and map iteration methods typing their parameters, and destructuring and for-of bindings typed by the member or element they take; calls bind to functions, methods and constructors found the same way; type references bind to class, interface, enum, alias, namespace and type-parameter declarations or to predefined and global types as intrinsics; heritage clauses bind as inheritance and a method binds as an override of the same-named method of its resolved base class; a package chain the tier cannot follow member by member, dynamic imports and computed callees are outside the tier and yield unresolved lookups with an explicit cause; every proven binding carries syntax provenance"

// Resolver binds with the syntax tier and, when it has a compiler, then
// with the compiler tier over the same sites.
type Resolver struct {
	compiler *Compiler
}

var _ semantic.Resolver = (*Resolver)(nil)

// New is the syntax tier alone.
func New() *Resolver { return &Resolver{} }

// NewWithCompiler adds the compiler tier.
func NewWithCompiler(c Compiler) *Resolver {
	c.AnalyzerPath = AnalyzerPath(c.AnalyzerPath)
	return &Resolver{compiler: &c}
}

func (r *Resolver) Version() string {
	if r.compiler != nil {
		return Version + "+" + CompilerVersion
	}
	return Version
}

func (r *Resolver) PolicyDigest() string {
	if r.compiler != nil {
		return graph.Digest([]string{Policy, CompilerPolicy})
	}
	return graph.Digest(Policy)
}

// run is the state of one Resolve call.
type run struct {
	// nesting counts the binders on the stack across calls that restart
	// their own depth, so cyclic initializers or heritage stop; inferring
	// holds the expressions and declarations whose type is being inferred,
	// so a cycle ends at its first revisit instead of branching.
	nesting   int
	inferring map[inferenceKey]bool
	ctx       context.Context
	req       semantic.ResolveRequest
	w         semantic.Workspace
	build     bc.BuildContext
	sets      map[bc.SourceSetID]bc.SourceSet
	modules   []*module
	byFile    map[ir.FileID]*module
	byPath    map[string]*module // checkout-relative path; TypeScript sets share one namespace
	settings  map[bc.SourceSetID]Settings
	parser    *tsparser.Parser // created on first use for unchanged files whose IR left the cache
	symbols   *symbolWriter
	result    semantic.ResolutionResult
	compiler  *Compiler
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
	s := &run{ctx: ctx, req: req, w: w, build: build, sets: map[bc.SourceSetID]bc.SourceSet{}, byFile: map[ir.FileID]*module{}, byPath: map[string]*module{}, settings: map[bc.SourceSetID]Settings{}, symbols: newSymbolWriter(ctx, w), compiler: r.compiler}
	defer s.close()
	for _, set := range build.Inventory.SourceSets {
		s.sets[set.ID] = set
	}
	selected, err := s.selectContexts()
	if err != nil {
		return zero, err
	}
	for id := range selected {
		settings, err := DecodeSettings(s.sets[id].LanguageOptions)
		if err != nil {
			return zero, fmt.Errorf("%w: source set %s: %v", semantic.ErrInvalid, id, err)
		}
		s.settings[id] = settings
	}
	files, err := w.Files(ctx)
	if err != nil {
		return zero, err
	}
	for _, in := range files {
		if !selected[bc.SourceSetID(in.Source.SourceSetID)] {
			continue
		}
		m := &module{in: in, set: bc.SourceSetID(in.Source.SourceSetID), path: modulePath(in.Source.Path)}
		s.modules = append(s.modules, m)
		s.byFile[in.Source.FileID] = m
		s.byPath[in.Source.Path] = m
	}
	sort.Slice(s.modules, func(i, j int) bool { return s.modules[i].in.Source.Path < s.modules[j].in.Source.Path })
	for _, m := range s.modules {
		if err := s.load(m); err != nil {
			return s.result, fmt.Errorf("TypeScript resolution %s: %w", m.in.Source.Path, err)
		}
	}
	// Every declaration of every file in the contexts is represented, so
	// unchanged files remain valid targets.
	for _, m := range s.modules {
		for i := range m.decls {
			if err := s.symbols.add(s.sourceSymbol(m, &m.decls[i])); err != nil {
				return s.result, err
			}
		}
	}
	pending := map[*module][]semantic.Lookup{}
	sites := map[string]site{}
	for _, m := range s.modules {
		if !m.in.Affected || m.skipped {
			continue
		}
		if m.file == nil {
			return s.result, fmt.Errorf("affected file %s has no syntax", m.in.Source.Path)
		}
		lookups, err := s.bind(m, sites)
		if err != nil {
			return s.result, fmt.Errorf("TypeScript resolution %s: %w", m.in.Source.Path, err)
		}
		pending[m] = lookups
	}
	// The compiler rebinds what it can of the same sites.
	s.compile(pending, sites)
	if err := ctx.Err(); err != nil {
		return s.result, err
	}
	for _, m := range s.modules {
		lookups, ok := pending[m]
		if !ok {
			continue
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
		if err := w.PutLookups(ctx, m.in.Source.FileID, lookups); err != nil {
			return s.result, err
		}
	}
	if err := s.symbols.flush(); err != nil {
		return s.result, err
	}
	s.result.Symbols = s.symbols.written
	return s.result, nil
}

// parse re-extracts an unchanged file from its checkout bytes.
func (s *run) parse(in semantic.SourceInput) (ir.SourceFile, bool) {
	content, err := s.w.SourceBytes(s.ctx, in)
	if err != nil {
		return ir.SourceFile{}, false
	}
	if s.parser == nil {
		p, err := tsparser.New()
		if err != nil {
			return ir.SourceFile{}, false
		}
		s.parser = p
	}
	f, err := s.parser.Parse(s.ctx, parser.Input{Source: in.Source, Content: content, Limits: s.req.SyntaxLimits})
	if err != nil {
		return ir.SourceFile{}, false
	}
	return f, true
}

func (s *run) close() {
	if s.parser != nil {
		_ = s.parser.Close(context.Background())
		s.parser = nil
	}
}

// selectContexts picks the requested source sets, or every TypeScript set,
// and refuses a set of another language.
func (s *run) selectContexts() (map[bc.SourceSetID]bool, error) {
	ids := s.req.Contexts
	if len(ids) == 0 {
		for _, set := range s.build.Inventory.SourceSets {
			if language, _ := set.SyntaxLanguage(); language == Language {
				ids = append(ids, set.ID)
			}
		}
	}
	selected := map[bc.SourceSetID]bool{}
	for _, id := range ids {
		set, ok := s.sets[id]
		if !ok {
			return nil, fmt.Errorf("%w: unknown compilation context %s", semantic.ErrInvalid, id)
		}
		if language, _ := set.SyntaxLanguage(); language != Language {
			return nil, fmt.Errorf("%w: TypeScript resolver received %s source set %s", semantic.ErrInvalid, language, id)
		}
		selected[id] = true
	}
	return selected, nil
}

// load reads a module's syntax from the cache; an unchanged file whose IR
// left the cache is parsed again from the checkout so its exports and
// re-exports stay followable, and only when that is impossible do its
// previous identities stand in.
func (s *run) load(m *module) error {
	f, err := s.w.Syntax(s.ctx, m.in)
	if err == nil {
		m.fromIR(f)
		return nil
	}
	if !errors.Is(err, semantic.ErrNotFound) {
		return err
	}
	if f, ok := s.parse(m.in); ok {
		m.fromIR(f)
		return nil
	}
	if m.in.Affected {
		// The parse stage skipped this file (a bundle beyond the parser's
		// limits, typically): it is discovered but has no declarations to
		// offer and no sites to bind, so it contributes nothing.
		newModuleMaps(m)
		m.skipped = true
		s.result.Skipped++
		return nil
	}
	ids, err := s.w.PreviousIdentities(s.ctx, m.in.Lineage)
	if errors.Is(err, semantic.ErrNotFound) {
		// An unchanged file the parser always declines (generated,
		// minified, beyond its limits) has no syntax and never had
		// identities: it contributes nothing, as when it was affected.
		newModuleMaps(m)
		m.skipped = true
		return nil
	}
	if err != nil {
		return fmt.Errorf("neither syntax nor previous identities are available: %w", err)
	}
	m.fromIdentities(ids)
	return nil
}
