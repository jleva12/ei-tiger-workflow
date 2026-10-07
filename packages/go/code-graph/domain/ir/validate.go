package ir

import (
	"errors"
	"fmt"
	"sort"
	"strings"
)

// ValidationError identifies a structural problem in the serialized contract.
// Validation does not resolve Java names or certify extraction completeness.
type ValidationError struct {
	Path    string
	Message string
}

func (e ValidationError) Error() string { return e.Path + ": " + e.Message }

type validator struct {
	file *SourceFile
	ids  map[string]map[string]bool
	errs []error
}

func (v *validator) problem(p, message string) {
	v.errs = append(v.errs, ValidationError{Path: p, Message: message})
}
func (v *validator) required(p, value string) {
	if strings.TrimSpace(value) == "" {
		v.problem(p, "must not be empty")
	}
}
func (v *validator) choice(p, value string, choices ...string) {
	for _, c := range choices {
		if value == c {
			return
		}
	}
	v.problem(p, fmt.Sprintf("unsupported value %q", value))
}
func (v *validator) span(p string, s Span) {
	if s.Start.Line == 0 || s.End.Line == 0 {
		v.problem(p, "line numbers must be one-based")
	}
	if s.Start.ByteOffset > s.End.ByteOffset || s.End.ByteOffset > v.file.Source.SizeBytes {
		v.problem(p, "byte range is reversed or outside the source")
	}
	if s.Start.Line > s.End.Line || (s.Start.Line == s.End.Line && s.Start.Column > s.End.Column) {
		v.problem(p, "source positions are reversed")
	}
	if s.Start.Line == s.End.Line && s.End.ByteOffset-s.Start.ByteOffset != uint64(s.End.Column)-uint64(s.Start.Column) {
		v.problem(p, "same-line columns must match the byte-offset distance")
	}
}
func (v *validator) optionalSpan(p string, s *Span) {
	if s != nil {
		v.span(p, *s)
	}
}
func (v *validator) register(table, id, p string) {
	if id == "" {
		v.problem(p, "ID must not be empty")
		return
	}
	if v.ids[table] == nil {
		v.ids[table] = map[string]bool{}
	}
	if v.ids[table][id] {
		v.problem(p, "duplicate "+table+" ID "+id)
	}
	v.ids[table][id] = true
}
func (v *validator) ref(table, id, p string, required bool) {
	if id == "" && !required {
		return
	}
	if id == "" || !v.ids[table][id] {
		v.problem(p, "missing "+table+" ID "+id)
	}
}
func refs[T ~string](v *validator, table, p string, values []T) {
	for i, id := range values {
		v.ref(table, string(id), fmt.Sprintf("%s[%d]", p, i), true)
	}
}
func (v *validator) occurrence(p string, o Occurrence) {
	v.span(p+".span", o.Span)
	v.ref("scope", string(o.ScopeID), p+".scope_id", true)
	v.ref("declaration", string(o.EnclosingDeclarationID), p+".enclosing_declaration_id", false)
}
func (v *validator) name(p string, n Name) {
	if len(n.Segments) == 0 {
		v.problem(p, "name must contain a segment")
	}
	for i, s := range n.Segments {
		p := fmt.Sprintf("%s.segments[%d]", p, i)
		v.required(p+".text", s.Text)
		v.optionalSpan(p+".span", s.Span)
	}
}

// Validate checks version/source identity, local IDs and links, spans, ownership
// cycles, and the main tagged-union contracts. It does not read source bytes,
// check Java access/visibility, bind symbols, or act as a hostile-input decoder.
func (f *SourceFile) Validate() error {
	if f == nil {
		return ValidationError{Path: "file", Message: "must not be nil"}
	}
	v := &validator{file: f, ids: map[string]map[string]bool{}}
	v.choice("schema_version", f.SchemaVersion, SchemaVersion)
	if err := f.Source.Validate(); err != nil {
		v.errs = append(v.errs, err)
	}
	v.required("producer.name", f.Producer.Name)
	v.required("producer.version", f.Producer.Version)
	v.required("coverage.feature_set", f.Coverage.FeatureSet)
	v.choice("coverage.status", string(f.Coverage.Status), "complete", "partial", "failed")
	if f.Coverage.Status == ExtractionComplete && len(f.Coverage.Issues) > 0 {
		v.problem("coverage.status", "complete extraction cannot contain coverage losses")
	}
	if f.Coverage.Status != ExtractionComplete && len(f.Coverage.Issues) == 0 {
		v.problem("coverage.issues", "incomplete extraction requires a coverage issue")
	}

	// Register everything before checking forward references.
	for i, s := range f.Scopes {
		v.register("scope", string(s.ID), fmt.Sprintf("scopes[%d].id", i))
	}
	for i, d := range f.Declarations {
		v.register("declaration", string(d.ID), fmt.Sprintf("declarations[%d].id", i))
	}
	for i, t := range f.Types {
		v.register("type", string(t.ID), fmt.Sprintf("types[%d].id", i))
	}
	for i, e := range f.Expressions {
		v.register("expression", string(e.ID), fmt.Sprintf("expressions[%d].id", i))
	}
	registerOccurrence := func(p string, o Occurrence) { v.register("occurrence", string(o.ID), p+".occurrence.id") }
	for i, x := range f.Imports {
		registerOccurrence(fmt.Sprintf("imports[%d]", i), x.Occurrence)
	}
	for i, x := range f.Calls {
		registerOccurrence(fmt.Sprintf("calls[%d]", i), x.Occurrence)
	}
	for i, x := range f.CallableReferences {
		registerOccurrence(fmt.Sprintf("callable_references[%d]", i), x.Occurrence)
	}
	for i, x := range f.Lambdas {
		registerOccurrence(fmt.Sprintf("lambdas[%d]", i), x.Occurrence)
	}
	for i, x := range f.References {
		registerOccurrence(fmt.Sprintf("references[%d]", i), x.Occurrence)
	}
	for i, x := range f.TypeUses {
		registerOccurrence(fmt.Sprintf("type_uses[%d]", i), x.Occurrence)
	}
	for i, x := range f.Annotations {
		registerOccurrence(fmt.Sprintf("annotations[%d]", i), x.Occurrence)
		v.register("annotation", string(x.ID), fmt.Sprintf("annotations[%d].id", i))
	}

	v.registerSyntax()

	v.ref("scope", string(f.RootScopeID), "root_scope_id", true)
	scopeParents, owners := map[string][]string{}, map[string][]string{}
	scopeByID := map[ScopeID]Scope{}
	for _, s := range f.Scopes {
		scopeByID[s.ID] = s
	}
	for i, s := range f.Scopes {
		p := fmt.Sprintf("scopes[%d]", i)
		v.choice(p+".kind", string(s.Kind), "file", "type", "callable", "block", "lambda", "initializer", "loop", "catch", "resource", "pattern", "comprehension", "annotation")
		v.span(p+".span", s.Span)
		v.ref("scope", string(s.ParentID), p+".parent_id", s.ID != f.RootScopeID)
		v.ref("declaration", string(s.OwnerDeclarationID), p+".owner_declaration_id", false)
		if s.ID == f.RootScopeID {
			if s.Kind != ScopeFile || s.ParentID != "" || s.OwnerDeclarationID != "" {
				v.problem(p, "root must be an unowned file scope without a parent")
			}
			if s.Span.Start.ByteOffset != 0 || s.Span.End.ByteOffset != f.Source.SizeBytes {
				v.problem(p+".span", "root scope must cover all source bytes")
			}
		} else if s.Kind == ScopeFile {
			v.problem(p+".kind", "only the root can be a file scope")
		}
		if parent, ok := scopeByID[s.ParentID]; ok && !containsSpan(parent.Span, s.Span) {
			v.problem(p+".span", "scope must be contained by its parent")
		}
		for j, r := range s.Regions {
			v.span(fmt.Sprintf("%s.regions[%d]", p, j), r)
			if !containsSpan(s.Span, r) {
				v.problem(p+".regions", "region outside scope")
			}
		}
		if s.ParentID != "" {
			scopeParents[string(s.ID)] = []string{string(s.ParentID)}
		}
	}
	for i, d := range f.Declarations {
		p := fmt.Sprintf("declarations[%d]", i)
		v.span(p+".span", d.Span)
		v.optionalSpan(p+".name_span", d.NameSpan)
		v.ref("scope", string(d.DeclaringScopeID), p+".declaring_scope_id", true)
		v.ref("scope", string(d.BodyScopeID), p+".body_scope_id", false)
		v.ref("declaration", string(d.OwnerID), p+".owner_id", false)
		if d.OwnerID != "" {
			owners[string(d.ID)] = []string{string(d.OwnerID)}
		}
		refs(v, "annotation", p+".annotation_ids", d.AnnotationIDs)
		refs(v, "expression", p+".decorator_expression_ids", d.DecoratorExpressionIDs)
		for j, m := range d.Modifiers {
			v.required(p+".modifiers.keyword", m.Keyword)
			v.span(fmt.Sprintf("%s.modifiers[%d].span", p, j), m.Span)
		}
		v.declaration(p, d)
	}
	v.cycles("scopes.parent_id", scopeParents)
	v.cycles("declarations.owner_id", owners)
	if f.Package != nil {
		v.name("package.name", f.Package.Name)
		v.span("package.span", f.Package.Span)
		refs(v, "annotation", "package.annotation_ids", f.Package.AnnotationIDs)
	}
	for i, x := range f.Imports {
		p := fmt.Sprintf("imports[%d]", i)
		v.occurrence(p+".occurrence", x.Occurrence)
		v.choice(p+".kind", string(x.Kind), "single_type", "type_on_demand", "single_static", "static_on_demand", "module", "re_export")
		v.name(p+".name", x.Name)
		v.required(p+".spelling", x.Spelling)
	}
	for i, x := range f.Types {
		v.typeRef(fmt.Sprintf("types[%d]", i), x)
	}
	for i, x := range f.Expressions {
		v.expression(fmt.Sprintf("expressions[%d]", i), x)
	}
	for i, x := range f.Calls {
		v.call(fmt.Sprintf("calls[%d]", i), x)
	}
	for i, x := range f.CallableReferences {
		p := fmt.Sprintf("callable_references[%d]", i)
		v.occurrence(p+".occurrence", x.Occurrence)
		v.choice(p+".kind", string(x.Kind), "method", "constructor")
		v.ref("expression", string(x.ExpressionID), p+".expression_id", true)
		v.ref("expression", string(x.QualifierID), p+".qualifier_id", true)
		v.required(p+".name", x.Name)
		refs(v, "type", p+".type_argument_ids", x.TypeArgumentIDs)
		if x.Kind == CallableConstructorReference && x.Name != "new" {
			v.problem(p+".name", "constructor reference must retain new")
		}
	}
	declarations := make(map[DeclarationID]Declaration, len(f.Declarations))
	for _, d := range f.Declarations {
		declarations[d.ID] = d
	}
	for i, x := range f.Lambdas {
		v.lambdaSite(fmt.Sprintf("lambdas[%d]", i), x, declarations)
	}
	for i, x := range f.References {
		p := fmt.Sprintf("references[%d]", i)
		v.occurrence(p+".occurrence", x.Occurrence)
		v.choice(p+".kind", string(x.Kind), "name", "member")
		v.choice(p+".access", string(x.Access), "read", "write", "read_write", "unknown")
		v.ref("expression", string(x.ExpressionID), p+".expression_id", true)
		v.ref("expression", string(x.ReceiverID), p+".receiver_id", x.Kind == ReferenceMember)
		v.name(p+".name", x.Name)
	}
	for i, x := range f.TypeUses {
		p := fmt.Sprintf("type_uses[%d]", i)
		v.occurrence(p+".occurrence", x.Occurrence)
		v.choice(p+".role", string(x.Role), "field", "parameter", "return", "local", "generic_argument", "bound", "heritage", "permitted_type", "throws", "catch", "cast", "construction", "callable_reference", "class_literal", "annotation", "record_component", "pattern", "module_service", "module_provider", "alias", "component")
		v.ref("type", string(x.TypeRefID), p+".type_ref_id", true)
		v.ref("type", string(x.ParentTypeID), p+".parent_type_id", false)
	}
	for i, x := range f.Annotations {
		p := fmt.Sprintf("annotations[%d]", i)
		v.occurrence(p+".occurrence", x.Occurrence)
		v.ref("type", string(x.TypeRefID), p+".type_ref_id", true)
		for j, a := range x.Arguments {
			p := fmt.Sprintf("%s.arguments[%d]", p, j)
			v.optionalSpan(p+".name_span", a.NameSpan)
			v.annotationValue(p+".value", a.Value, 0)
		}
	}
	for i, d := range f.Diagnostics {
		p := fmt.Sprintf("diagnostics[%d]", i)
		v.required(p+".code", d.Code)
		v.required(p+".message", d.Message)
		v.optionalSpan(p+".span", d.Span)
		v.choice(p+".severity", string(d.Severity), "info", "warning", "error")
		if d.Severity == SeverityError && f.Coverage.Status == ExtractionComplete {
			v.problem("coverage.status", "extraction errors must be reflected in coverage")
		}
	}
	for i, c := range f.Coverage.Issues {
		p := fmt.Sprintf("coverage.issues[%d]", i)
		v.required(p+".feature", c.Feature)
		v.required(p+".reason", string(c.Reason))
		v.choice(p+".reason", string(c.Reason), "unsupported", "parse_error", "resource_limit", "skipped")
		v.required(p+".message", c.Message)
		v.optionalSpan(p+".span", c.Span)
	}
	v.expressionLinks()
	v.validateSyntax()
	return errors.Join(v.errs...)
}

func containsSpan(outer, inner Span) bool {
	return outer.Start.ByteOffset <= inner.Start.ByteOffset && inner.End.ByteOffset <= outer.End.ByteOffset
}

// Iterative DFS avoids call-stack growth with deeply nested scopes/expressions.
func (v *validator) cycles(p string, adj map[string][]string) {
	color := map[string]uint8{}
	type frame struct {
		id   string
		next int
	}
	roots := make([]string, 0, len(adj))
	for root := range adj {
		roots = append(roots, root)
	}
	sort.Strings(roots)
	for _, root := range roots {
		if color[root] != 0 {
			continue
		}
		stack := []frame{{id: root}}
		color[root] = 1
		for len(stack) > 0 {
			top := &stack[len(stack)-1]
			if top.next >= len(adj[top.id]) {
				color[top.id] = 2
				stack = stack[:len(stack)-1]
				continue
			}
			next := adj[top.id][top.next]
			top.next++
			if color[next] == 1 {
				v.problem(p, "cycle through ID "+next)
				continue
			}
			if color[next] == 0 {
				color[next] = 1
				stack = append(stack, frame{id: next})
			}
		}
	}
}
