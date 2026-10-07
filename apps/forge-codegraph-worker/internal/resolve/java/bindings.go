package java

import (
	"fmt"
	"sort"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// binding is the outcome of one site: the candidate symbol IDs javac
// enumerated, the one it selected, and the status/reason when it did not.
type binding struct {
	ids            []string
	selected       string
	status         semantic.LookupStatus
	reason, typ    string
	cause          semantic.LookupCause
	diagnosticCode string
	err            error
}

func fail(status semantic.LookupStatus, reason string) binding {
	b := binding{status: status, reason: reason, cause: semantic.CauseUnknown}
	if status == semantic.LookupUnsupported {
		b.cause = semantic.CauseAnalysisLimitation
	}
	if status == semantic.LookupAmbiguous {
		b.cause = semantic.CauseAmbiguousBinding
	}
	if strings.HasPrefix(reason, "external_platform_type:") {
		b.cause = semantic.CauseExternalDependency
	}
	if strings.HasPrefix(reason, "compiler_error: ") {
		b.cause = semantic.CauseSourceDiagnostic
		code, _, _ := strings.Cut(strings.TrimPrefix(reason, "compiler_error: "), ":")
		b.diagnosticCode = code
		if strings.Contains(code, "ambiguous") {
			b.status = semantic.LookupAmbiguous
			b.cause = semantic.CauseAmbiguousBinding
		}
	}
	return b
}

func bound(ids []string, reason string) binding {
	unique := map[string]bool{}
	var out []string
	for _, id := range ids {
		if !unique[id] {
			unique[id] = true
			out = append(out, id)
		}
	}
	sort.Strings(out)
	status := semantic.LookupResolved
	if len(out) == 0 {
		status = semantic.LookupUnresolved
	}
	if len(out) > 1 {
		status = semantic.LookupAmbiguous
	}
	return binding{ids: out, status: status, reason: reason}
}

func selectedBinding(sym semantic.Symbol, reason string) binding {
	b := bound([]string{sym.ID}, reason)
	b.selected = sym.ID
	return b
}

// fileContext resolves one affected file against its events.
type fileContext struct {
	s            *run
	v            *fileView
	fe           *fileEvents
	typeBindings map[ir.TypeRefID]binding
	visiting     map[string]bool
	lookups      []semantic.Lookup
	emitted      map[string]bool
	expressions  map[ir.ExpressionID]int // index into v.file.Expressions, built on first use
}

func (s *run) resolveFile(v *fileView, fe *fileEvents) ([]semantic.Lookup, error) {
	c := &fileContext{s: s, v: v, fe: fe, typeBindings: map[ir.TypeRefID]binding{}, visiting: map[string]bool{}, emitted: map[string]bool{}}
	f := v.file
	for _, u := range f.TypeUses {
		kind := semantic.LookupType
		if u.Role == ir.TypeUseHeritage || u.Role == ir.TypeUsePermittedType {
			kind = semantic.LookupInheritance
		}
		if err := c.emit(u.Occurrence, kind, c.typeBinding(u.TypeRefID)); err != nil {
			return nil, err
		}
	}
	for _, call := range f.Calls {
		if err := c.emit(call.Occurrence, semantic.LookupCall, c.occurrenceBinding(call.Occurrence, "call", c.callHead(call))); err != nil {
			return nil, err
		}
	}
	for _, ref := range f.CallableReferences {
		if err := c.emit(ref.Occurrence, semantic.LookupCall, c.occurrenceBinding(ref.Occurrence, "callable_reference", ref.Occurrence.Span)); err != nil {
			return nil, err
		}
	}
	for _, ref := range f.References {
		// Package and module qualifiers and .class/.this/.super are
		// environments or syntax, not declaration lookups.
		if e, found := fe.at(ref.Occurrence.Span, "reference"); found && (e.ElementKind == "PACKAGE" || e.ElementKind == "MODULE" || e.Status == "language_expression") {
			continue
		}
		if err := c.emit(ref.Occurrence, semantic.LookupMember, c.occurrenceBinding(ref.Occurrence, "reference", ref.Occurrence.Span)); err != nil {
			return nil, err
		}
	}
	if f.Module != nil {
		for _, directive := range f.Module.Directives {
			if err := c.emit(directive.Occurrence, semantic.LookupFramework, fail(semantic.LookupUnsupported, "analysis_limitation: module directive graph mapping is not implemented")); err != nil {
				return nil, err
			}
		}
	}
	if err := c.overrides(); err != nil {
		return nil, err
	}
	if err := c.implements(); err != nil {
		return nil, err
	}
	return c.lookups, nil
}

func (c *fileContext) emit(o ir.Occurrence, kind semantic.LookupKind, b binding) error {
	l, err := c.lookup(graph.ID("lookup", string(c.v.fileID()), string(o.ID)), kind, b, c.v.anchor(o.Span))
	if err != nil {
		return err
	}
	l.OccurrenceID = o.ID
	c.emitted[string(o.ID)+"\x00"+string(kind)] = true
	c.lookups = append(c.lookups, l)
	return nil
}

func (c *fileContext) lookup(id string, kind semantic.LookupKind, b binding, evidence graph.SourceAnchor) (semantic.Lookup, error) {
	if b.err != nil {
		return semantic.Lookup{}, b.err
	}
	if b.status == "" {
		b.status = semantic.LookupUnresolved
	}
	if b.reason == "" {
		b.reason = "symbol_not_found: insufficient evidence to distinguish source error from unavailable input"
	}
	l := semantic.Lookup{ID: id, FileID: c.v.fileID(), Kind: kind, Status: b.status, CandidateIDs: b.ids, Reason: b.reason, Provenance: "compiler", Evidence: evidence}
	if b.status != semantic.LookupResolved {
		l.Cause = b.cause
		l.DiagnosticCode = b.diagnosticCode
		if l.Cause == "" {
			switch b.status {
			case semantic.LookupUnsupported:
				l.Cause = semantic.CauseAnalysisLimitation
			case semantic.LookupAmbiguous:
				l.Cause = semantic.CauseAmbiguousBinding
			default:
				l.Cause = semantic.CauseUnknown
			}
		}
		return l, nil
	}
	l.SelectedSymbolID = b.selected
	if l.SelectedSymbolID == "" && len(b.ids) == 1 {
		l.SelectedSymbolID = b.ids[0]
	}
	if l.SelectedSymbolID == "" {
		return l, fmt.Errorf("resolved lookup without unique symbol")
	}
	return l, nil
}

// callHead is the part of a call whose compiler errors are the call's own:
// its receiver and name, up to its first argument or anonymous class body.
// An error inside an argument (an unknown variable passed along) or in the
// body leaves javac's attribution of the call itself standing; the argument
// has its own lookup that carries the error.
func (c *fileContext) callHead(call ir.Call) ir.Span {
	head := call.Occurrence.Span
	cut := head.End.ByteOffset
	within := func(start uint64) {
		if start > head.Start.ByteOffset && start < cut {
			cut = start
		}
	}
	for _, a := range call.Arguments {
		if e, ok := c.expression(a.ExpressionID); ok {
			within(e.Span.Start.ByteOffset)
		}
	}
	if call.AnonymousTypeDeclarationID != "" {
		for _, d := range c.v.file.Declarations {
			if d.ID == call.AnonymousTypeDeclarationID {
				within(d.Span.Start.ByteOffset)
			}
		}
	}
	head.End.ByteOffset = cut
	return head
}

// expression finds an expression of the file's syntax by ID.
func (c *fileContext) expression(id ir.ExpressionID) (ir.Expression, bool) {
	if c.expressions == nil {
		c.expressions = make(map[ir.ExpressionID]int, len(c.v.file.Expressions))
		for i, e := range c.v.file.Expressions {
			c.expressions[e.ID] = i
		}
	}
	i, ok := c.expressions[id]
	if !ok {
		return ir.Expression{}, false
	}
	return c.v.file.Expressions[i], true
}

// occurrenceBinding binds an occurrence from javac's attribution of it. A
// compiler error within own, the part of the occurrence whose errors are
// its own, makes it unresolved whatever javac attributed.
func (c *fileContext) occurrenceBinding(o ir.Occurrence, mode string, own ir.Span) binding {
	e, found := c.fe.at(o.Span, mode)
	if !found && c.fe.failure != "" {
		return fail(semantic.LookupUnsupported, c.fe.failure)
	}
	if !found {
		return fail(semantic.LookupUnsupported, "analysis_limitation: compiler tree could not be mapped to the exact syntax occurrence")
	}
	if diagnostic := c.fe.failureAt(own, e.Status); diagnostic != "" {
		return fail(semantic.LookupUnresolved, diagnostic)
	}
	if e.Status == "unresolved" {
		return fail(semantic.LookupUnresolved, "compiler_unresolved: no attributed declaration; inspect compiler diagnostics and pinned build inputs")
	}
	selected, err := c.targetSymbol(e)
	if err != nil {
		return binding{err: err}
	}
	if selected == nil {
		return fail(semantic.LookupUnsupported, "analysis_limitation: attributed compiler entity has no supported graph symbol origin")
	}
	b := selectedBinding(*selected, "javac_attributed: exact declaration binding")
	if mode == "call" || mode == "callable_reference" {
		// Materialize the complete accessible same-name candidate environment
		// javac emitted, including overloads rejected for this invocation.
		candidates := c.fe.candidates[spanKey{int64(o.Span.Start.ByteOffset), int64(o.Span.End.ByteOffset)}]
		if len(candidates) > 20000 {
			return fail(semantic.LookupUnsupported, "analysis_limitation: invocation candidates exceeded bound")
		}
		ids := append([]string{}, b.ids...)
		for _, candidate := range candidates {
			sym, err := c.targetSymbol(candidate)
			if err != nil {
				return binding{err: err}
			}
			if sym == nil {
				// An overload the graph cannot name (a class file from no
				// pinned input) leaves the environment, not javac's choice.
				continue
			}
			ids = append(ids, sym.ID)
		}
		b.ids = bound(ids, b.reason).ids
	}
	return b
}

// targetSymbol maps an attributed target to its symbol: a language
// intrinsic, a source declaration (in this or another file, attributed or
// not), a compiler-derived member, a reactor output mapped back to source,
// or an external JDK/JAR entity. nil means no supported origin.
func (c *fileContext) targetSymbol(e event) (*semantic.Symbol, error) {
	if e.Owner == "Array" && e.TargetPath == "" && e.ArtifactURI == "" {
		var kind ir.DeclarationKind
		var rule string
		if e.ElementKind == "FIELD" && e.Name == "length" {
			kind, rule = ir.DeclarationField, "array-length-field"
		}
		if e.Status == "array_constructor" && e.TreeKind == "MEMBER_REFERENCE" && e.ElementKind == "CONSTRUCTOR" && e.Name == "<init>" {
			kind, rule = ir.DeclarationConstructor, "array-allocation-constructor-reference"
		}
		if e.Status == "array_clone" && e.ElementKind == "METHOD" && e.Name == "clone" && e.Signature == "clone()" {
			kind, rule = ir.DeclarationMethod, "array-clone-method"
		}
		if rule != "" {
			sym, err := c.s.arrayMember(e, kind, rule)
			if err != nil {
				return nil, err
			}
			return &sym, nil
		}
	}
	if e.Status == "intrinsic" {
		sym := intrinsic(e.Type)
		if _, err := c.s.symbols.addUnique(sym); err != nil {
			return nil, err
		}
		return &sym, nil
	}
	if e.TargetPath != "" {
		if e.Generated != "" {
			// Its span is the field or annotation it came from, never a
			// declaration of its own.
			return c.s.generatedSymbol(e)
		}
		target, err := c.s.views.byBridgePath(e.TargetPath)
		if err != nil {
			return nil, err
		}
		if e.ElementKind == "PARAMETER" && e.TargetStart >= 0 && e.TargetEnd < 0 {
			return c.s.compactRecordParameter(target, c.fe, e)
		}
		kind, ok := elementKind(e)
		if !ok {
			return nil, nil
		}
		d, found := target.match(kind, e.Name, e.TargetStart, e.TargetEnd)
		if !found {
			if e.ElementKind == "CONSTRUCTOR" || e.ElementKind == "METHOD" || e.ElementKind == "FIELD" {
				return c.s.derivedSymbol(target, e)
			}
			return nil, nil
		}
		return c.s.sourceTarget(target, d)
	}
	if e.ArtifactURI != "" {
		if set, ok := c.s.sourceOutputSet(e.ArtifactURI); ok {
			return c.sourceOutputSymbol(set, e)
		}
		return c.s.externalSymbol(e)
	}
	// Package and module identifiers in qualified names have no declaration.
	return nil, nil
}

// sourceTarget returns the symbol of a source declaration. Files of
// attributed contexts write their own symbols; a declaration in a context
// that is not attributed this run is written here, once.
func (s *run) sourceTarget(v *fileView, d *declSummary) (*semantic.Symbol, error) {
	sym := v.sourceSymbol(d)
	if _, attributed := s.attributed[v.set]; !attributed {
		if _, err := s.symbols.addUnique(sym); err != nil {
			return nil, err
		}
	}
	return &sym, nil
}

// A reactor class-directory origin has explicit source-set provenance.
// Binary signatures are joined only inside that set, never repository-wide.
func (c *fileContext) sourceOutputSymbol(set bc.SourceSet, e event) (*semantic.Symbol, error) {
	entries, err := c.s.keys.lookup(set.ID, e.Owner, e.ElementKind, e.Name, e.Signature)
	if err != nil {
		return nil, err
	}
	if len(entries) > 1 {
		return nil, nil
	}
	if len(entries) == 1 {
		v, err := c.s.views.get(set.ID, entries[0].path)
		if err != nil {
			return nil, err
		}
		d, ok := v.decl(entries[0].decl)
		if !ok {
			return nil, nil
		}
		return c.s.sourceTarget(v, d)
	}
	// A compiler-established member absent from explicit syntax may be derived
	// from its exact attributed source owner (constructors/enum/record members).
	if e.ElementKind == "CONSTRUCTOR" || e.ElementKind == "METHOD" || e.ElementKind == "FIELD" {
		owners, err := c.s.keys.lookupType(set.ID, e.Owner)
		if err != nil {
			return nil, err
		}
		if len(owners) > 0 {
			v, err := c.s.views.get(set.ID, owners[0].path)
			if err != nil {
				return nil, err
			}
			d, ok := v.decl(owners[0].decl)
			if !ok {
				return nil, nil
			}
			e.OwnerSourcePath = v.bridgePath
			e.OwnerSourceStart = int64(d.Span.Start.ByteOffset)
			e.OwnerSourceEnd = int64(d.Span.End.ByteOffset)
			// javac's own constructor of a class takes no parameters (a
			// record's is its canonical one); in a Lombok set, one that takes
			// some was generated. A no-argument one stays ambiguous.
			lombokConstructor := e.ElementKind == "CONSTRUCTOR" && !strings.HasSuffix(e.Signature, "()") && d.Kind != ir.DeclarationRecord && d.Kind != ir.DeclarationEnum && c.s.usesLombok(set.ID)
			if !lombokConstructor {
				if sym, err := c.s.derivedSymbol(v, e); sym != nil || err != nil {
					return sym, err
				}
			}
		}
	}
	if !c.s.usesLombok(set.ID) {
		return nil, nil
	}
	// Anything else the producing set's bytecode has and its sources lack
	// was generated by its Lombok: it belongs to the nearest enclosing type
	// written in the sources.
	for name := e.Owner; name != ""; {
		owners, err := c.s.keys.lookupType(set.ID, name)
		if err != nil {
			return nil, err
		}
		if len(owners) == 1 {
			v, err := c.s.views.get(set.ID, owners[0].path)
			if err != nil {
				return nil, err
			}
			d, ok := v.decl(owners[0].decl)
			if !ok {
				return nil, nil
			}
			return c.s.lombokSymbol(v, d, name, e)
		}
		i := strings.LastIndexByte(name, '.')
		if i < 0 {
			break
		}
		name = name[:i]
	}
	return nil, nil
}

func (c *fileContext) typeBinding(tID ir.TypeRefID) (out binding) {
	if b, ok := c.typeBindings[tID]; ok {
		return b
	}
	key := "compiler-type:" + string(tID)
	if c.visiting[key] {
		return fail(semantic.LookupUnsupported, "analysis_limitation: recursive constructed-type mapping")
	}
	c.visiting[key] = true
	defer func() {
		delete(c.visiting, key)
		if out.err == nil {
			c.typeBindings[tID] = out
		}
	}()
	t, ok := c.v.types[tID]
	if !ok {
		return fail(semantic.LookupUnsupported, "analysis_limitation: type syntax absent")
	}
	e, found := c.fe.at(t.Span, "type")
	if t.Kind == ir.TypeInferred {
		for i := range c.v.decls {
			d := &c.v.decls[i]
			if d.Variable == nil || d.Variable.DeclaredTypeID != tID {
				continue
			}
			if declaration, ok := c.fe.declarationFor(d); ok && declaration.InferredType != nil {
				if diagnostic := c.fe.diagnosticAt(d.Span); diagnostic != "" {
					return fail(semantic.LookupUnresolved, diagnostic)
				}
				return c.inferredTypeBinding(*declaration.InferredType, 0)
			}
		}
	}
	if !found && t.Kind == ir.TypeArray {
		for _, expression := range c.v.file.Expressions {
			if expression.Kind == ir.ExpressionArrayCreation && expression.TypeRefID == tID {
				e, found = c.fe.at(expression.Span, "type")
				break
			}
		}
	}
	if !found && t.Kind == ir.TypeArray {
		e, found = c.varargsArrayEvent(t)
	}
	if !found && t.Kind == ir.TypeArray {
		// C-style post-name dimensions have a syntax span covering only the
		// brackets. Javac's ArrayType spans the actual component type through
		// those brackets, including the intervening declaration name.
		if element, ok := c.v.types[t.ElementTypeID]; ok && element.Span.Start.ByteOffset < t.Span.Start.ByteOffset {
			span := t.Span
			span.Start = element.Span.Start
			e, found = c.fe.at(span, "type")
		}
	}
	if !found && c.fe.failure != "" {
		return fail(semantic.LookupUnsupported, c.fe.failure)
	}
	if !found {
		return fail(semantic.LookupUnsupported, "analysis_limitation: compiler type position does not match syntax")
	}
	if diagnostic := c.fe.failureAt(t.Span, e.Status); diagnostic != "" {
		return fail(semantic.LookupUnresolved, diagnostic)
	}
	if e.Status == "unresolved" {
		return fail(semantic.LookupUnresolved, "compiler_unresolved_type: no attributed type under the pinned input environment")
	}
	if e.Status != "structural_type" {
		sym, err := c.targetSymbol(e)
		if err != nil {
			return binding{err: err}
		}
		if sym == nil {
			return fail(semantic.LookupUnsupported, "analysis_limitation: compiler type entity has no mapped origin")
		}
		b := selectedBinding(*sym, "javac_attributed_type")
		b.typ = e.Type
		return b
	}
	var components []ir.TypeRefID
	switch t.Kind {
	case ir.TypeArray:
		components = []ir.TypeRefID{t.ElementTypeID}
	case ir.TypeWildcard:
		for _, bnd := range t.Bounds {
			components = append(components, bnd.TypeRefID)
		}
	case ir.TypeUnion, ir.TypeIntersection:
		components = t.MemberTypeIDs
	default:
		return fail(semantic.LookupUnsupported, "analysis_limitation: compiler structural type has no matching syntax components")
	}
	var ids []string
	for _, component := range components {
		b := c.typeBinding(component)
		if b.err != nil {
			return b
		}
		if b.status != semantic.LookupResolved || len(b.ids) != 1 {
			return fail(semantic.LookupUnresolved, "compiler_unresolved_type_component: constructed type constituent has no unique binding")
		}
		ids = append(ids, b.ids[0])
	}
	return c.constructedTypeBinding(t.Kind, e.Type, ids)
}

func (c *fileContext) constructedTypeBinding(kind ir.TypeKind, canonical string, ids []string) binding {
	sym, err := c.s.constructedSymbol(kind, canonical, ids)
	if err != nil {
		return binding{err: err}
	}
	b := selectedBinding(sym, "javac_attributed_constructed_type")
	b.typ = canonical
	return b
}

// Javac gives every array layer in a varargs parameter the same end position,
// including the ellipsis. The syntax type excludes that extra parameter-array
// layer. Join inside the exact written parameter and use javac's array rank to
// select its component type, without interpreting the canonical type string.
func (c *fileContext) varargsArrayEvent(t ir.TypeRef) (event, bool) {
	for i := range c.v.decls {
		d := &c.v.decls[i]
		if d.Variable == nil || !d.Variable.Variadic || d.Variable.DeclaredTypeID != t.ID || d.NameSpan == nil {
			continue
		}
		var selected event
		found := false
		for _, e := range c.fe.bindingsByStart[int64(t.Span.Start.ByteOffset)] {
			if e.TreeKind != "ARRAY_TYPE" || e.End < int64(t.Span.End.ByteOffset) || e.End > int64(d.NameSpan.Start.ByteOffset) || e.ArrayDimensions != len(t.Dimensions) {
				continue
			}
			if found && (selected.Type != e.Type || selected.Status != e.Status) {
				return event{}, false
			}
			selected, found = e, true
		}
		return selected, found
	}
	return event{}, false
}

// Inferred types have no written component spans. Their provenance comes from
// the attributed variable declaration, including compiler-supplied array and
// intersection constituents. Never interpret the printed type to find a target.
func (c *fileContext) inferredTypeBinding(e event, depth int) binding {
	if err := c.s.ctx.Err(); err != nil {
		return binding{err: err}
	}
	if depth > 64 {
		return fail(semantic.LookupUnsupported, "analysis_limitation: inferred type nesting exceeds bound")
	}
	if e.Status == "unresolved" {
		return fail(semantic.LookupUnresolved, "compiler_unresolved_type: inferred declaration has no valid attributed type")
	}
	if e.Status == "resolved" || e.Status == "intrinsic" {
		sym, err := c.targetSymbol(e)
		if err != nil {
			return binding{err: err}
		}
		if sym == nil {
			return fail(semantic.LookupUnsupported, "analysis_limitation: inferred compiler type has no mapped origin")
		}
		b := selectedBinding(*sym, "javac_attributed_inferred_type")
		b.typ = e.Type
		return b
	}
	var kind ir.TypeKind
	switch e.TypeKind {
	case "ARRAY":
		kind = ir.TypeArray
	case "INTERSECTION":
		kind = ir.TypeIntersection
	}
	if e.Status != "structural_type" || kind == "" || len(e.TypeComponents) == 0 {
		return fail(semantic.LookupUnsupported, "analysis_limitation: inferred type algebra is not supported")
	}
	var ids []string
	for _, component := range e.TypeComponents {
		b := c.inferredTypeBinding(component, depth+1)
		if b.err != nil || b.status != semantic.LookupResolved {
			return b
		}
		if len(b.ids) != 1 {
			return fail(semantic.LookupUnsupported, "analysis_limitation: inferred type constituent has no unique binding")
		}
		ids = append(ids, b.ids[0])
	}
	return c.constructedTypeBinding(kind, e.Type, ids)
}

// overrides binds each method declaration to every method javac says it
// overrides. The site is the declaration; evidence is its name.
func (c *fileContext) overrides() error {
	for _, e := range c.fe.overrides {
		d, ok := c.v.match(ir.DeclarationMethod, e.Name, e.Start, e.End)
		if !ok {
			continue
		}
		var b binding
		sym, err := c.targetSymbol(e)
		if err != nil {
			return err
		}
		if sym == nil {
			b = fail(semantic.LookupUnsupported, "analysis_limitation: overridden method has no supported graph symbol origin")
		} else {
			b = selectedBinding(*sym, "javac_overrides: exact overridden declaration")
		}
		span := d.Span
		if d.NameSpan != nil {
			span = *d.NameSpan
		}
		l, err := c.lookup(graph.ID("lookup", string(c.v.fileID()), "override", string(d.ID), e.Owner, e.Signature), semantic.LookupOverride, b, c.v.anchor(span))
		if err != nil {
			return err
		}
		l.DeclarationID = d.ID
		c.lookups = append(c.lookups, l)
	}
	return nil
}

// implements binds a lambda or method reference site to the single abstract
// method of its attributed functional interface, and the site to that
// interface as a type use.
func (c *fileContext) implements() error {
	for _, e := range c.fe.implements {
		occ, ok := c.site(e)
		if !ok {
			continue
		}
		// The functional interface comes from the lambda's context, so an
		// error in its body does not undo javac's choice of it; one where
		// javac could not choose shows in its status.
		var b binding
		if diagnostic := c.fe.diagnosticAt(occ.Span); diagnostic != "" && e.Status != "resolved" {
			b = fail(semantic.LookupUnresolved, diagnostic)
		} else {
			switch e.Status {
			case "resolved":
				sym, err := c.targetSymbol(e)
				if err != nil {
					return err
				}
				if sym == nil {
					b = fail(semantic.LookupUnsupported, "analysis_limitation: functional interface method has no supported graph symbol origin")
				} else {
					b = selectedBinding(*sym, "javac_functional_interface: single abstract method of the attributed target type")
				}
			case "unsupported":
				b = fail(semantic.LookupUnsupported, "analysis_limitation: "+e.Message)
			default:
				b = fail(semantic.LookupUnresolved, "compiler_unresolved: "+e.Message)
			}
		}
		l, err := c.lookup(graph.ID("lookup", string(c.v.fileID()), string(occ.ID), "implements"), semantic.LookupImplements, b, c.v.anchor(occ.Span))
		if err != nil {
			return err
		}
		l.OccurrenceID = occ.ID
		c.lookups = append(c.lookups, l)
		if e.Interface == nil || c.emitted[string(occ.ID)+"\x00"+string(semantic.LookupType)] {
			continue
		}
		var tb binding
		if diagnostic := c.fe.diagnosticAt(occ.Span); diagnostic != "" && e.Status != "resolved" {
			tb = fail(semantic.LookupUnresolved, diagnostic)
		} else if sym, err := c.targetSymbol(*e.Interface); err != nil {
			return err
		} else if sym == nil {
			tb = fail(semantic.LookupUnsupported, "analysis_limitation: functional interface has no supported graph symbol origin")
		} else {
			tb = selectedBinding(*sym, "javac_attributed_type")
		}
		t, err := c.lookup(graph.ID("lookup", string(c.v.fileID()), string(occ.ID), "type"), semantic.LookupType, tb, c.v.anchor(occ.Span))
		if err != nil {
			return err
		}
		t.OccurrenceID = occ.ID
		c.emitted[string(occ.ID)+"\x00"+string(semantic.LookupType)] = true
		c.lookups = append(c.lookups, t)
	}
	return nil
}

// site finds the lambda or callable-reference occurrence written at exactly
// the span javac attributed.
func (c *fileContext) site(e event) (ir.Occurrence, bool) {
	matches := func(o ir.Occurrence) bool {
		return int64(o.Span.Start.ByteOffset) == e.Start && int64(o.Span.End.ByteOffset) == e.End
	}
	if e.TreeKind == "LAMBDA_EXPRESSION" {
		for _, l := range c.v.file.Lambdas {
			if matches(l.Occurrence) {
				return l.Occurrence, true
			}
		}
		return ir.Occurrence{}, false
	}
	for _, r := range c.v.file.CallableReferences {
		if matches(r.Occurrence) {
			return r.Occurrence, true
		}
	}
	return ir.Occurrence{}, false
}
