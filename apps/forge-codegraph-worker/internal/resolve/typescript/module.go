package typescript

import (
	pathpkg "path"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// decl is what binding needs from a declaration, available from parsed IR
// and, for unchanged files, from the previous generation's identity map.
type decl struct {
	ID       ir.DeclarationID
	Kind     ir.DeclarationKind
	Name     string
	Span     ir.Span
	NameSpan *ir.Span
	OwnerID  ir.DeclarationID
	Scope    ir.ScopeID // declaring scope; IR-backed modules only
	Body     ir.ScopeID
	Exported bool
	Default  bool
	Callable *ir.CallableDeclaration
	Variable *ir.VariableDeclaration
	Type     *ir.TypeDeclaration
	Key      *semantic.DeclarationKey // stored key of an identity-backed declaration
}

// importBinding is one local name an import statement introduces.
type importBinding struct {
	module string // written specifier
	name   string // "default", "*" or the exported name
}

// reexport forwards a binding under an exported name: name is the binding
// in the target module ("*" for all or the namespace), exported the name
// this module exposes ("" for export *). An empty module is this module
// itself (export { X as Y } of a local declaration).
type reexport struct {
	module   string
	name     string
	exported string
}

// module is one file of the resolved contexts.
type module struct {
	in        semantic.SourceInput
	set       bc.SourceSetID
	path      string // repository path without its extension
	skipped   bool   // affected, but the parse stage produced no syntax (beyond its limits)
	file      *ir.SourceFile
	decls     []decl
	byID      map[ir.DeclarationID]int
	byScope   map[ir.ScopeID][]int
	byOwner   map[ir.DeclarationID][]int
	top       []int
	parent    map[ir.ScopeID]ir.ScopeID
	owner     map[ir.ScopeID]ir.DeclarationID
	imports   map[string]importBinding
	reexports []reexport
	exprs     map[ir.ExpressionID]ir.Expression
	types     map[ir.TypeRefID]ir.TypeRef
	calls     map[ir.OccurrenceID]ir.Call
	// lambdaParams maps a lambda parameter to its lambda and position;
	// callArgs maps an argument expression to the call passing it.
	lambdaParams map[ir.DeclarationID]lambdaRef
	callArgs     map[ir.ExpressionID]callArg
	// byName indexes declarations by their name's bytes, for the compiler's
	// targets; built on first use.
	byName map[[2]uint64]int
}

type lambdaRef struct {
	expr  ir.ExpressionID
	index int
}

type callArg struct {
	call  ir.Call
	index int
}

func newModuleMaps(m *module) {
	m.byID = map[ir.DeclarationID]int{}
	m.byScope = map[ir.ScopeID][]int{}
	m.byOwner = map[ir.DeclarationID][]int{}
	m.parent = map[ir.ScopeID]ir.ScopeID{}
	m.owner = map[ir.ScopeID]ir.DeclarationID{}
	m.imports = map[string]importBinding{}
	m.exprs = map[ir.ExpressionID]ir.Expression{}
	m.types = map[ir.TypeRefID]ir.TypeRef{}
	m.calls = map[ir.OccurrenceID]ir.Call{}
	m.lambdaParams = map[ir.DeclarationID]lambdaRef{}
	m.callArgs = map[ir.ExpressionID]callArg{}
}

func (m *module) index(i int) {
	d := m.decls[i]
	m.byID[d.ID] = i
	if d.OwnerID == "" {
		m.top = append(m.top, i)
	} else {
		m.byOwner[d.OwnerID] = append(m.byOwner[d.OwnerID], i)
	}
	if d.Scope != "" {
		m.byScope[d.Scope] = append(m.byScope[d.Scope], i)
	}
}

func (m *module) fromIR(f ir.SourceFile) {
	newModuleMaps(m)
	file := f
	m.file = &file
	for _, sc := range f.Scopes {
		m.parent[sc.ID] = sc.ParentID
		m.owner[sc.ID] = sc.OwnerDeclarationID
	}
	for _, x := range f.Expressions {
		m.exprs[x.ID] = x
	}
	for _, t := range f.Types {
		m.types[t.ID] = t
	}
	for _, c := range f.Calls {
		m.calls[c.Occurrence.ID] = c
		for i, a := range c.Arguments {
			m.callArgs[a.ExpressionID] = callArg{call: c, index: i}
		}
	}
	for _, l := range f.Lambdas {
		for i, id := range l.ParameterIDs {
			m.lambdaParams[id] = lambdaRef{expr: l.ExpressionID, index: i}
		}
	}
	m.decls = make([]decl, len(f.Declarations))
	for i, d := range f.Declarations {
		m.decls[i] = decl{ID: d.ID, Kind: d.Kind, Name: d.Name, Span: d.Span, NameSpan: d.NameSpan, OwnerID: d.OwnerID, Scope: d.DeclaringScopeID, Body: d.BodyScopeID, Callable: d.Callable, Variable: d.Variable, Type: d.Type}
		for _, mod := range d.Modifiers {
			switch mod.Keyword {
			case "export":
				m.decls[i].Exported = true
			case "default":
				m.decls[i].Default = true
			}
		}
		m.index(i)
	}
	for _, imp := range f.Imports {
		if len(imp.Name.Segments) == 0 {
			continue
		}
		name := imp.Name.Segments[0].Text
		if imp.Kind == ir.ImportReExport {
			// An empty module re-exports this module's own binding.
			exported := imp.Alias
			if exported == "" && name != "*" {
				exported = name
			}
			m.reexports = append(m.reexports, reexport{module: imp.Module, name: name, exported: exported})
			continue
		}
		if imp.Module == "" {
			continue
		}
		local := imp.Alias
		if local == "" {
			if name == "default" || name == "*" {
				continue
			}
			local = name
		}
		if _, taken := m.imports[local]; !taken {
			m.imports[local] = importBinding{module: imp.Module, name: name}
		}
	}
}

func (m *module) fromIdentities(ids semantic.FileIdentities) {
	newModuleMaps(m)
	m.decls = make([]decl, len(ids.Declarations))
	for i, d := range ids.Declarations {
		m.decls[i] = decl{ID: d.DeclarationID, Kind: d.Kind, Name: d.Name, Span: d.Span, OwnerID: d.OwnerID, Key: d.Key}
		m.index(i)
	}
}

func (m *module) decl(id ir.DeclarationID) (*decl, bool) {
	i, ok := m.byID[id]
	if !ok {
		return nil, false
	}
	return &m.decls[i], true
}

// topLevel finds a module-level declaration by name; an exported one wins.
func (m *module) topLevel(name string, accept func(ir.DeclarationKind) bool) (*decl, bool) {
	var found *decl
	for _, i := range m.top {
		d := &m.decls[i]
		if d.Name != name || !accept(d.Kind) {
			continue
		}
		if d.Exported {
			return d, true
		}
		if found == nil {
			found = d
		}
	}
	return found, found != nil
}

// defaultExport is the declaration written export default, when the module
// is syntax-backed.
func (m *module) defaultExport() (*decl, bool) {
	for _, i := range m.top {
		if m.decls[i].Default {
			return &m.decls[i], true
		}
	}
	return nil, false
}

// member finds a member of a declaration by name.
func (m *module) member(owner ir.DeclarationID, name string, accept func(ir.DeclarationKind) bool) (*decl, bool) {
	for _, i := range m.byOwner[owner] {
		d := &m.decls[i]
		if d.Name == name && accept(d.Kind) {
			return d, true
		}
	}
	return nil, false
}

func (m *module) anchor(span ir.Span) graph.SourceAnchor {
	return graph.SourceAnchor{Lineage: m.in.Lineage, ContentSHA256: m.in.Source.ContentSHA256, Span: span}
}

// enclosingType is the class, interface or namespace whose body contains
// the scope, for `this` and inherited members.
func (m *module) enclosingType(scope ir.ScopeID) (*decl, bool) {
	for depth := 0; scope != "" && depth < 256; depth++ {
		if owner := m.owner[scope]; owner != "" {
			if d, ok := m.decl(owner); ok && d.Body == scope && (d.Kind == ir.DeclarationClass || d.Kind == ir.DeclarationInterface) {
				return d, true
			}
		}
		scope = m.parent[scope]
	}
	return nil, false
}

var extensions = []string{".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".d.ts"}

// extensionSwaps map the compiled extension an ESM specifier writes to the
// source extensions it may stand for.
var extensionSwaps = map[string][]string{".js": {".ts", ".tsx", ".d.ts"}, ".jsx": {".tsx"}, ".mjs": {".mts"}, ".cjs": {".cts"}}

func modulePath(p string) string {
	for _, ext := range []string{".d.ts"} {
		if strings.HasSuffix(p, ext) {
			return strings.TrimSuffix(p, ext)
		}
	}
	return strings.TrimSuffix(p, pathpkg.Ext(p))
}

// moduleStatus classifies a module specifier resolution.
type moduleStatus int

const (
	moduleFound moduleStatus = iota
	moduleExternal
	moduleMissing
)

// find locates a discovered file for a checkout-relative module path: the
// written path, the source extensions a compiled extension stands for,
// every registered extension, then an index file.
func (s *run) find(base string) (*module, bool) {
	candidates := []string{base}
	if ext := pathpkg.Ext(base); ext != "" {
		for _, alt := range extensionSwaps[ext] {
			candidates = append(candidates, strings.TrimSuffix(base, ext)+alt)
		}
	}
	for _, ext := range extensions {
		candidates = append(candidates, base+ext)
	}
	for _, ext := range extensions {
		candidates = append(candidates, pathpkg.Join(base, "index"+ext))
	}
	for _, candidate := range candidates {
		if m, ok := s.byPath[candidate]; ok {
			return m, true
		}
	}
	return nil, false
}

// outputDirs are the directories a package's build writes and its entries
// name; the sources they are built from are under src.
var outputDirs = map[string]bool{"dist": true, "build": true, "lib": true, "out": true, "esm": true, "cjs": true}

// sourceOfOutput is the source an entry in a package's build output is
// built from (packages/common/dist/index.d.ts is packages/common/src/index),
// or "" for an entry that is not build output.
func sourceOfOutput(dir, entry string) string {
	rel := strings.TrimPrefix(entry, dir+"/")
	if dir == "." {
		rel = entry
	}
	first, rest, ok := strings.Cut(rel, "/")
	if !ok || !outputDirs[first] {
		return ""
	}
	return pathpkg.Join(dir, "src", modulePath(strings.TrimSuffix(rest, ".d")))
}

func isRelative(spec string) bool {
	return spec == "." || spec == ".." || strings.HasPrefix(spec, "./") || strings.HasPrefix(spec, "../")
}

// resolveModule follows a specifier from the importing file to a
// discovered file: relative to the importer, through the nearest project's
// path mappings and baseUrl, or by workspace package name and subpath.
// Anything else is a package outside the repository.
func (s *run) resolveModule(from *module, spec string) (*module, moduleStatus) {
	switch {
	case spec == "" || strings.HasPrefix(spec, "/"):
		return nil, moduleExternal
	case isRelative(spec):
		if m, ok := s.find(pathpkg.Join(pathpkg.Dir(from.in.Source.Path), spec)); ok {
			return m, moduleFound
		}
		return nil, moduleMissing
	}
	settings := s.settings[from.set]
	if targets, ok := settings.project(from.in.Source.Path, hasPaths).mapped(spec); ok {
		for _, target := range targets {
			if m, ok := s.find(target); ok {
				return m, moduleFound
			}
		}
		return nil, moduleMissing
	}
	if project := settings.project(from.in.Source.Path, hasBaseURL); project != nil {
		if m, ok := s.find(pathpkg.Join(project.BaseURL, spec)); ok {
			return m, moduleFound
		}
	}
	for _, pkg := range settings.Packages {
		switch {
		case spec == pkg.Name:
			for _, entry := range pkg.Entries {
				for _, candidate := range []string{entry, sourceOfOutput(pkg.Dir, entry)} {
					if m, ok := s.find(candidate); candidate != "" && ok {
						return m, moduleFound
					}
				}
			}
			for _, index := range []string{"src/index", "index"} {
				if m, ok := s.find(pathpkg.Join(pkg.Dir, index)); ok {
					return m, moduleFound
				}
			}
			return nil, moduleMissing
		case strings.HasPrefix(spec, pkg.Name+"/"):
			// A subpath is a directory of the package, or of its sources
			// when it exports built output (./audit is dist/audit, built
			// from src/audit).
			sub := strings.TrimPrefix(spec, pkg.Name+"/")
			for _, base := range []string{pathpkg.Join(pkg.Dir, sub), pathpkg.Join(pkg.Dir, "src", sub)} {
				if m, ok := s.find(base); ok {
					return m, moduleFound
				}
			}
			return nil, moduleMissing
		}
	}
	return nil, moduleExternal
}
