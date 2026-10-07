package java

import (
	"fmt"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// declSummary is what target mapping needs from a declaration, available
// both from parsed IR (affected files) and from a previous generation's
// identity map (unchanged files).
type declSummary struct {
	ID        ir.DeclarationID
	Kind      ir.DeclarationKind
	Name      string
	Span      ir.Span
	NameSpan  *ir.Span
	OwnerID   ir.DeclarationID
	Form      ir.TypeForm // type declarations only; "" when unknown
	Qualified string      // qualified type name when provable
	Callable  *ir.CallableDeclaration
	Variable  *ir.VariableDeclaration
	Type      *ir.TypeDeclaration
}

func (d *declSummary) isType() bool {
	switch d.Kind {
	case ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationRecord, ir.DeclarationAnnotationType:
		return true
	}
	return false
}

// fileView is one file of an attributed or referenced context.
type fileView struct {
	set        bc.SourceSetID
	input      semantic.SourceInput
	bridgePath string
	decls      []declSummary
	byID       map[ir.DeclarationID]int
	file       *ir.SourceFile // nil for identity-backed views
	types      map[ir.TypeRefID]ir.TypeRef
	pkg        string
}

func (v *fileView) fileID() ir.FileID { return v.input.Source.FileID }

func (v *fileView) anchor(span ir.Span) graph.SourceAnchor {
	return graph.SourceAnchor{Lineage: v.input.Lineage, ContentSHA256: v.input.Source.ContentSHA256, Span: span}
}

func (v *fileView) decl(id ir.DeclarationID) (*declSummary, bool) {
	i, ok := v.byID[id]
	if !ok {
		return nil, false
	}
	return &v.decls[i], true
}

func (v *fileView) sourceSymbol(d *declSummary) semantic.Symbol {
	sym := semantic.Symbol{ID: symbolID(v.fileID(), d.ID), Name: d.Name, Source: &semantic.SourceSymbol{FileID: v.fileID(), DeclarationID: d.ID, Evidence: v.anchor(d.Span)}}
	if d.OwnerID != "" {
		sym.OwnerSymbolID = symbolID(v.fileID(), d.OwnerID)
	}
	return sym
}

// match finds the declaration javac points at: the same kind of element with
// the same name whose name span lies inside the reported target span. When a
// same-named declaration nests inside the target (a local class member), the
// outermost candidate is the target itself.
func (v *fileView) match(kind ir.DeclarationKind, name string, start, end int64) (*declSummary, bool) {
	if start < 0 || end < start {
		return nil, false
	}
	var best *declSummary
	for i := range v.decls {
		d := &v.decls[i]
		if !summaryMatches(d, kind, name) {
			continue
		}
		span := d.Span
		if d.NameSpan != nil {
			span = *d.NameSpan
		}
		if uint64(start) <= span.Start.ByteOffset && uint64(end) >= span.End.ByteOffset {
			if best == nil || d.Span.Start.ByteOffset < best.Span.Start.ByteOffset {
				best = d
			}
		}
	}
	if best != nil || v.file != nil {
		return best, best != nil
	}
	// Identity maps carry no name spans. Take the innermost declaration
	// containing the target span when it describes the same element, else
	// the closest overlapping declaration of that kind and name.
	var closest *declSummary
	var distance uint64
	for i := range v.decls {
		d := &v.decls[i]
		if !summaryMatches(d, kind, name) || d.Span.Start.ByteOffset >= uint64(end) || d.Span.End.ByteOffset <= uint64(start) {
			continue
		}
		dist := absDiff(d.Span.Start.ByteOffset, uint64(start)) + absDiff(d.Span.End.ByteOffset, uint64(end))
		if closest == nil || dist < distance {
			closest, distance = d, dist
		}
	}
	return closest, closest != nil
}

func summaryMatches(d *declSummary, kind ir.DeclarationKind, name string) bool {
	if kind == ir.DeclarationConstructor {
		return d.Kind == ir.DeclarationConstructor
	}
	if d.Name != name {
		return false
	}
	switch kind {
	case ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationRecord, ir.DeclarationAnnotationType:
		return d.isType()
	}
	return d.Kind == kind
}

func absDiff(a, b uint64) uint64 {
	if a > b {
		return a - b
	}
	return b - a
}

// elementKind converts a javac element kind into the IR declaration kind
// used by target matching.
func elementKind(e event) (ir.DeclarationKind, bool) {
	switch e.ElementKind {
	case "CLASS", "INTERFACE", "ENUM", "RECORD", "ANNOTATION_TYPE":
		return ir.DeclarationClass, true
	case "METHOD":
		return ir.DeclarationMethod, true
	case "CONSTRUCTOR":
		return ir.DeclarationConstructor, true
	case "FIELD":
		return ir.DeclarationField, true
	case "PARAMETER":
		return ir.DeclarationParameter, true
	case "TYPE_PARAMETER":
		return ir.DeclarationTypeParameter, true
	case "ENUM_CONSTANT":
		return ir.DeclarationEnumConstant, true
	case "LOCAL_VARIABLE", "EXCEPTION_PARAMETER", "RESOURCE_VARIABLE":
		return ir.DeclarationLocal, true
	case "BINDING_VARIABLE":
		return ir.DeclarationPatternVariable, true
	case "RECORD_COMPONENT":
		return ir.DeclarationRecordComponent, true
	}
	return "", false
}

func viewFromIR(set bc.SourceSetID, in semantic.SourceInput, f ir.SourceFile) *fileView {
	v := &fileView{set: set, input: in, bridgePath: bridgePath(setDirectory(string(set)), in.Source.Path), byID: map[ir.DeclarationID]int{}, types: map[ir.TypeRefID]ir.TypeRef{}}
	file := f
	v.file = &file
	if f.Package != nil {
		v.pkg = name(f.Package.Name)
	}
	for _, t := range f.Types {
		v.types[t.ID] = t
	}
	v.decls = make([]declSummary, len(f.Declarations))
	for i, d := range f.Declarations {
		v.decls[i] = declSummary{ID: d.ID, Kind: d.Kind, Name: d.Name, Span: d.Span, NameSpan: d.NameSpan, OwnerID: d.OwnerID, Callable: d.Callable, Variable: d.Variable, Type: d.Type}
		if d.Type != nil {
			v.decls[i].Form = d.Type.Form
		}
		v.byID[d.ID] = i
	}
	for i := range v.decls {
		if q, ok := v.qualified(&v.decls[i], 0); ok {
			v.decls[i].Qualified = q
		}
	}
	return v
}

func viewFromIdentities(set bc.SourceSetID, in semantic.SourceInput, ids semantic.FileIdentities) *fileView {
	v := &fileView{set: set, input: in, bridgePath: bridgePath(setDirectory(string(set)), in.Source.Path), byID: map[ir.DeclarationID]int{}}
	v.decls = make([]declSummary, len(ids.Declarations))
	for i, d := range ids.Declarations {
		v.decls[i] = declSummary{ID: d.DeclarationID, Kind: d.Kind, Name: d.Name, Span: d.Span, OwnerID: d.OwnerID}
		if v.decls[i].isType() {
			if d.Name == "" {
				v.decls[i].Form = ir.TypeAnonymous
			} else if d.Key != nil {
				v.decls[i].Qualified = d.Key.CanonicalSignature
			}
		}
		v.byID[d.DeclarationID] = i
	}
	return v
}

// qualified names a member or top-level type by its package and enclosing
// types; local and anonymous types have no portable qualified name.
func (v *fileView) qualified(d *declSummary, depth int) (string, bool) {
	if depth > 64 || !d.isType() || d.Name == "" || d.Form == ir.TypeAnonymous || d.Form == ir.TypeLocal {
		return "", false
	}
	if d.OwnerID != "" {
		owner, ok := v.decl(d.OwnerID)
		if !ok {
			return "", false
		}
		q, ok := v.qualified(owner, depth+1)
		return q + "." + d.Name, ok
	}
	if v.pkg == "" {
		return d.Name, true
	}
	return v.pkg + "." + d.Name, true
}

func name(n ir.Name) string {
	p := make([]string, len(n.Segments))
	for i, v := range n.Segments {
		p[i] = v.Text
	}
	return strings.Join(p, ".")
}

// viewCache keeps the views of the last few target files. The file being
// resolved is pinned for the duration of its processing.
type viewCache struct {
	s       *run
	limit   int
	entries map[string]*fileView
	order   []string
	pinned  *fileView
}

func newViewCache(s *run, limit int) *viewCache {
	return &viewCache{s: s, limit: limit, entries: map[string]*fileView{}}
}

func viewKey(set bc.SourceSetID, path string) string { return string(set) + "\x00" + path }

func (c *viewCache) pin(set bc.SourceSetID, in semantic.SourceInput) (*fileView, error) {
	v, err := c.build(set, in)
	if err != nil {
		return nil, err
	}
	c.pinned = v
	return v, nil
}

func (c *viewCache) unpin() { c.pinned = nil }

// byBridgePath resolves the file javac reported, sources/<set>/<path>.
func (c *viewCache) byBridgePath(path string) (*fileView, error) {
	if c.pinned != nil && c.pinned.bridgePath == path {
		return c.pinned, nil
	}
	rest, ok := strings.CutPrefix(path, "sources/")
	if !ok {
		return nil, fmt.Errorf("compiler source target %q is outside the materialized sources", path)
	}
	dir, rel, ok := strings.Cut(rest, "/")
	set, known := c.s.setDirs[dir]
	if !ok || !known {
		return nil, fmt.Errorf("compiler source target %q belongs to no attributed source set", path)
	}
	return c.get(set, rel)
}

func (c *viewCache) get(set bc.SourceSetID, path string) (*fileView, error) {
	if c.pinned != nil && c.pinned.set == set && c.pinned.input.Source.Path == path {
		return c.pinned, nil
	}
	key := viewKey(set, path)
	if v, ok := c.entries[key]; ok {
		c.touch(key)
		return v, nil
	}
	in, err := c.s.w.FileByPath(c.s.ctx, set, path)
	if err != nil {
		return nil, fmt.Errorf("compiler source target %s in %s is outside the inventory: %w", path, set, err)
	}
	v, err := c.build(set, in)
	if err != nil {
		return nil, err
	}
	c.entries[key] = v
	c.order = append(c.order, key)
	if len(c.order) > c.limit {
		delete(c.entries, c.order[0])
		c.order = c.order[1:]
	}
	return v, nil
}

func (c *viewCache) touch(key string) {
	for i, k := range c.order {
		if k == key {
			c.order = append(append(c.order[:i:i], c.order[i+1:]...), key)
			return
		}
	}
}

// build reads the syntax of an affected file, or the previous identities of
// an unchanged one; an unchanged file without a baseline falls back to
// syntax when the workspace has it and to an opaque view otherwise.
func (c *viewCache) build(set bc.SourceSetID, in semantic.SourceInput) (*fileView, error) {
	if in.Affected {
		f, err := c.s.w.Syntax(c.s.ctx, in)
		if err == nil {
			return viewFromIR(set, in, f), nil
		}
		if !errNotFound(err) {
			return nil, fmt.Errorf("syntax of %s: %w", in.Source.Path, err)
		}
		// The parse stage skipped it (beyond the parser's limits, or not
		// UTF-8): javac still compiles it, but it has no declarations to
		// offer and no sites to bind.
		return &fileView{set: set, input: in, bridgePath: bridgePath(setDirectory(string(set)), in.Source.Path), byID: map[ir.DeclarationID]int{}}, nil
	}
	ids, err := c.s.w.PreviousIdentities(c.s.ctx, in.Lineage)
	if err == nil {
		return viewFromIdentities(set, in, ids), nil
	}
	if !errNotFound(err) {
		return nil, fmt.Errorf("previous identities of %s: %w", in.Source.Path, err)
	}
	if f, err := c.s.w.Syntax(c.s.ctx, in); err == nil {
		return viewFromIR(set, in, f), nil
	} else if !errNotFound(err) {
		return nil, fmt.Errorf("syntax of %s: %w", in.Source.Path, err)
	}
	return &fileView{set: set, input: in, bridgePath: bridgePath(setDirectory(string(set)), in.Source.Path), byID: map[ir.DeclarationID]int{}}, nil
}
