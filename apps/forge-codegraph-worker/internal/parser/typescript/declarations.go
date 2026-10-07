package typescript

import (
	"strings"

	"ei-aitiger-codegraph/pkg/ir"

	sitter "github.com/tree-sitter/go-tree-sitter"
)

// program walks the module. Statements recurse into their children;
// declarations become records; expressions are extracted for calls,
// references, type uses and function literals.
func (b *builder) program(root *sitter.Node) {
	b.nodes(root, environment{scope: b.file.RootScopeID})
}

func (b *builder) nodes(n *sitter.Node, env environment) {
	for _, child := range named(n) {
		b.node(child, env)
	}
}

func (b *builder) node(n *sitter.Node, env environment) {
	b.check()
	switch n.Kind() {
	case "comment", "hash_bang_line":
	case "export_statement":
		b.export(n, env)
	case "import_statement":
		b.importStatement(n, env)
	case "class_declaration", "abstract_class_declaration":
		b.class(n, env, false)
	case "class":
		b.class(n, env, !valid(field(n, "name")))
	case "interface_declaration":
		b.interfaceDeclaration(n, env)
	case "enum_declaration":
		b.enumDeclaration(n, env)
	case "type_alias_declaration":
		b.typeAlias(n, env)
	case "internal_module", "module":
		b.namespace(n, env)
	case "ambient_declaration":
		b.nodes(n, env)
	case "function_declaration", "generator_function_declaration", "function_signature":
		b.function(n, env)
	case "lexical_declaration", "variable_declaration":
		b.variables(n, env)
	case "statement_block":
		b.block(n, env)
	case "return_statement":
		value := firstNamed(n)
		if !valid(value) || !isExpression(value.Kind()) {
			b.nodes(n, env)
			break
		}
		if id := b.expression(value, env, ir.AccessRead); id != "" && env.returns != nil {
			*env.returns = append(*env.returns, id)
		}
	case "method_definition", "method_signature", "abstract_method_signature":
		b.method(n, env)
	case "catch_clause":
		inner := env
		inner.scope = b.scope(ir.ScopeCatch, b.span(n), env)
		if parameter := field(n, "parameter"); valid(parameter) && parameter.Kind() == "identifier" {
			b.variable(parameter, parameter, field(n, "type"), nil, inner, ir.DeclarationLocal, ir.TypeUseCatch, "")
		}
		b.nodes(field(n, "body"), inner)
	case "for_in_statement":
		inner := env
		inner.scope = b.scope(ir.ScopeLoop, b.span(n), env)
		left := field(n, "left")
		rightID := b.expression(field(n, "right"), inner, ir.AccessRead)
		ofLoop := field(n, "operator") != nil && b.spelling(field(n, "operator")) == "of"
		element := func(ident *sitter.Node) ir.ExpressionID {
			if !ofLoop || rightID == "" {
				return ""
			}
			return b.saveSynthetic(ir.Expression{ID: ir.ExpressionID(b.id("expr", nil)), Kind: ir.ExpressionArrayAccess, Span: b.span(ident), ScopeID: inner.scope, Spelling: b.spelling(ident), SyntaxKind: "for_of", OperandIDs: []ir.ExpressionID{rightID}})
		}
		switch {
		case field(n, "kind") != nil && valid(left) && left.Kind() == "identifier":
			if id := b.variable(left, left, nil, nil, inner, ir.DeclarationLocal, ir.TypeUseLocal, ""); id != "" {
				b.setInitializer(id, element(left))
			}
		case field(n, "kind") != nil && valid(left) && isPattern(left.Kind()):
			for _, ident := range b.patternNames(left, inner) {
				if id := b.variable(ident, ident, nil, nil, inner, ir.DeclarationLocal, ir.TypeUseLocal, ""); id != "" {
					b.setInitializer(id, b.bindingInitializer(left, ident, element(ident), inner))
				}
			}
		default:
			b.expression(left, inner, ir.AccessWrite)
		}
		b.node(field(n, "body"), inner)
	default:
		if isExpression(n.Kind()) {
			b.expression(n, env, ir.AccessRead)
		} else {
			b.nodes(n, env)
		}
	}
}

func (b *builder) block(n *sitter.Node, env environment) {
	inner := env
	inner.scope = b.scope(ir.ScopeBlock, b.span(n), env)
	b.nodes(n, inner)
}

// exportedName is a module-level declaration exported by name after the
// fact: export { X, Y as default } or export default X.
type exportedName struct {
	name      string  // the local binding
	alias     string  // the exported name when it differs ("default" for a default export)
	span      ir.Span // the export keyword
	nameSpan  ir.Span // the exported identifier
	defaultKw *ir.Span
}

// applyExportedNames adds the export (and default) modifiers to the
// root-scope declarations that export statements name, so that a module's
// exports are readable from its declarations alone.
func (b *builder) applyExportedNames() {
	for _, ex := range b.exports {
		exported := ex.alias
		if exported == "" {
			exported = ex.name
		}
		declared := false
		for i := range b.file.Declarations {
			d := &b.file.Declarations[i]
			if d.OwnerID != "" || d.DeclaringScopeID != b.file.RootScopeID || d.Name != ex.name {
				continue
			}
			declared = true
			if !hasModifier(*d, "export") {
				d.Modifiers = append(d.Modifiers, ir.Modifier{Keyword: "export", Span: ex.span})
			}
			if exported == "default" && !hasModifier(*d, "default") {
				d.Modifiers = append(d.Modifiers, ir.Modifier{Keyword: "default", Span: *ex.defaultKw})
			}
			b.account(string(d.ID), *d)
		}
		if declared {
			if exported != ex.name && exported != "default" {
				// export { X as Y } of a local declaration: a re-export of
				// this module's own binding under another name.
				b.forward(ex, ir.Name{Segments: []ir.NameSegment{{Text: ex.name}}}, exported, "")
			}
			continue
		}
		// An exported name that is an import binding forwards that import:
		// export { X } after import { X } from "m" is export { X } from "m".
		for _, imp := range b.file.Imports {
			if imp.Kind == ir.ImportReExport || len(imp.Name.Segments) == 0 || imp.Module == "" {
				continue
			}
			local := imp.Alias
			if local == "" {
				local = imp.Name.Segments[0].Text
			}
			if local != ex.name || local == "*" || local == "default" && imp.Alias == "" {
				continue
			}
			b.forward(ex, imp.Name, exported, imp.Module)
			break
		}
	}
}

// forward records a synthesized re-export: name in module (this module when
// module is empty) exposed as exported.
func (b *builder) forward(ex exportedName, name ir.Name, exported, module string) {
	forwarded := ir.Import{Occurrence: ir.Occurrence{ID: ir.OccurrenceID(b.id("occ", nil)), Span: ex.nameSpan, ScopeID: b.file.RootScopeID}, Kind: ir.ImportReExport, Name: name, Alias: exported, Spelling: b.text[ex.nameSpan.Start.ByteOffset:ex.nameSpan.End.ByteOffset], Module: module}
	b.record()
	b.account(string(forwarded.Occurrence.ID), forwarded)
	b.file.Imports = append(b.file.Imports, forwarded)
}

func hasModifier(d ir.Declaration, keyword string) bool {
	for _, m := range d.Modifiers {
		if m.Keyword == keyword {
			return true
		}
	}
	return false
}

// export records what an export statement wraps: a declaration gets the
// export modifier, a default expression is extracted, re-exports become
// imports carrying their module, and exported local names are references
// that also mark the named root-scope declarations as exported.
func (b *builder) export(n *sitter.Node, env environment) {
	ex := &exportContext{doc: b.documentation(n)}
	for i := uint(0); i < n.ChildCount(); i++ {
		child := n.Child(i)
		switch {
		case child.Kind() == "decorator":
			ex.decorators = append(ex.decorators, child)
		case !child.IsNamed() && b.spelling(child) == "export":
			ex.span = b.span(child)
		case !child.IsNamed() && b.spelling(child) == "default":
			span := b.span(child)
			ex.defaultKw = &span
		}
	}
	if declaration := field(n, "declaration"); valid(declaration) {
		b.exporting = ex
		b.node(declaration, env)
		b.exporting = nil
		return
	}
	if value := field(n, "value"); valid(value) {
		if value.Kind() == "class" || value.Kind() == "function_expression" || value.Kind() == "arrow_function" {
			b.exporting = ex
		}
		if value.Kind() == "class" {
			b.class(value, env, !valid(field(value, "name")))
		} else {
			if value.Kind() == "identifier" {
				b.exports = append(b.exports, exportedName{name: b.spelling(value), alias: "default", span: ex.span, nameSpan: b.span(value), defaultKw: ex.defaultKw})
			}
			b.expression(value, env, ir.AccessRead)
		}
		b.exporting = nil
		return
	}
	source := field(n, "source")
	module := ""
	if valid(source) {
		module = b.stringValue(source)
	}
	reexported := false
	for _, child := range named(n) {
		switch child.Kind() {
		case "export_clause":
			for _, spec := range named(child) {
				if spec.Kind() != "export_specifier" {
					continue
				}
				name, alias := field(spec, "name"), field(spec, "alias")
				if !valid(name) {
					continue
				}
				if valid(source) {
					reexported = true
					b.importRecord(spec, env, ir.ImportReExport, b.memberName(name), b.memberName(alias), module)
				} else if name.Kind() == "identifier" {
					exported := exportedName{name: b.spelling(name), span: ex.span, nameSpan: b.span(name)}
					if valid(alias) {
						exported.alias = b.memberName(alias)
						if exported.alias == "default" {
							span := b.span(alias)
							exported.defaultKw = &span
						}
					}
					b.exports = append(b.exports, exported)
					b.expression(name, env, ir.AccessRead)
				}
			}
		case "namespace_export":
			reexported = true
			b.importRecord(child, env, ir.ImportReExport, "*", b.memberName(firstNamed(child)), module)
		}
	}
	if valid(source) && !reexported {
		// export * from "m"
		b.importRecord(n, env, ir.ImportReExport, "*", "", module)
	}
}

// importStatement records one Import per imported binding: the default
// binding, a namespace binding or each named specifier, all carrying the
// module specifier; a side-effect import records the module alone.
func (b *builder) importStatement(n *sitter.Node, env environment) {
	source := field(n, "source")
	module := b.stringValue(source)
	bound := false
	for _, clause := range named(n) {
		switch clause.Kind() {
		case "import_clause":
			for _, child := range named(clause) {
				switch child.Kind() {
				case "identifier":
					bound = true
					b.importRecord(child, env, ir.ImportSingleType, "default", b.spelling(child), module)
				case "namespace_import":
					bound = true
					b.importRecord(child, env, ir.ImportTypeOnDemand, "*", b.spelling(firstNamed(child)), module)
				case "named_imports":
					for _, spec := range named(child) {
						if spec.Kind() != "import_specifier" {
							continue
						}
						name := field(spec, "name")
						if !valid(name) {
							continue
						}
						bound = true
						b.importRecord(spec, env, ir.ImportSingleType, b.memberName(name), b.memberName(field(spec, "alias")), module)
					}
				}
			}
		case "import_require_clause":
			// import x = require("m")
			var local, spec *sitter.Node
			for _, child := range named(clause) {
				if child.Kind() == "identifier" {
					local = child
				} else if child.Kind() == "string" {
					spec = child
				}
			}
			if valid(local) && valid(spec) {
				bound = true
				b.importRecord(clause, env, ir.ImportTypeOnDemand, "*", b.spelling(local), b.stringValue(spec))
			}
		}
	}
	if !bound {
		b.importRecord(n, env, ir.ImportModule, module, "", module)
	}
}

func (b *builder) importRecord(n *sitter.Node, env environment, kind ir.ImportKind, name, alias, module string) {
	if name == "" {
		return
	}
	imp := ir.Import{Occurrence: b.occurrence(n, env), Kind: kind, Name: ir.Name{Segments: []ir.NameSegment{{Text: name}}}, Alias: alias, Spelling: b.spelling(n), Module: module}
	b.record()
	b.account(string(imp.Occurrence.ID), imp)
	b.file.Imports = append(b.file.Imports, imp)
}

func (b *builder) reserve(d ir.Declaration) int {
	b.record()
	b.account(string(d.ID), d)
	i := len(b.file.Declarations)
	b.declarationIndex[d.ID] = i
	b.file.Declarations = append(b.file.Declarations, d)
	return i
}
func (b *builder) save(i int, d ir.Declaration) {
	b.account(string(d.ID), d)
	b.file.Declarations[i] = d
}
func (b *builder) declarationKind(id ir.DeclarationID) ir.DeclarationKind {
	if i, ok := b.declarationIndex[id]; ok {
		return b.file.Declarations[i].Kind
	}
	return ""
}
func isTypeKind(kind ir.DeclarationKind) bool {
	switch kind {
	case ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationTypeAlias, ir.DeclarationNamespace:
		return true
	}
	return false
}

// form classifies a type declaration by where it is introduced.
func (b *builder) form(env environment) ir.TypeForm {
	switch {
	case env.owner == "":
		return ir.TypeTopLevel
	case isTypeKind(b.declarationKind(env.owner)):
		return ir.TypeMember
	}
	return ir.TypeLocal
}

// moduleLevel reports whether declarations in env belong to the module or a
// namespace rather than to a callable body.
func (b *builder) moduleLevel(env environment) bool {
	return env.owner == "" || b.declarationKind(env.owner) == ir.DeclarationNamespace
}

func (b *builder) baseDeclaration(n, name *sitter.Node, env environment, kind ir.DeclarationKind) ir.Declaration {
	d := ir.Declaration{ID: ir.DeclarationID(b.id("decl", n)), Kind: kind, Span: b.span(n), DeclaringScopeID: env.scope, OwnerID: env.owner}
	if valid(name) {
		d.Name = b.memberName(name)
		span := b.span(name)
		d.NameSpan = &span
	}
	end := n.EndByte()
	if body := field(n, "body"); body != nil {
		end = body.StartByte()
	} else if value := field(n, "value"); value != nil {
		end = value.StartByte()
	}
	d.SignatureText = strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(b.text[n.StartByte():end]), "="))
	d.DocComment = b.documentation(n)
	return d
}

var modifierKeywords = map[string]bool{"static": true, "async": true, "readonly": true, "abstract": true, "declare": true, "get": true, "set": true, "accessor": true, "export": true, "default": true, "override": true}

// modifiers collects written keyword modifiers, accessibility modifiers and
// decorators of a declaration node, plus decorators an export statement
// wrote before the declaration.
func (b *builder) modifiers(n *sitter.Node, env environment, d *ir.Declaration) {
	pending := b.pendingDecorator
	b.pendingDecorator = nil
	for _, decorator := range pending {
		d.AnnotationIDs = append(d.AnnotationIDs, b.decorator(decorator, env))
	}
	for i := uint(0); i < n.ChildCount(); i++ {
		child := n.Child(i)
		switch {
		case child.Kind() == "decorator":
			d.AnnotationIDs = append(d.AnnotationIDs, b.decorator(child, env))
		case child.Kind() == "accessibility_modifier" || child.Kind() == "override_modifier":
			d.Modifiers = append(d.Modifiers, ir.Modifier{Keyword: b.spelling(child), Span: b.span(child)})
		case !child.IsNamed() && modifierKeywords[b.spelling(child)]:
			d.Modifiers = append(d.Modifiers, ir.Modifier{Keyword: b.spelling(child), Span: b.span(child)})
		}
	}
	if n.Kind() == "abstract_class_declaration" {
		found := false
		for _, m := range d.Modifiers {
			found = found || m.Keyword == "abstract"
		}
		if !found {
			d.Modifiers = append(d.Modifiers, ir.Modifier{Keyword: "abstract", Span: b.rawSpan(n.StartByte(), n.StartByte())})
		}
	}
}

// decorator records @name or @name(args) as an annotation whose written
// type is the decorator expression's name chain.
func (b *builder) decorator(n *sitter.Node, env environment) ir.AnnotationID {
	inner := firstNamed(n)
	callee, arguments := inner, (*sitter.Node)(nil)
	if inner != nil && inner.Kind() == "parenthesized_expression" {
		callee = firstNamed(inner)
	}
	if callee != nil && callee.Kind() == "call_expression" {
		arguments = field(callee, "arguments")
		callee = field(callee, "function")
	}
	a := ir.Annotation{ID: ir.AnnotationID(b.id("annotation", n)), Occurrence: b.occurrence(n, env)}
	a.TypeRefID = b.typeFromExpression(callee, env, ir.TypeUseAnnotation)
	if a.TypeRefID == "" {
		a.TypeRefID = b.computedType(n, env, ir.TypeUseAnnotation)
		b.expression(callee, env, ir.AccessRead)
	}
	for _, argument := range named(arguments) {
		if id := b.expression(argument, env, ir.AccessRead); id != "" {
			a.Arguments = append(a.Arguments, ir.AnnotationArgument{Value: ir.AnnotationValue{Kind: ir.AnnotationExpression, Span: b.span(argument), ExpressionID: id}})
		}
	}
	b.record()
	b.account(string(a.ID), a)
	b.file.Annotations = append(b.file.Annotations, a)
	return a.ID
}

func (b *builder) class(n *sitter.Node, env environment, anonymous bool) ir.DeclarationID {
	ex := b.take()
	name := field(n, "name")
	if !anonymous && !valid(name) {
		return ""
	}
	d := b.baseDeclaration(n, name, env, ir.DeclarationClass)
	b.applyExport(&d, ex)
	form := b.form(env)
	if anonymous {
		form = ir.TypeAnonymous
		d.Name, d.NameSpan, d.SignatureText = "", nil, ""
	}
	d.Type = &ir.TypeDeclaration{Form: form}
	i := b.reserve(d)
	inner := environment{owner: d.ID}
	inner.scope = b.scope(ir.ScopeType, b.span(n), environment{scope: env.scope, owner: d.ID})
	d.BodyScopeID = inner.scope
	b.modifiers(n, inner, &d)
	d.Type.TypeParameterIDs = b.typeParameters(field(n, "type_parameters"), inner)
	for _, heritage := range named(n) {
		if heritage.Kind() != "class_heritage" {
			continue
		}
		for _, clause := range named(heritage) {
			switch clause.Kind() {
			case "extends_clause":
				if id := b.heritageType(field(clause, "value"), inner); id != "" {
					d.Type.Heritage = append(d.Type.Heritage, ir.Heritage{Kind: ir.HeritageExtends, TypeRefID: id, Span: b.span(clause)})
					b.typeArgumentsOf(field(clause, "type_arguments"), inner, id)
				}
			case "implements_clause":
				for _, t := range named(clause) {
					if id := b.typeRef(t, inner, ir.TypeUseHeritage, ""); id != "" {
						d.Type.Heritage = append(d.Type.Heritage, ir.Heritage{Kind: ir.HeritageImplements, TypeRefID: id, Span: b.span(t)})
					}
				}
			default:
				// JavaScript writes the extended expression directly.
				if id := b.heritageType(clause, inner); id != "" {
					d.Type.Heritage = append(d.Type.Heritage, ir.Heritage{Kind: ir.HeritageExtends, TypeRefID: id, Span: b.span(clause)})
				}
			}
		}
	}
	b.members(field(n, "body"), inner)
	b.save(i, d)
	return d.ID
}

// heritageType writes an extended expression as a named type when it is a
// name chain, or as a computed operator type with the expression extracted
// (a mixin call) otherwise.
func (b *builder) heritageType(value *sitter.Node, env environment) ir.TypeRefID {
	if id := b.typeFromExpression(value, env, ir.TypeUseHeritage); id != "" {
		return id
	}
	if !valid(value) {
		return ""
	}
	id := b.computedType(value, env, ir.TypeUseHeritage)
	b.expression(value, env, ir.AccessRead)
	return id
}

// members extracts a class, interface or object-type body.
func (b *builder) members(body *sitter.Node, env environment) {
	for _, member := range named(body) {
		switch member.Kind() {
		case "method_definition", "method_signature", "abstract_method_signature":
			b.method(member, env)
		case "public_field_definition", "field_definition", "property_signature":
			b.fieldDeclaration(member, env)
		case "class_static_block":
			b.initializer(member, env)
		case "export_statement":
			b.export(member, env)
		}
	}
}

func (b *builder) method(n *sitter.Node, env environment) {
	nameNode := field(n, "name")
	if !valid(nameNode) {
		return
	}
	name := b.memberName(nameNode)
	kind := ir.DeclarationMethod
	if name == "constructor" && nameNode.Kind() == "property_identifier" && b.declarationKind(env.owner) == ir.DeclarationClass {
		kind = ir.DeclarationConstructor
	}
	d := b.baseDeclaration(n, nameNode, env, kind)
	d.Callable = &ir.CallableDeclaration{}
	if kind == ir.DeclarationConstructor {
		d.Callable.ConstructorForm = ir.ConstructorNormal
	}
	i := b.reserve(d)
	inner := environment{owner: d.ID}
	inner.scope = b.scope(ir.ScopeCallable, b.span(n), environment{scope: env.scope, owner: d.ID})
	d.BodyScopeID = inner.scope
	b.modifiers(n, inner, &d)
	d.Callable.TypeParameterIDs = b.typeParameters(field(n, "type_parameters"), inner)
	d.Callable.ParameterIDs = b.parameters(field(n, "parameters"), inner)
	d.Callable.ReturnTypeID = b.returnType(field(n, "return_type"), inner)
	if body := field(n, "body"); valid(body) {
		var returns []ir.ExpressionID
		inner.returns = &returns
		b.nodes(body, inner)
		d.Callable.ReturnExpressionIDs = returns
	}
	b.save(i, d)
}

func (b *builder) function(n *sitter.Node, env environment) {
	ex := b.take()
	name := field(n, "name")
	if !valid(name) {
		return
	}
	d := b.baseDeclaration(n, name, env, ir.DeclarationFunction)
	b.applyExport(&d, ex)
	d.Callable = &ir.CallableDeclaration{}
	i := b.reserve(d)
	inner := environment{owner: d.ID}
	inner.scope = b.scope(ir.ScopeCallable, b.span(n), environment{scope: env.scope, owner: d.ID})
	d.BodyScopeID = inner.scope
	b.modifiers(n, inner, &d)
	d.Callable.TypeParameterIDs = b.typeParameters(field(n, "type_parameters"), inner)
	d.Callable.ParameterIDs = b.parameters(field(n, "parameters"), inner)
	d.Callable.ReturnTypeID = b.returnType(field(n, "return_type"), inner)
	if body := field(n, "body"); valid(body) {
		var returns []ir.ExpressionID
		inner.returns = &returns
		b.nodes(body, inner)
		d.Callable.ReturnExpressionIDs = returns
	}
	b.save(i, d)
}

// callableBody extracts a function literal's signature and body into a
// declaration that already owns a callable scope.
func (b *builder) callableBody(fn *sitter.Node, inner environment, d *ir.Declaration) {
	d.Callable.TypeParameterIDs = b.typeParameters(field(fn, "type_parameters"), inner)
	if single := field(fn, "parameter"); valid(single) {
		if id := b.variable(single, single, nil, nil, inner, ir.DeclarationParameter, ir.TypeUseParameter, ""); id != "" {
			d.Callable.ParameterIDs = []ir.DeclarationID{id}
		}
	} else {
		d.Callable.ParameterIDs = b.parameters(field(fn, "parameters"), inner)
	}
	d.Callable.ReturnTypeID = b.returnType(field(fn, "return_type"), inner)
	for i := uint(0); i < fn.ChildCount(); i++ {
		if child := fn.Child(i); !child.IsNamed() && b.spelling(child) == "async" {
			d.Modifiers = append(d.Modifiers, ir.Modifier{Keyword: "async", Span: b.span(child)})
		}
	}
	body := field(fn, "body")
	switch {
	case !valid(body):
	case body.Kind() == "statement_block":
		var returns []ir.ExpressionID
		inner.returns = &returns
		b.nodes(body, inner)
		d.Callable.ReturnExpressionIDs = returns
	default:
		d.Callable.BodyExpressionID = b.expression(body, inner, ir.AccessRead)
	}
}

func isFunctionLiteral(kind string) bool {
	return kind == "arrow_function" || kind == "function_expression" || kind == "generator_function"
}

func (b *builder) fieldDeclaration(n *sitter.Node, env environment) {
	nameNode := field(n, "name")
	if nameNode == nil {
		nameNode = field(n, "property")
	}
	if !valid(nameNode) {
		return
	}
	value, typ := field(n, "value"), field(n, "type")
	if valid(value) && isFunctionLiteral(value.Kind()) {
		// A field holding a function literal is the class's callable member.
		d := b.baseDeclaration(n, nameNode, env, ir.DeclarationMethod)
		d.Callable = &ir.CallableDeclaration{}
		i := b.reserve(d)
		inner := environment{owner: d.ID}
		inner.scope = b.scope(ir.ScopeCallable, b.span(n), environment{scope: env.scope, owner: d.ID})
		d.BodyScopeID = inner.scope
		b.modifiers(n, inner, &d)
		if typ != nil {
			b.typeRef(typ, inner, ir.TypeUseField, "")
		}
		b.callableBody(value, inner, &d)
		b.save(i, d)
		return
	}
	d := b.baseDeclaration(n, nameNode, env, ir.DeclarationField)
	d.Variable = &ir.VariableDeclaration{}
	i := b.reserve(d)
	inner := environment{scope: env.scope, owner: d.ID}
	b.modifiers(n, inner, &d)
	d.Variable.DeclaredTypeID = b.typeRef(typ, inner, ir.TypeUseField, "")
	if valid(value) {
		inner.scope = b.scope(ir.ScopeInitializer, b.span(value), inner)
		d.Variable.InitializerID = b.expression(value, inner, ir.AccessRead)
	}
	b.save(i, d)
}

func (b *builder) initializer(n *sitter.Node, env environment) {
	d := b.baseDeclaration(n, nil, env, ir.DeclarationInitializer)
	d.SignatureText = ""
	d.Initializer = &ir.InitializerDeclaration{Kind: ir.InitializerStatic}
	i := b.reserve(d)
	inner := environment{owner: d.ID}
	inner.scope = b.scope(ir.ScopeInitializer, b.span(n), environment{scope: env.scope, owner: d.ID})
	d.BodyScopeID = inner.scope
	b.nodes(n, inner)
	b.save(i, d)
}

// variables extracts const, let and var declarators: a function literal
// bound to a name is a function declaration, a destructuring pattern
// declares nothing but its initializer is still extracted, and every other
// declarator is a module-level variable or a local.
func (b *builder) variables(n *sitter.Node, env environment) {
	ex := b.take()
	keyword := "var"
	if kind := field(n, "kind"); kind != nil {
		keyword = b.spelling(kind)
	} else if n.Kind() == "lexical_declaration" {
		keyword = "let"
	}
	for _, declarator := range named(n) {
		if declarator.Kind() != "variable_declarator" {
			continue
		}
		name, value, typ := field(declarator, "name"), field(declarator, "value"), field(declarator, "type")
		kind, role := ir.DeclarationLocal, ir.TypeUseLocal
		if b.moduleLevel(env) {
			kind, role = ir.DeclarationVariable, ir.TypeUseField
		}
		if module, ok := b.requireSpecifier(value); ok && valid(name) {
			// const x = require("m") and const { a, b: c } = require("m")
			// bind imports, not variables; the call itself is still syntax.
			b.typeRef(typ, env, role, "")
			b.expression(value, env, ir.AccessRead)
			b.requireImports(declarator, name, module, env)
			continue
		}
		if valid(name) && isPattern(name.Kind()) {
			// A destructuring declaration: the written type is a fact of
			// the statement, and each top-level binding is initialized by
			// the member or element it takes from the initializer.
			b.typeRef(typ, env, role, "")
			valueID := b.expression(value, env, ir.AccessRead)
			for _, ident := range b.patternNames(name, env) {
				d := b.baseDeclaration(ident, ident, env, kind)
				d.SignatureText = strings.TrimSpace(keyword + " " + d.SignatureText)
				b.applyExport(&d, ex)
				d.Variable = &ir.VariableDeclaration{InitializerID: b.bindingInitializer(name, ident, valueID, env)}
				b.save(b.reserve(d), d)
			}
			continue
		}
		if !valid(name) || name.Kind() != "identifier" {
			if typ != nil {
				b.typeRef(typ, env, role, "")
			}
			b.expression(value, env, ir.AccessRead)
			continue
		}
		if valid(value) && isFunctionLiteral(value.Kind()) {
			d := b.baseDeclaration(declarator, name, env, ir.DeclarationFunction)
			d.SignatureText = strings.TrimSpace(keyword + " " + d.SignatureText)
			b.applyExport(&d, ex)
			d.Callable = &ir.CallableDeclaration{}
			i := b.reserve(d)
			inner := environment{owner: d.ID}
			inner.scope = b.scope(ir.ScopeCallable, b.span(declarator), environment{scope: env.scope, owner: d.ID})
			d.BodyScopeID = inner.scope
			b.modifiers(declarator, inner, &d)
			if typ != nil {
				b.typeRef(typ, inner, ir.TypeUseLocal, "")
			}
			b.callableBody(value, inner, &d)
			b.save(i, d)
			continue
		}
		d := b.baseDeclaration(declarator, name, env, kind)
		d.SignatureText = strings.TrimSpace(keyword + " " + d.SignatureText)
		b.applyExport(&d, ex)
		d.Variable = &ir.VariableDeclaration{}
		i := b.reserve(d)
		inner := environment{scope: env.scope, owner: d.ID}
		b.modifiers(declarator, inner, &d)
		d.Variable.DeclaredTypeID = b.typeRef(typ, inner, role, "")
		if valid(value) {
			if kind == ir.DeclarationVariable {
				inner.scope = b.scope(ir.ScopeInitializer, b.span(value), inner)
			}
			d.Variable.InitializerID = b.expression(value, inner, ir.AccessRead)
		}
		b.save(i, d)
	}
}

// requireSpecifier recognizes require("m") with a literal specifier.
func (b *builder) requireSpecifier(value *sitter.Node) (string, bool) {
	if !valid(value) || value.Kind() != "call_expression" {
		return "", false
	}
	callee, arguments := field(value, "function"), named(field(value, "arguments"))
	if !valid(callee) || callee.Kind() != "identifier" || b.spelling(callee) != "require" || len(arguments) != 1 || arguments[0].Kind() != "string" {
		return "", false
	}
	return b.stringValue(arguments[0]), true
}

// requireImports records the bindings of a require declarator: a name is a
// namespace import of the module, an object pattern imports each property
// (aliased by its binding), and anything else binds nothing.
func (b *builder) requireImports(declarator, name *sitter.Node, module string, env environment) {
	switch name.Kind() {
	case "identifier":
		b.importRecord(declarator, env, ir.ImportTypeOnDemand, "*", b.spelling(name), module)
	case "object_pattern":
		for _, property := range named(name) {
			switch property.Kind() {
			case "shorthand_property_identifier_pattern":
				b.importRecord(property, env, ir.ImportSingleType, b.spelling(property), "", module)
			case "pair_pattern":
				key, value := field(property, "key"), field(property, "value")
				if valid(key) && valid(value) && value.Kind() == "identifier" {
					b.importRecord(property, env, ir.ImportSingleType, b.memberName(key), b.spelling(value), module)
				}
			case "object_assignment_pattern":
				if left := field(property, "left"); valid(left) && left.Kind() == "shorthand_property_identifier_pattern" {
					b.importRecord(property, env, ir.ImportSingleType, b.spelling(left), "", module)
				}
				b.expression(field(property, "right"), env, ir.AccessRead)
			}
		}
	}
}

func isPattern(kind string) bool { return kind == "object_pattern" || kind == "array_pattern" }

// bindingInitializer desugars a top-level destructuring binding into the
// access it stands for: `{ x }` and `{ key: x }` take a member of the
// initializer, `[x]` an element. Nested bindings keep no initializer.
func (b *builder) bindingInitializer(pattern, ident *sitter.Node, value ir.ExpressionID, env environment) ir.ExpressionID {
	if value == "" || !valid(ident) {
		return ""
	}
	parent := ident.Parent()
	if parent == nil {
		return ""
	}
	member := ""
	switch {
	case parent.Kind() == "object_pattern" && ident.Kind() == "shorthand_property_identifier_pattern":
		member = b.spelling(ident)
	case parent.Kind() == "pair_pattern" && parent.Parent() != nil && parent.Parent().Kind() == "object_pattern":
		if key := field(parent, "key"); valid(key) {
			member = b.memberName(key)
		}
	case parent.Kind() == "object_assignment_pattern" && parent.Parent() != nil && parent.Parent().Kind() == "object_pattern":
		member = b.spelling(ident)
	case parent.Kind() == "array_pattern", parent.Kind() == "assignment_pattern" && parent.Parent() != nil && parent.Parent().Kind() == "array_pattern":
		x := ir.Expression{ID: ir.ExpressionID(b.id("expr", nil)), Kind: ir.ExpressionArrayAccess, Span: b.span(ident), ScopeID: env.scope, Spelling: b.spelling(ident), SyntaxKind: "array_pattern", OperandIDs: []ir.ExpressionID{value}}
		return b.saveSynthetic(x)
	}
	if member == "" || parent.Parent() != pattern && parent != pattern {
		return ""
	}
	name := ir.Name{Segments: []ir.NameSegment{{Text: member}}}
	x := ir.Expression{ID: ir.ExpressionID(b.id("expr", nil)), Kind: ir.ExpressionMemberAccess, Span: b.span(ident), ScopeID: env.scope, Spelling: b.spelling(ident), SyntaxKind: "object_pattern", Name: &name, OperandIDs: []ir.ExpressionID{value}}
	id := b.saveSynthetic(x)
	r := ir.Reference{Occurrence: ir.Occurrence{ID: ir.OccurrenceID(b.id("occ", nil)), Span: b.span(ident), ScopeID: env.scope, EnclosingDeclarationID: env.owner}, Kind: ir.ReferenceMember, ExpressionID: id, Name: name, ReceiverID: value, Access: ir.AccessRead}
	b.record()
	b.account(string(r.Occurrence.ID), r)
	b.file.References = append(b.file.References, r)
	return id
}

// setInitializer attaches a derived initializer to a saved variable.
func (b *builder) setInitializer(id ir.DeclarationID, initializer ir.ExpressionID) {
	if initializer == "" {
		return
	}
	if i, ok := b.declarationIndex[id]; ok && b.file.Declarations[i].Variable != nil {
		b.file.Declarations[i].Variable.InitializerID = initializer
		b.account(string(id), b.file.Declarations[i])
	}
}

// saveSynthetic records an expression the parser derived from a pattern.
func (b *builder) saveSynthetic(x ir.Expression) ir.ExpressionID {
	b.record()
	b.account(string(x.ID), x)
	b.file.Expressions = append(b.file.Expressions, x)
	return x.ID
}

// parameterProperty declares the field a constructor parameter with an
// accessibility, readonly or override modifier introduces on its class.
func (b *builder) parameterProperty(p, name *sitter.Node, parameter ir.DeclarationID, env environment) {
	property := false
	for _, child := range named(p) {
		switch child.Kind() {
		case "accessibility_modifier", "override_modifier":
			property = true
		}
	}
	for i := uint(0); i < p.ChildCount() && !property; i++ {
		if child := p.Child(i); !child.IsNamed() && b.spelling(child) == "readonly" {
			property = true
		}
	}
	if !property || env.owner == "" {
		return
	}
	i, ok := b.declarationIndex[env.owner]
	if !ok || b.file.Declarations[i].Kind != ir.DeclarationConstructor || b.file.Declarations[i].OwnerID == "" {
		return
	}
	class := b.file.Declarations[i].OwnerID
	scope := ir.ScopeID("")
	for _, sc := range b.file.Scopes {
		if sc.ID == env.scope {
			scope = sc.ParentID
		}
	}
	if scope == "" {
		return
	}
	span := b.span(name)
	d := ir.Declaration{ID: ir.DeclarationID(b.id("decl", nil)), Kind: ir.DeclarationField, Name: b.spelling(name), NameSpan: &span, Span: b.span(p), DeclaringScopeID: scope, OwnerID: class, SignatureText: b.spelling(p), Variable: &ir.VariableDeclaration{}}
	if parameter != "" {
		if j, ok := b.declarationIndex[parameter]; ok && b.file.Declarations[j].Variable != nil {
			d.Variable.DeclaredTypeID = b.file.Declarations[j].Variable.DeclaredTypeID
			d.Modifiers = append(d.Modifiers, b.file.Declarations[j].Modifiers...)
		}
	}
	b.save(b.reserve(d), d)
}

// patternNames collects the identifiers a binding pattern introduces, in
// source order, and extracts the default-value expressions it carries.
func (b *builder) patternNames(pattern *sitter.Node, env environment) []*sitter.Node {
	var out []*sitter.Node
	var walk func(n *sitter.Node, depth int)
	walk = func(n *sitter.Node, depth int) {
		if !valid(n) || depth > 32 {
			return
		}
		switch n.Kind() {
		case "identifier", "shorthand_property_identifier_pattern":
			out = append(out, n)
		case "object_pattern", "array_pattern":
			for _, child := range named(n) {
				walk(child, depth+1)
			}
		case "pair_pattern":
			walk(field(n, "value"), depth+1)
		case "rest_pattern":
			walk(firstNamed(n), depth+1)
		case "assignment_pattern", "object_assignment_pattern":
			walk(field(n, "left"), depth+1)
			b.expression(field(n, "right"), env, ir.AccessRead)
		}
	}
	walk(pattern, 0)
	return out
}

// variable declares a parameter, local or catch variable.
func (b *builder) variable(n, name, typ, value *sitter.Node, env environment, kind ir.DeclarationKind, role ir.TypeUseRole, variadicKind string) ir.DeclarationID {
	if !valid(name) {
		return ""
	}
	d := b.baseDeclaration(n, name, env, kind)
	d.Variable = &ir.VariableDeclaration{Variadic: variadicKind == "rest"}
	i := b.reserve(d)
	inner := environment{scope: env.scope, owner: d.ID}
	b.modifiers(n, inner, &d)
	d.Variable.DeclaredTypeID = b.typeRef(typ, inner, role, "")
	if valid(value) {
		d.Variable.InitializerID = b.expression(value, inner, ir.AccessRead)
	}
	b.save(i, d)
	return d.ID
}

// parameters declares every named parameter; destructured parameters
// declare nothing but their types and defaults are still extracted.
func (b *builder) parameters(n *sitter.Node, env environment) []ir.DeclarationID {
	var ids []ir.DeclarationID
	add := func(id ir.DeclarationID) {
		if id != "" {
			ids = append(ids, id)
		}
	}
	for _, p := range named(n) {
		switch p.Kind() {
		case "required_parameter", "optional_parameter":
			pattern := field(p, "pattern")
			if pattern == nil {
				pattern = field(p, "name")
			}
			typ, value := field(p, "type"), field(p, "value")
			variadic := ""
			if pattern != nil && pattern.Kind() == "rest_pattern" {
				variadic, pattern = "rest", firstNamed(pattern)
			}
			if valid(pattern) && pattern.Kind() == "identifier" {
				id := b.variable(p, pattern, typ, value, env, ir.DeclarationParameter, ir.TypeUseParameter, variadic)
				add(id)
				b.parameterProperty(p, pattern, id, env)
				continue
			}
			b.typeRef(typ, env, ir.TypeUseParameter, "")
			b.expression(value, env, ir.AccessRead)
			if valid(pattern) && isPattern(pattern.Kind()) {
				for _, ident := range b.patternNames(pattern, env) {
					add(b.variable(ident, ident, nil, nil, env, ir.DeclarationParameter, ir.TypeUseParameter, variadic))
				}
			}
		case "identifier":
			add(b.variable(p, p, nil, nil, env, ir.DeclarationParameter, ir.TypeUseParameter, ""))
		case "assignment_pattern":
			if left := field(p, "left"); valid(left) && left.Kind() == "identifier" {
				add(b.variable(p, left, nil, field(p, "right"), env, ir.DeclarationParameter, ir.TypeUseParameter, ""))
			} else {
				b.expression(field(p, "right"), env, ir.AccessRead)
			}
		case "rest_pattern":
			if inner := firstNamed(p); valid(inner) && inner.Kind() == "identifier" {
				add(b.variable(p, inner, nil, nil, env, ir.DeclarationParameter, ir.TypeUseParameter, "rest"))
			}
		}
	}
	return ids
}

func (b *builder) typeParameters(n *sitter.Node, env environment) []ir.DeclarationID {
	var ids []ir.DeclarationID
	for _, parameter := range named(n) {
		if parameter.Kind() != "type_parameter" {
			continue
		}
		name := field(parameter, "name")
		if !valid(name) {
			continue
		}
		d := b.baseDeclaration(parameter, name, env, ir.DeclarationTypeParameter)
		d.TypeParameter = &ir.TypeParameterDeclaration{}
		i := b.reserve(d)
		inner := environment{scope: env.scope, owner: d.ID}
		if constraint := field(parameter, "constraint"); constraint != nil {
			if id := b.typeRef(firstNamed(constraint), inner, ir.TypeUseBound, ""); id != "" {
				d.TypeParameter.BoundTypeIDs = append(d.TypeParameter.BoundTypeIDs, id)
			}
		}
		b.save(i, d)
		ids = append(ids, d.ID)
	}
	return ids
}

func (b *builder) interfaceDeclaration(n *sitter.Node, env environment) {
	ex := b.take()
	name := field(n, "name")
	if !valid(name) {
		return
	}
	d := b.baseDeclaration(n, name, env, ir.DeclarationInterface)
	b.applyExport(&d, ex)
	d.Type = &ir.TypeDeclaration{Form: b.form(env)}
	i := b.reserve(d)
	inner := environment{owner: d.ID}
	inner.scope = b.scope(ir.ScopeType, b.span(n), environment{scope: env.scope, owner: d.ID})
	d.BodyScopeID = inner.scope
	b.modifiers(n, inner, &d)
	d.Type.TypeParameterIDs = b.typeParameters(field(n, "type_parameters"), inner)
	if clause := childKind(n, "extends_type_clause"); clause != nil {
		for _, t := range named(clause) {
			if id := b.typeRef(t, inner, ir.TypeUseHeritage, ""); id != "" {
				d.Type.Heritage = append(d.Type.Heritage, ir.Heritage{Kind: ir.HeritageExtends, TypeRefID: id, Span: b.span(t)})
			}
		}
	}
	b.members(field(n, "body"), inner)
	b.save(i, d)
}

func (b *builder) enumDeclaration(n *sitter.Node, env environment) {
	ex := b.take()
	name := field(n, "name")
	if !valid(name) {
		return
	}
	d := b.baseDeclaration(n, name, env, ir.DeclarationEnum)
	b.applyExport(&d, ex)
	d.Type = &ir.TypeDeclaration{Form: b.form(env)}
	i := b.reserve(d)
	inner := environment{owner: d.ID}
	inner.scope = b.scope(ir.ScopeType, b.span(n), environment{scope: env.scope, owner: d.ID})
	d.BodyScopeID = inner.scope
	b.modifiers(n, inner, &d)
	for _, member := range named(field(n, "body")) {
		nameNode, value := member, (*sitter.Node)(nil)
		if member.Kind() == "enum_assignment" {
			nameNode, value = field(member, "name"), field(member, "value")
		}
		if !valid(nameNode) {
			continue
		}
		c := b.baseDeclaration(member, nameNode, inner, ir.DeclarationEnumConstant)
		c.Variable = &ir.VariableDeclaration{}
		j := b.reserve(c)
		if valid(value) {
			c.Variable.InitializerID = b.expression(value, environment{scope: inner.scope, owner: c.ID}, ir.AccessRead)
		}
		b.save(j, c)
	}
	b.save(i, d)
}

func (b *builder) typeAlias(n *sitter.Node, env environment) {
	ex := b.take()
	name := field(n, "name")
	if !valid(name) {
		return
	}
	d := b.baseDeclaration(n, name, env, ir.DeclarationTypeAlias)
	b.applyExport(&d, ex)
	d.Type = &ir.TypeDeclaration{Form: b.form(env)}
	i := b.reserve(d)
	inner := environment{owner: d.ID}
	inner.scope = b.scope(ir.ScopeType, b.span(n), environment{scope: env.scope, owner: d.ID})
	d.BodyScopeID = inner.scope
	b.modifiers(n, inner, &d)
	d.Type.TypeParameterIDs = b.typeParameters(field(n, "type_parameters"), inner)
	b.typeRef(field(n, "value"), inner, ir.TypeUseAlias, "")
	b.aliasMembers(field(n, "value"), inner, &d)
	b.save(i, d)
}

// aliasMembers gives a type alias the members its aliased object type
// literal writes (`type P = { a: A }`, also under Readonly<...> or in an
// intersection) and, for an intersection, the named constituents as
// written bases, so a value of the alias type has members to bind.
func (b *builder) aliasMembers(value *sitter.Node, inner environment, d *ir.Declaration) {
	if !valid(value) {
		return
	}
	switch value.Kind() {
	case "parenthesized_type":
		b.aliasMembers(firstNamed(value), inner, d)
	case "object_type":
		b.members(value, inner)
	case "type_identifier", "nested_type_identifier":
		// type Alias = Named: the named type is the alias's base.
		if id := b.typeAt(b.span(value)); id != "" {
			d.Type.Heritage = append(d.Type.Heritage, ir.Heritage{Kind: ir.HeritageExtends, TypeRefID: id, Span: b.span(value)})
		}
	case "generic_type":
		if !hasObjectArgument(value) {
			if id := b.typeAt(b.span(value)); id != "" {
				d.Type.Heritage = append(d.Type.Heritage, ir.Heritage{Kind: ir.HeritageExtends, TypeRefID: id, Span: b.span(value)})
			}
		}
		for _, argument := range named(field(value, "type_arguments")) {
			if argument.Kind() == "object_type" {
				b.members(argument, inner)
			}
		}
	case "union_type", "intersection_type":
		// A union's constituents are recorded like an intersection's: the
		// syntax tier binds a member to the first constituent declaring it.
		for _, part := range named(value) {
			switch part.Kind() {
			case "object_type", "parenthesized_type", "generic_type", "intersection_type", "union_type":
				b.aliasMembers(part, inner, d)
			case "type_identifier", "nested_type_identifier":
				if id := b.typeAt(b.span(part)); id != "" {
					d.Type.Heritage = append(d.Type.Heritage, ir.Heritage{Kind: ir.HeritageExtends, TypeRefID: id, Span: b.span(part)})
				}
			}
			if part.Kind() == "generic_type" && !hasObjectArgument(part) {
				// Omit<Base, K>, Partial<Base>: the named argument is a base.
				if id := b.typeAt(b.span(part)); id != "" {
					d.Type.Heritage = append(d.Type.Heritage, ir.Heritage{Kind: ir.HeritageExtends, TypeRefID: id, Span: b.span(part)})
				}
			}
		}
	}
}

func hasObjectArgument(generic *sitter.Node) bool {
	for _, argument := range named(field(generic, "type_arguments")) {
		if argument.Kind() == "object_type" {
			return true
		}
	}
	return false
}

// typeAt finds the recorded written type with exactly this span.
func (b *builder) typeAt(span ir.Span) ir.TypeRefID {
	for i := len(b.file.Types) - 1; i >= 0; i-- {
		if b.file.Types[i].Span == span {
			return b.file.Types[i].ID
		}
	}
	return ""
}

func (b *builder) namespace(n *sitter.Node, env environment) {
	ex := b.take()
	name := field(n, "name")
	if !valid(name) {
		return
	}
	d := b.baseDeclaration(n, name, env, ir.DeclarationNamespace)
	b.applyExport(&d, ex)
	d.Type = &ir.TypeDeclaration{Form: b.form(env)}
	i := b.reserve(d)
	inner := environment{owner: d.ID}
	inner.scope = b.scope(ir.ScopeType, b.span(n), environment{scope: env.scope, owner: d.ID})
	d.BodyScopeID = inner.scope
	b.modifiers(n, inner, &d)
	if body := field(n, "body"); valid(body) {
		b.nodes(body, inner)
	}
	b.save(i, d)
}
