package typescript

import (
	"fmt"
	pathpkg "path"
	"strings"

	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// target is what a name binds to: a declaration, a namespace-imported
// module, or an intrinsic.
type target struct {
	m         *module
	d         *decl
	namespace bool
	intrinsic string
	// external names the package, framework or platform a value comes from
	// when it is outside the repository: its members are attributed to that
	// dependency rather than unknown.
	external string
	// pkg is the canonical specifier of the package, Node built-in or
	// framework such a value comes from, when it is known, and path the
	// members and calls taken from what the package exports ("create().get"
	// for axios.create().get). A value with a pkg binds to an external symbol
	// named by both. An intrinsic keeps a path the same way ("console" and
	// "log").
	pkg  string
	path string
	// tm and args are the written type arguments of an intrinsic type
	// (Array<T>, T[], Promise<T>, Map<K, V>) for element and result typing.
	tm   *module
	args []ir.TypeRefID
}

func (t target) ok() bool {
	return t.d != nil || t.namespace || t.intrinsic != "" || t.external != ""
}

// child is the value a member access or a call takes from an external or
// platform value: segment is a member name, "()" for a call's result, or
// "[]" for an element. Element and argument typing does not carry over.
func (t target) child(segment string) target {
	next := target{external: t.external, pkg: t.pkg, intrinsic: t.intrinsic}
	switch {
	case segment == "()" || segment == "[]" || t.path == "":
		next.path = t.path + segment
	default:
		next.path = t.path + "." + segment
	}
	if strings.HasPrefix(next.path, ".") {
		next.path = next.path[1:]
	}
	return next
}

func startsUpper(name string) bool { return name != "" && name[0] >= 'A' && name[0] <= 'Z' }

// joinPath names a path below a root: "console.log", "fetch().then".
func joinPath(root, path string) string {
	switch {
	case path == "":
		return root
	case strings.HasPrefix(path, "(") || strings.HasPrefix(path, "["):
		return root + path
	}
	return root + "." + path
}

// packageTarget is what an import from a package binds: the export by
// name; a namespace or default import is the module value itself, so
// React.useState and a named useState are one symbol.
func packageTarget(spec, name string) target {
	path := name
	if name == "*" || name == "default" {
		path = ""
	}
	if !packageSpecifier(spec) {
		// Not a package name (an alias such as @/components or ~/lib that
		// no tsconfig maps): outside the repository's discovered files, but
		// nothing to name a symbol after.
		return target{external: spec, path: path}
	}
	return target{external: spec, pkg: canonicalSpecifier(spec), path: path}
}

// element is the element type of an intrinsic collection target.
func (s *run) element(t target, index int) (target, bool) {
	if t.external != "" {
		return t, true
	}
	if t.tm == nil || index >= len(t.args) {
		return target{}, false
	}
	r, b := s.resolveTypeRef(t.tm, t.args[index], 0)
	if b.status == semantic.LookupResolved && r.ok() || r.external != "" {
		return r, true
	}
	return target{}, false
}

// binding is the outcome for one site.
type binding struct {
	status semantic.LookupStatus
	target target
	reason string
	cause  semantic.LookupCause
	skip   bool
}

func resolved(t target, reason string) binding {
	return binding{status: semantic.LookupResolved, target: t, reason: "syntax_derived: " + reason}
}
func unresolved(cause semantic.LookupCause, reason string) binding {
	return binding{status: semantic.LookupUnresolved, cause: cause, reason: reason}
}
func unsupported(reason string) binding {
	return binding{status: semantic.LookupUnsupported, cause: semantic.CauseAnalysisLimitation, reason: "analysis_limitation: " + reason}
}

var skip = binding{skip: true}

func valueKind(kind ir.DeclarationKind) bool {
	switch kind {
	case ir.DeclarationVariable, ir.DeclarationLocal, ir.DeclarationParameter, ir.DeclarationFunction, ir.DeclarationClass, ir.DeclarationEnum, ir.DeclarationNamespace:
		return true
	}
	return false
}
func typeKind(kind ir.DeclarationKind) bool {
	switch kind {
	case ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationTypeAlias, ir.DeclarationNamespace, ir.DeclarationTypeParameter:
		return true
	}
	return false
}
func anyKind(ir.DeclarationKind) bool { return true }
func memberKind(kind ir.DeclarationKind) bool {
	switch kind {
	case ir.DeclarationMethod, ir.DeclarationConstructor, ir.DeclarationField, ir.DeclarationEnumConstant, ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationNamespace, ir.DeclarationTypeAlias, ir.DeclarationFunction, ir.DeclarationVariable:
		return true
	}
	return false
}
func callableKind(kind ir.DeclarationKind) bool {
	switch kind {
	case ir.DeclarationMethod, ir.DeclarationConstructor, ir.DeclarationFunction, ir.DeclarationClass, ir.DeclarationVariable, ir.DeclarationField, ir.DeclarationLocal, ir.DeclarationParameter:
		return true
	}
	return false
}

// lookupName walks the syntactic scope chain for a declaration of the name,
// then the module's import bindings, then the known globals.
func (s *run) lookupName(m *module, scope ir.ScopeID, name string, accept func(ir.DeclarationKind) bool, intrinsics map[string]bool) (target, binding) {
	for depth := 0; scope != "" && depth < 256; depth++ {
		for _, i := range m.byScope[scope] {
			d := &m.decls[i]
			if d.Name == name && accept(d.Kind) {
				return target{m: m, d: d}, resolved(target{m: m, d: d}, "lexical scope")
			}
		}
		scope = m.parent[scope]
	}
	if imp, ok := m.imports[name]; ok {
		return s.importTarget(m, imp, accept)
	}
	if intrinsics[name] {
		t := target{intrinsic: name}
		return t, resolved(t, "language intrinsic")
	}
	if provider, ok := frameworkGlobals[name]; ok {
		t := target{external: provider}
		if g, known := frameworkPackages[name]; known {
			t.pkg, t.path = g.pkg, g.path
			return t, resolved(t, "external: "+name+" is provided by "+provider)
		}
		return t, unresolved(semantic.CauseExternalDependency, "framework_global: "+name+" is provided by "+provider+" outside the repository")
	}
	return target{}, unresolved(semantic.CauseAnalysisLimitation, "symbol_not_found: "+name+" is not declared in scope, imported or a known global")
}

// moduleFailure explains a specifier that reached no discovered file.
func moduleFailure(status moduleStatus, spec string) binding {
	if status == moduleExternal {
		return unresolved(semantic.CauseExternalDependency, "external_package: "+spec+" is not part of the repository")
	}
	return unresolved(semantic.CauseAnalysisLimitation, "module_not_found: "+spec+" has no discovered file in the checkout")
}

// importTarget follows an import binding to the declaration it names.
func (s *run) importTarget(m *module, imp importBinding, accept func(ir.DeclarationKind) bool) (target, binding) {
	if isAssetSpecifier(imp.module) {
		return target{}, unsupported("asset_import: " + imp.module + " is not a source module")
	}
	t, status := s.resolveModule(m, imp.module)
	if status == moduleExternal {
		pt := packageTarget(imp.module, imp.name)
		if pt.pkg == "" {
			return pt, moduleFailure(status, imp.module)
		}
		return pt, resolved(pt, "external: import from the package "+imp.module)
	}
	if status != moduleFound {
		return target{}, moduleFailure(status, imp.module)
	}
	if imp.name == "*" {
		ns := target{m: t, namespace: true}
		return ns, resolved(ns, "namespace import")
	}
	return s.export(t, imp.name, accept)
}

// export finds what a module exposes under a name: its own declaration, a
// named re-export or a namespace re-export, or a name forwarded by
// export *. Re-exports are followed through other discovered modules with
// a cycle guard; export * never forwards the default export.
func (s *run) export(m *module, name string, accept func(ir.DeclarationKind) bool) (target, binding) {
	return s.exportFrom(m, name, accept, map[*module]bool{})
}

func (s *run) exportFrom(m *module, name string, accept func(ir.DeclarationKind) bool, visited map[*module]bool) (target, binding) {
	if visited[m] {
		return target{}, unresolved(semantic.CauseAnalysisLimitation, "re_export_cycle: "+m.in.Source.Path+" re-exports itself")
	}
	visited[m] = true
	if name == "default" {
		if d, ok := m.defaultExport(); ok {
			tt := target{m: m, d: d}
			return tt, resolved(tt, "default export of "+m.in.Source.Path)
		}
	} else if d, ok := m.topLevel(name, accept); ok {
		tt := target{m: m, d: d}
		return tt, resolved(tt, "import of "+m.in.Source.Path)
	}
	for _, re := range m.reexports {
		if re.exported != name {
			continue
		}
		if re.module == "" {
			if d, ok := m.topLevel(re.name, accept); ok {
				tt := target{m: m, d: d}
				return tt, resolved(tt, "import of "+m.in.Source.Path+" as "+name)
			}
			continue
		}
		t, status := s.resolveModule(m, re.module)
		if status != moduleFound {
			return target{}, moduleFailure(status, re.module)
		}
		if re.name == "*" {
			ns := target{m: t, namespace: true}
			return ns, resolved(ns, "namespace re-export of "+t.in.Source.Path)
		}
		return s.exportFrom(t, re.name, accept, visited)
	}
	external := ""
	if name != "default" {
		for _, re := range m.reexports {
			if re.name != "*" || re.exported != "" {
				continue
			}
			t, status := s.resolveModule(m, re.module)
			if status == moduleExternal {
				external = re.module
				continue
			}
			if status != moduleFound || visited[t] {
				continue
			}
			if tt, b := s.exportFrom(t, name, accept, visited); b.status == semantic.LookupResolved {
				return tt, b
			}
		}
	}
	if external != "" {
		return target{external: external}, unresolved(semantic.CauseExternalDependency, "re_exported_from_external: "+name+" may come from "+external+" through export * in "+m.in.Source.Path)
	}
	if name == "default" {
		return target{}, unresolved(semantic.CauseAnalysisLimitation, "default_export_not_identified: "+m.in.Source.Path+" declares no syntactic default export")
	}
	return target{}, unresolved(semantic.CauseAnalysisLimitation, "export_not_found: "+m.in.Source.Path+" declares no module-level "+name+" and no re-export provides it")
}

// member finds a member of a target, following the base classes of a class
// or interface.
func (s *run) member(t target, name string, accept func(ir.DeclarationKind) bool) (target, bool) {
	if t.namespace {
		if tt, b := s.export(t.m, name, accept); b.status == semantic.LookupResolved {
			return tt, true
		}
		return target{}, false
	}
	return s.memberIn(t, name, accept, map[ir.DeclarationID]bool{}, 0)
}

func (s *run) memberIn(t target, name string, accept func(ir.DeclarationKind) bool, visited map[ir.DeclarationID]bool, depth int) (target, bool) {
	defer s.leave()
	if !s.enter() {
		return target{}, false
	}
	if t.d == nil || depth > 16 || visited[t.d.ID] {
		return target{}, false
	}
	visited[t.d.ID] = true
	if d, ok := t.m.member(t.d.ID, name, accept); ok {
		return target{m: t.m, d: d}, true
	}
	for _, base := range s.bases(t) {
		if r, ok := s.memberIn(base, name, accept, visited, depth+1); ok {
			return r, true
		}
	}
	return target{}, false
}

// bases resolves every extends clause of a class, interface or alias
// (an alias over an intersection extends its named constituents).
func (s *run) bases(t target) []target {
	if t.d == nil || t.d.Type == nil {
		return nil
	}
	var out []target
	for _, h := range t.d.Type.Heritage {
		if h.Kind != ir.HeritageExtends {
			continue
		}
		if base, b := s.resolveTypeRef(t.m, h.TypeRefID, 0); b.status == semantic.LookupResolved && base.d != nil {
			out = append(out, base)
		}
	}
	return out
}

// baseType is the first resolved base class, for super calls.
func (s *run) baseType(t target) (target, bool) {
	if bases := s.bases(t); len(bases) > 0 {
		return bases[0], true
	}
	return target{}, false
}

// unresolvedBase reports the first written base of a declaration, or of its
// resolved bases, that this tier cannot follow, so that a member which is
// not declared in the repository is attributed to that base.
func (s *run) unresolvedBase(t target) (binding, bool) {
	visited := map[ir.DeclarationID]bool{}
	var walk func(t target, depth int) (binding, bool)
	walk = func(t target, depth int) (binding, bool) {
		if t.d == nil || t.d.Type == nil || depth > 16 || visited[t.d.ID] {
			return binding{}, false
		}
		visited[t.d.ID] = true
		for _, h := range t.d.Type.Heritage {
			if h.Kind != ir.HeritageExtends {
				continue
			}
			base, b := s.resolveTypeRef(t.m, h.TypeRefID, 0)
			if b.status == semantic.LookupResolved && base.d != nil {
				if r, ok := walk(base, depth+1); ok {
					return r, true
				}
				continue
			}
			spelling := t.m.types[h.TypeRefID].Spelling
			switch {
			case base.intrinsic != "":
				return unresolved(semantic.CauseExternalDependency, "runtime_library: "+t.d.Name+" extends the platform type "+spelling), true
			case b.cause == semantic.CauseExternalDependency || base.external != "":
				return unresolved(semantic.CauseExternalDependency, "external_base: "+t.d.Name+" extends "+spelling+" outside the repository"), true
			default:
				return unresolved(semantic.CauseAnalysisLimitation, "unresolved_base: "+t.d.Name+" extends "+spelling+", which is not resolvable"), true
			}
		}
		return binding{}, false
	}
	return walk(t, 0)
}

// externalBase finds the first base of a class, interface or alias, or of
// its resolved bases, that comes from a package: a member the repository
// does not declare is that base's (this.setState of a React component).
func (s *run) externalBase(t target) (target, bool) {
	visited := map[ir.DeclarationID]bool{}
	var walk func(t target, depth int) (target, bool)
	walk = func(t target, depth int) (target, bool) {
		if t.d == nil || t.d.Type == nil || depth > 16 || visited[t.d.ID] {
			return target{}, false
		}
		visited[t.d.ID] = true
		for _, h := range t.d.Type.Heritage {
			if h.Kind != ir.HeritageExtends {
				continue
			}
			base, b := s.resolveTypeRef(t.m, h.TypeRefID, 0)
			if b.status != semantic.LookupResolved {
				continue
			}
			if base.pkg != "" {
				return base, true
			}
			if base.d != nil {
				if r, ok := walk(base, depth+1); ok {
					return r, true
				}
			}
		}
		return target{}, false
	}
	return walk(t, 0)
}

// resolveTypeRef binds a written type to its declaration or intrinsic.
// maxNesting bounds the binders on the stack at once.
const maxNesting = 256

// inferenceKey names an expression, or a declaration, whose type is being
// inferred.
type inferenceKey struct {
	m    *module
	expr ir.ExpressionID
	decl ir.DeclarationID
}

// begin marks k as being inferred; false means it already is (a cycle).
func (s *run) begin(k inferenceKey) bool {
	if s.inferring == nil {
		s.inferring = map[inferenceKey]bool{}
	}
	if s.inferring[k] {
		return false
	}
	s.inferring[k] = true
	return true
}

// enter admits one more binder on the stack; leave must follow.
func (s *run) enter() bool {
	s.nesting++
	return s.nesting <= maxNesting
}

func (s *run) leave() { s.nesting-- }

func (s *run) resolveTypeRef(m *module, id ir.TypeRefID, depth int) (target, binding) {
	defer s.leave()
	if !s.enter() {
		return target{}, unsupported("type inference nests too deeply")
	}
	t, ok := m.types[id]
	if !ok {
		return target{}, unsupported("type reference is not in the syntax")
	}
	switch t.Kind {
	case ir.TypePrimitive:
		tt := target{intrinsic: t.Primitive}
		return tt, resolved(tt, "predefined type")
	case ir.TypeArray:
		tt := target{intrinsic: "Array", tm: m, args: []ir.TypeRefID{t.ElementTypeID}}
		return tt, resolved(tt, "array type")
	case ir.TypeNamed:
		if t.Named == nil || len(t.Named.Segments) == 0 {
			return target{}, unsupported("named type without segments")
		}
		first := t.Named.Segments[0].Name
		tt, b := s.lookupName(m, t.ScopeID, first, typeKind, predefinedTypes)
		if b.status != semantic.LookupResolved {
			if !tt.ok() && !predefinedTypes[first] {
				// A value-only binding (a namespace import used as a type
				// qualifier) still qualifies the remaining segments.
				if imp, ok := m.imports[first]; ok && imp.name == "*" {
					tt, b = s.importTarget(m, imp, typeKind)
				}
			}
			if b.status != semantic.LookupResolved {
				return tt, b
			}
		}
		for _, segment := range t.Named.Segments[1:] {
			if tt.pkg != "" {
				// A type of a package namespace (React.FC, express.Request).
				tt = tt.child(segment.Name)
				continue
			}
			if tt.intrinsic != "" {
				// A platform namespace (NodeJS.Timeout, Intl.Collator).
				tt = target{intrinsic: tt.intrinsic + "." + segment.Name}
				continue
			}
			next, ok := s.member(tt, segment.Name, typeKind)
			if !ok {
				return target{}, unresolved(semantic.CauseAnalysisLimitation, "member_type_not_found: "+segment.Name+" is not a type member of "+first)
			}
			tt = next
		}
		if tt.namespace {
			return tt, unsupported("a namespace is not a type")
		}
		if len(t.Named.Segments) > 1 {
			b = resolved(tt, "qualified type through "+first)
		}
		if tt.intrinsic != "" && len(t.Named.Segments) == 1 {
			args := t.Named.Segments[0].TypeArguments
			if transparentTypes[tt.intrinsic] && len(args) > 0 && depth < 8 {
				// Readonly<T>, Partial<T>, Omit<T, K>, Awaited<T> denote T's members.
				return s.resolveTypeRef(m, args[0], depth+1)
			}
			tt.tm, tt.args = m, args
			b = resolved(tt, "predefined type")
		}
		return tt, b
	case ir.TypeUnion, ir.TypeIntersection, ir.TypeFunction, ir.TypeStructural, ir.TypeLiteral, ir.TypeOperator:
		return target{}, unsupported("composite type has no single declaration; its components are bound separately")
	}
	return target{}, unsupported("opaque type syntax: " + t.Spelling)
}

// typeOf infers the declared or constructed type of an expression, one
// member at a time and without any runtime evaluation.
func (s *run) typeOf(m *module, id ir.ExpressionID, depth int) (target, bool) {
	defer s.leave()
	if !s.enter() {
		return target{}, false
	}
	k := inferenceKey{m: m, expr: id}
	if !s.begin(k) {
		return target{}, false
	}
	defer delete(s.inferring, k)
	x, ok := m.exprs[id]
	if !ok || depth > 8 {
		return target{}, false
	}
	switch x.Kind {
	case ir.ExpressionThis:
		if d, ok := m.enclosingType(x.ScopeID); ok {
			return target{m: m, d: d}, true
		}
	case ir.ExpressionName:
		if x.Name == nil || len(x.Name.Segments) == 0 {
			return target{}, false
		}
		t, b := s.lookupName(m, x.ScopeID, x.Name.Segments[0].Text, valueKind, globals)
		if b.status != semantic.LookupResolved {
			// A value from a package, framework or platform outside the
			// repository stays external through every member access.
			return t, t.external != ""
		}
		if t.pkg != "" {
			return t, true
		}
		return s.typeOfTarget(t, depth+1)
	case ir.ExpressionMemberAccess:
		if x.Name == nil || len(x.OperandIDs) == 0 {
			return target{}, false
		}
		receiver, ok := s.typeOf(m, x.OperandIDs[0], depth+1)
		if !ok {
			return target{}, false
		}
		if receiver.external != "" || receiver.intrinsic != "" {
			return receiver.child(x.Name.Segments[0].Text), true
		}
		member, ok := s.member(receiver, x.Name.Segments[0].Text, memberKind)
		if !ok {
			if base, ok := s.externalBase(receiver); ok {
				return base.child(x.Name.Segments[0].Text), true
			}
			if bb, ok := s.unresolvedBase(receiver); ok && bb.cause == semantic.CauseExternalDependency {
				return target{external: bb.reason}, true
			}
			return target{}, false
		}
		return s.typeOfTarget(member, depth+1)
	case ir.ExpressionArrayAccess:
		if len(x.OperandIDs) == 0 {
			return target{}, false
		}
		receiver, ok := s.typeOf(m, x.OperandIDs[0], depth+1)
		if !ok {
			return target{}, false
		}
		if (receiver.intrinsic == "Array" || receiver.intrinsic == "ReadonlyArray") && receiver.path == "" {
			return s.element(receiver, 0)
		}
		if receiver.external != "" || receiver.intrinsic != "" {
			return receiver.child("[]"), true
		}
		return target{}, false
	case ir.ExpressionUnary:
		if x.Operator == "await" && len(x.OperandIDs) == 1 {
			t, ok := s.typeOf(m, x.OperandIDs[0], depth+1)
			if !ok {
				return target{}, false
			}
			if t.intrinsic == "Promise" {
				if r, ok := s.element(t, 0); ok {
					return r, true
				}
			}
			return t, true
		}
	case ir.ExpressionCall:
		c, ok := m.calls[x.OccurrenceID]
		if !ok {
			return target{}, false
		}
		if c.Kind == ir.CallObjectCreation {
			t, b := s.resolveTypeRef(m, c.ConstructedTypeID, 0)
			return t, b.status == semantic.LookupResolved && t.ok() || t.external != ""
		}
		if c.ReceiverID != "" {
			if receiver, ok := s.typeOf(m, c.ReceiverID, depth+1); ok && (receiver.external != "" || receiver.intrinsic != "") {
				return s.libraryResult(receiver, c.Name), true
			}
		}
		callee, b := s.bindCallTarget(m, c)
		if b.status != semantic.LookupResolved {
			return callee, callee.external != ""
		}
		// A platform function's result is named by the call (fetch().then);
		// a constructor-like callable (String(x), Array(n)) returns its own
		// type.
		if callee.pkg != "" || (callee.intrinsic != "" && (callee.path != "" || !startsUpper(callee.intrinsic))) {
			return callee.child("()"), true
		}
		if callee.intrinsic != "" {
			// The result of a runtime-library call is a runtime-library value.
			return callee, true
		}
		if callee.d != nil && (callee.d.Callable == nil || callee.d.Callable.ReturnTypeID == "") {
			// Without a declared return type, what the function returns:
			// an arrow function's expression body (const make = () => new
			// Client()), or the first return statement whose value has a
			// type.
			if body, ok := s.arrowBody(callee); ok {
				return s.typeOf(callee.m, body, depth+1)
			}
			if callee.d.Callable != nil {
				for _, returned := range callee.d.Callable.ReturnExpressionIDs {
					if t, ok := s.typeOf(callee.m, returned, depth+1); ok {
						return t, true
					}
				}
			}
		}
		if callee.d == nil || callee.d.Callable == nil || callee.d.Callable.ReturnTypeID == "" {
			return target{}, false
		}
		t, rb := s.resolveTypeRef(callee.m, callee.d.Callable.ReturnTypeID, 0)
		return t, rb.status == semantic.LookupResolved && t.ok()
	case ir.ExpressionCast:
		if x.TypeRefID != "" {
			t, b := s.resolveTypeRef(m, x.TypeRefID, 0)
			return t, b.status == semantic.LookupResolved && t.ok()
		}
	case ir.ExpressionParenthesized:
		if len(x.OperandIDs) == 1 {
			return s.typeOf(m, x.OperandIDs[0], depth+1)
		}
	case ir.ExpressionLiteral:
		// A literal is a value of its primitive type; a regular expression
		// is kept as a string literal whose lexeme starts with its slash.
		if x.Literal != nil {
			switch x.Literal.Kind {
			case ir.LiteralString:
				if strings.HasPrefix(x.Literal.Lexeme, "/") {
					return target{intrinsic: "RegExp"}, true
				}
				return target{intrinsic: "string"}, true
			case ir.LiteralInteger, ir.LiteralFloating:
				return target{intrinsic: "number"}, true
			case ir.LiteralBoolean:
				return target{intrinsic: "boolean"}, true
			}
		}
	case ir.ExpressionArrayInitializer:
		return target{intrinsic: "Array"}, true
	case ir.ExpressionBinary:
		switch x.Operator {
		case "+":
			for _, operand := range x.OperandIDs {
				if t, ok := s.typeOf(m, operand, depth+1); ok && t.intrinsic == "string" && t.path == "" {
					return t, true
				}
			}
		case "-", "*", "/", "%", "**", "<<", ">>", ">>>", "&", "|", "^":
			return target{intrinsic: "number"}, true
		case "===", "!==", "==", "!=", "<", ">", "<=", ">=", "instanceof", "in":
			return target{intrinsic: "boolean"}, true
		case "??", "||":
			// The left operand's type when it has one (x ?? [] is x's).
			for _, operand := range x.OperandIDs {
				if t, ok := s.typeOf(m, operand, depth+1); ok {
					return t, true
				}
			}
		}
	case ir.ExpressionConditional:
		// c ? a : b is typed by the branch that has a type.
		if len(x.OperandIDs) == 3 {
			for _, operand := range x.OperandIDs[1:] {
				if t, ok := s.typeOf(m, operand, depth+1); ok {
					return t, true
				}
			}
		}
	}
	return target{}, false
}

// arrowBody is the expression a function literal returns when its body is
// an expression rather than a block: a const holding one is a callable
// declaration with that body, a variable one has it as initializer.
func (s *run) arrowBody(t target) (ir.ExpressionID, bool) {
	if t.d == nil {
		return "", false
	}
	if t.d.Callable != nil && t.d.Callable.BodyExpressionID != "" {
		return t.d.Callable.BodyExpressionID, true
	}
	if t.d.Variable == nil || t.d.Variable.InitializerID == "" {
		return "", false
	}
	x, ok := t.m.exprs[t.d.Variable.InitializerID]
	if !ok || x.Kind != ir.ExpressionLambda || x.Lambda == nil || x.Lambda.BodyExpressionID == "" {
		return "", false
	}
	return x.Lambda.BodyExpressionID, true
}

// typeOfTarget is the type a bound declaration has: itself for types and
// namespaces, its declared or constructed type for variables.
func (s *run) typeOfTarget(t target, depth int) (target, bool) {
	if t.namespace || t.intrinsic != "" {
		return t, true
	}
	if t.external != "" {
		return t, true
	}
	if t.d == nil || depth > 8 {
		return target{}, false
	}
	switch t.d.Kind {
	case ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationNamespace, ir.DeclarationTypeAlias:
		return t, true
	}
	k := inferenceKey{m: t.m, decl: t.d.ID}
	if !s.begin(k) {
		return target{}, false
	}
	defer delete(s.inferring, k)
	if t.d.Kind == ir.DeclarationParameter && (t.d.Variable == nil || t.d.Variable.DeclaredTypeID == "") {
		if elem, ok := s.callbackParameter(t, depth+1); ok {
			return elem, true
		}
	}
	if v := t.d.Variable; v != nil {
		if v.DeclaredTypeID != "" {
			tt, b := s.resolveTypeRef(t.m, v.DeclaredTypeID, 0)
			return tt, b.status == semantic.LookupResolved && tt.ok()
		}
		if v.InitializerID != "" {
			return s.typeOf(t.m, v.InitializerID, depth+1)
		}
	}
	return target{}, false
}

// libraryResult types the result of a method call on a platform or
// external value: element-returning array methods yield the element, the
// array-returning ones the same array, and everything else the value's
// own provider.
func (s *run) libraryResult(receiver target, method string) target {
	if receiver.external != "" || receiver.path != "" || platformNamespaces[receiver.intrinsic] {
		// A package value, a member of a platform object, or a platform
		// namespace (JSON, Math, console): the result is named by the call,
		// not typed as the receiver.
		return receiver.child(method).child("()")
	}
	switch receiver.intrinsic {
	case "Array", "ReadonlyArray":
		switch method {
		case "find", "findLast", "at", "pop", "shift":
			if elem, ok := s.element(receiver, 0); ok {
				return elem
			}
		case "filter", "slice", "reverse", "sort", "concat", "toSorted", "toReversed", "with", "splice":
			return receiver
		case "map", "flatMap", "flat":
			return target{intrinsic: "Array"}
		}
	case "Map":
		if method == "get" {
			if value, ok := s.element(receiver, 1); ok {
				return value
			}
		}
	case "Promise":
		switch method {
		case "then", "catch", "finally":
			return receiver
		}
	}
	return target{intrinsic: receiver.intrinsic}
}

// iterationMethods type the first callback parameter with the element.
var iterationMethods = map[string]bool{"map": true, "forEach": true, "filter": true, "find": true, "findIndex": true, "findLast": true, "findLastIndex": true, "some": true, "every": true, "flatMap": true}

// callbackParameter types an untyped lambda parameter from the collection
// method the lambda is passed to: the element for array and set iteration,
// the accumulator's element for reduce, the value and key for Map.forEach,
// and the provider itself for callbacks of external APIs.
func (s *run) callbackParameter(t target, depth int) (target, bool) {
	ref, ok := t.m.lambdaParams[t.d.ID]
	if !ok || depth > 8 {
		return target{}, false
	}
	arg, ok := t.m.callArgs[ref.expr]
	if !ok || arg.call.ReceiverID == "" {
		return target{}, false
	}
	receiver, ok := s.typeOf(t.m, arg.call.ReceiverID, depth)
	if !ok {
		return target{}, false
	}
	if receiver.external != "" {
		return receiver, true
	}
	method := arg.call.Name
	switch receiver.intrinsic {
	case "Array", "ReadonlyArray", "Set":
		switch {
		case iterationMethods[method] && ref.index == 0, method == "sort" || method == "toSorted":
			return s.element(receiver, 0)
		case (method == "reduce" || method == "reduceRight") && ref.index == 1:
			return s.element(receiver, 0)
		}
	case "Map":
		if method == "forEach" && ref.index < 2 {
			return s.element(receiver, 1-ref.index)
		}
	}
	return target{}, false
}

// bindCallTarget finds the callee of a call without emitting a lookup.
func (s *run) bindCallTarget(m *module, c ir.Call) (target, binding) {
	defer s.leave()
	if !s.enter() {
		return target{}, unsupported("type inference nests too deeply")
	}
	if c.Kind == ir.CallObjectCreation {
		t, b := s.resolveTypeRef(m, c.ConstructedTypeID, 0)
		if b.status != semantic.LookupResolved {
			return target{}, b
		}
		if t.d == nil {
			return t, resolved(t, "constructed intrinsic")
		}
		if ctor, ok := s.member(t, "constructor", func(k ir.DeclarationKind) bool { return k == ir.DeclarationConstructor }); ok {
			return ctor, resolved(ctor, "constructor of "+t.d.Name)
		}
		return t, resolved(t, "constructed class without a written constructor")
	}
	if c.Name == "()" {
		return target{}, unsupported("callee is a computed expression, not a name")
	}
	if c.ReceiverID == "" {
		if c.Name == "" {
			return target{}, unsupported("callee is not a name")
		}
		if c.Name == "import" {
			return target{}, unresolved(semantic.CauseAnalysisLimitation, "dynamic_import: import() targets are not followed at the syntax tier")
		}
		t, b := s.lookupName(m, c.Occurrence.ScopeID, c.Name, callableKind, globals)
		if b.status == semantic.LookupResolved && t.namespace {
			return target{}, unsupported("a module namespace is not callable")
		}
		return t, b
	}
	receiver, ok := m.exprs[c.ReceiverID]
	if !ok {
		return target{}, unsupported("receiver is not in the syntax")
	}
	if receiver.Kind == ir.ExpressionSuper {
		enclosing, ok := m.enclosingType(receiver.ScopeID)
		if !ok {
			return target{}, unresolved(semantic.CauseAnalysisLimitation, "super outside a class body")
		}
		base, ok := s.baseType(target{m: m, d: enclosing})
		if !ok {
			if external, ok := s.externalBase(target{m: m, d: enclosing}); ok {
				if c.Name == "constructor" {
					return external, resolved(external, "external: constructor of the base class "+joinPath(external.pkg, external.path))
				}
				t := external.child(c.Name)
				return t, resolved(t, "external: member of the base class "+joinPath(external.pkg, external.path))
			}
			if bb, ok := s.unresolvedBase(target{m: m, d: enclosing}); ok {
				return target{}, bb
			}
			return target{}, unresolved(semantic.CauseAnalysisLimitation, "base class of "+enclosing.Name+" is not resolvable")
		}
		if member, ok := s.member(base, c.Name, callableKind); ok {
			return member, resolved(member, "base class member")
		}
		if c.Name == "constructor" {
			return base, resolved(base, "base class without a written constructor")
		}
		if bb, ok := s.unresolvedBase(base); ok {
			return target{}, bb
		}
		return target{}, unresolved(semantic.CauseAnalysisLimitation, "member_not_found: "+c.Name+" on base class "+base.d.Name)
	}
	return s.bindMember(m, c.ReceiverID, c.Name, callableKind)
}

// bindMember binds a member access through the receiver's type.
func (s *run) bindMember(m *module, receiverID ir.ExpressionID, name string, accept func(ir.DeclarationKind) bool) (target, binding) {
	receiver, ok := s.typeOf(m, receiverID, 0)
	if ok {
		if receiver.pkg != "" {
			t := receiver.child(name)
			return t, resolved(t, "external: member of "+joinPath(receiver.pkg, receiver.path))
		}
		if receiver.external != "" {
			return target{}, unresolved(semantic.CauseExternalDependency, "external_value: "+name+" is a member of a value from "+receiver.external)
		}
		if receiver.intrinsic != "" {
			t := receiver.child(name)
			return t, resolved(t, "platform library member of "+joinPath(receiver.intrinsic, receiver.path))
		}
		if member, found := s.member(receiver, name, accept); found {
			if member.namespace {
				return member, skip
			}
			return member, resolved(member, "member of "+describe(receiver))
		}
		if base, ok := s.externalBase(receiver); ok {
			t := base.child(name)
			return t, resolved(t, "external: member inherited from "+joinPath(base.pkg, base.path))
		}
		if bb, ok := s.unresolvedBase(receiver); ok {
			bb.reason += " (member " + name + ")"
			return target{}, bb
		}
		return target{}, unresolved(semantic.CauseAnalysisLimitation, "member_not_found: "+name+" is not declared on "+describe(receiver))
	}
	if root := s.rootName(m, receiverID); root != "" {
		if imp, imported := m.imports[root]; imported {
			if t, b := s.importTarget(m, imp, anyKind); b.status != semantic.LookupResolved {
				return target{}, b
			} else if t.pkg != "" {
				// A chain from a package value this tier could not follow
				// member by member.
				return target{}, unresolved(semantic.CauseExternalDependency, "external_value: "+name+" is a member of a value from "+imp.module)
			}
		} else if globals[root] {
			return target{}, unresolved(semantic.CauseExternalDependency, "runtime_library: "+root+"."+name+" belongs to the platform library")
		}
	}
	return target{}, unresolved(semantic.CauseAnalysisLimitation, "receiver_type_unknown: the type of the receiver of "+name+" is not declared or constructed in the syntax")
}

// rootName is the leftmost identifier of a receiver chain.
func (s *run) rootName(m *module, id ir.ExpressionID) string {
	for depth := 0; depth < 32; depth++ {
		x, ok := m.exprs[id]
		if !ok {
			return ""
		}
		switch x.Kind {
		case ir.ExpressionName:
			if x.Name != nil && len(x.Name.Segments) > 0 {
				return x.Name.Segments[0].Text
			}
			return ""
		case ir.ExpressionMemberAccess, ir.ExpressionParenthesized, ir.ExpressionCast, ir.ExpressionArrayAccess:
			if len(x.OperandIDs) == 0 {
				return ""
			}
			id = x.OperandIDs[0]
		case ir.ExpressionCall:
			c, ok := m.calls[x.OccurrenceID]
			if !ok || c.ReceiverID == "" {
				return ""
			}
			id = c.ReceiverID
		default:
			return ""
		}
	}
	return ""
}

func describe(t target) string {
	switch {
	case t.namespace:
		return "module " + t.m.in.Source.Path
	case t.d != nil:
		return string(t.d.Kind) + " " + t.d.Name
	case t.intrinsic != "":
		return "intrinsic " + t.intrinsic
	case t.external != "":
		return "external " + t.external
	}
	return "unknown"
}

// assetExtensions are imported files that are not source modules.
var assetExtensions = map[string]bool{".css": true, ".scss": true, ".sass": true, ".less": true, ".styl": true, ".json": true, ".svg": true, ".png": true, ".jpg": true, ".jpeg": true, ".gif": true, ".webp": true, ".avif": true, ".ico": true, ".bmp": true, ".woff": true, ".woff2": true, ".ttf": true, ".otf": true, ".eot": true, ".md": true, ".mdx": true, ".txt": true, ".yaml": true, ".yml": true, ".toml": true, ".wasm": true, ".html": true, ".csv": true, ".xml": true, ".mp3": true, ".mp4": true, ".webm": true, ".wav": true, ".pdf": true, ".glsl": true, ".graphql": true, ".gql": true}

// isAssetSpecifier reports an import of a stylesheet, data or media file,
// or a bundler-transformed module (`?raw`, `?url`, `?worker`).
func isAssetSpecifier(spec string) bool {
	if strings.IndexByte(spec, '?') >= 0 {
		return true
	}
	return assetExtensions[strings.ToLower(pathpkg.Ext(spec))]
}

// binder collects the lookups of one affected file.
type binder struct {
	s       *run
	m       *module
	lookups []semantic.Lookup
	// sites records, by lookup, the syntax each lookup stands for, so the
	// compiler tier can bind the same sites.
	sites map[string]site
}

func (s *run) bind(m *module, sites map[string]site) ([]semantic.Lookup, error) {
	b := &binder{s: s, m: m, sites: sites}
	f := m.file
	for _, c := range f.Calls {
		if err := b.emit(c.Occurrence, semantic.LookupCall, s.bindCall(m, c), "call"); err != nil {
			return nil, err
		}
	}
	for _, r := range f.References {
		kind := "member"
		if r.Kind == ir.ReferenceName {
			kind = "name"
		}
		if err := b.emit(r.Occurrence, semantic.LookupMember, s.bindReference(m, r), kind); err != nil {
			return nil, err
		}
	}
	for _, t := range f.TypeUses {
		// Composite types name no declaration; their element and component
		// types are separate uses with their own lookups.
		if tr, ok := m.types[t.TypeRefID]; ok && compositeType(tr.Kind) {
			continue
		}
		kind := semantic.LookupType
		if t.Role == ir.TypeUseHeritage {
			kind = semantic.LookupInheritance
		}
		_, bd := s.resolveTypeRef(m, t.TypeRefID, 0)
		if err := b.emit(t.Occurrence, kind, bd, "type"); err != nil {
			return nil, err
		}
	}
	for _, a := range f.Annotations {
		if err := b.emit(a.Occurrence, semantic.LookupMember, s.bindDecorator(m, a), "decorator"); err != nil {
			return nil, err
		}
	}
	if err := b.overrides(); err != nil {
		return nil, err
	}
	return b.lookups, nil
}

func compositeType(kind ir.TypeKind) bool {
	switch kind {
	case ir.TypeArray, ir.TypeUnion, ir.TypeIntersection, ir.TypeFunction, ir.TypeStructural, ir.TypeLiteral, ir.TypeOperator:
		return true
	}
	return false
}

func (s *run) bindCall(m *module, c ir.Call) binding {
	_, b := s.bindCallTarget(m, c)
	return b
}

func (s *run) bindReference(m *module, r ir.Reference) binding {
	if len(r.Name.Segments) == 0 {
		return unsupported("reference without a name")
	}
	name := r.Name.Segments[0].Text
	if r.Kind == ir.ReferenceName {
		t, b := s.lookupName(m, r.Occurrence.ScopeID, name, valueKind, globals)
		if t.namespace {
			// A namespace qualifier is lexical evidence, not a declaration.
			return skip
		}
		return b
	}
	receiver, ok := m.exprs[r.ReceiverID]
	if ok && receiver.Kind == ir.ExpressionSuper {
		return s.bindSuperMember(m, receiver, name)
	}
	if ok && receiver.Kind == ir.ExpressionName && receiver.Name != nil && len(receiver.Name.Segments) > 0 {
		if imp, imported := m.imports[receiver.Name.Segments[0].Text]; imported && imp.name == "*" {
			t, b := s.importTarget(m, imp, anyKind)
			if b.status != semantic.LookupResolved {
				return b
			}
			if t.pkg != "" {
				member := t.child(name)
				return resolved(member, "external: member of "+joinPath(t.pkg, t.path))
			}
			if member, found := s.member(t, name, memberKind); found {
				if member.namespace {
					return skip
				}
				return resolved(member, "member of "+describe(t))
			}
			_, b = s.export(t.m, name, memberKind)
			return b
		}
	}
	_, b := s.bindMember(m, r.ReceiverID, name, memberKind)
	return b
}

func (s *run) bindSuperMember(m *module, receiver ir.Expression, name string) binding {
	enclosing, ok := m.enclosingType(receiver.ScopeID)
	if !ok {
		return unresolved(semantic.CauseAnalysisLimitation, "super outside a class body")
	}
	base, ok := s.baseType(target{m: m, d: enclosing})
	if !ok {
		if external, ok := s.externalBase(target{m: m, d: enclosing}); ok {
			return resolved(external.child(name), "external: member of the base class "+joinPath(external.pkg, external.path))
		}
		if bb, ok := s.unresolvedBase(target{m: m, d: enclosing}); ok {
			return bb
		}
		return unresolved(semantic.CauseAnalysisLimitation, "base class of "+enclosing.Name+" is not resolvable")
	}
	if member, ok := s.member(base, name, memberKind); ok {
		return resolved(member, "base class member")
	}
	if bb, ok := s.unresolvedBase(base); ok {
		return bb
	}
	return unresolved(semantic.CauseAnalysisLimitation, "member_not_found: "+name+" on base class "+base.d.Name)
}

func (s *run) bindDecorator(m *module, a ir.Annotation) binding {
	t, ok := m.types[a.TypeRefID]
	if !ok || t.Named == nil || len(t.Named.Segments) == 0 {
		return unsupported("decorator expression is not a name")
	}
	first := t.Named.Segments[0].Name
	tt, b := s.lookupName(m, a.Occurrence.ScopeID, first, valueKind, globals)
	if b.status != semantic.LookupResolved {
		return b
	}
	for _, segment := range t.Named.Segments[1:] {
		if tt.pkg != "" || tt.intrinsic != "" {
			tt = tt.child(segment.Name)
			continue
		}
		next, ok := s.member(tt, segment.Name, memberKind)
		if !ok {
			return unresolved(semantic.CauseAnalysisLimitation, "member_not_found: "+segment.Name+" on "+describe(tt))
		}
		tt = next
	}
	if tt.namespace {
		return unsupported("a namespace is not a decorator")
	}
	return resolved(tt, "decorator declaration")
}

// overrides binds each method to the same-named method of its resolved
// base class.
func (b *binder) overrides() error {
	for i := range b.m.decls {
		d := &b.m.decls[i]
		if d.Kind != ir.DeclarationMethod || d.OwnerID == "" {
			continue
		}
		owner, ok := b.m.decl(d.OwnerID)
		if !ok || owner.Kind != ir.DeclarationClass {
			continue
		}
		base, ok := b.s.baseType(target{m: b.m, d: owner})
		if !ok {
			continue
		}
		overridden, ok := b.s.member(base, d.Name, func(k ir.DeclarationKind) bool { return k == ir.DeclarationMethod })
		if !ok {
			continue
		}
		span := d.Span
		if d.NameSpan != nil {
			span = *d.NameSpan
		}
		l, err := b.lookup(graph.ID("lookup", string(b.m.in.Source.FileID), "override", string(d.ID)), semantic.LookupOverride, resolved(overridden, "same-named method of base class "+base.d.Name), b.m.anchor(span))
		if err != nil {
			return err
		}
		l.DeclarationID = d.ID
		b.lookups = append(b.lookups, l)
		if b.sites != nil && d.NameSpan != nil {
			b.sites[l.ID] = site{kind: "override", span: *d.NameSpan}
		}
	}
	return nil
}

func (b *binder) emit(o ir.Occurrence, kind semantic.LookupKind, bd binding, syntax string) error {
	if bd.skip {
		return nil
	}
	l, err := b.lookup(graph.ID("lookup", string(b.m.in.Source.FileID), string(o.ID)), kind, bd, b.m.anchor(o.Span))
	if err != nil {
		return err
	}
	l.OccurrenceID = o.ID
	b.lookups = append(b.lookups, l)
	if b.sites != nil {
		b.sites[l.ID] = site{kind: syntax, span: o.Span}
	}
	return nil
}

func (b *binder) lookup(id string, kind semantic.LookupKind, bd binding, evidence graph.SourceAnchor) (semantic.Lookup, error) {
	if bd.status == semantic.LookupResolved && bd.target.d == nil && bd.target.intrinsic == "" && bd.target.pkg == "" {
		// A module namespace (import * as ns, export * as ns) is a
		// binding without a declaration; using it as a value, callee or
		// type is not a fact this tier can attribute.
		bd = unsupported("a module namespace is not a declaration")
	}
	l := semantic.Lookup{ID: id, FileID: b.m.in.Source.FileID, Kind: kind, Status: bd.status, Reason: bd.reason, Provenance: "syntax", Evidence: evidence}
	if bd.status != semantic.LookupResolved {
		l.Cause = bd.cause
		if l.Cause == "" {
			l.Cause = semantic.CauseUnknown
		}
		return l, nil
	}
	symbol, err := b.s.symbolFor(bd.target)
	if err != nil {
		return semantic.Lookup{}, err
	}
	l.SelectedSymbolID, l.CandidateIDs = symbol, []string{symbol}
	return l, nil
}

// symbolFor is the run-local symbol of a target, writing intrinsics on
// first use.
func (s *run) symbolFor(t target) (string, error) {
	switch {
	case t.pkg != "":
		sym := externalSymbol(t.pkg, t.path)
		return sym.ID, s.symbols.addUnique(sym)
	case t.intrinsic != "":
		sym := intrinsic(joinPath(t.intrinsic, t.path))
		return sym.ID, s.symbols.addUnique(sym)
	case t.d != nil:
		return symbolID(t.m.in.Source.FileID, t.d.ID), nil
	}
	return "", fmt.Errorf("resolved binding without a symbol")
}
