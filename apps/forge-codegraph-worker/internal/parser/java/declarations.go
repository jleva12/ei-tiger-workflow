package java

import (
	"strings"

	"ei-aitiger-codegraph/pkg/ir"

	sitter "github.com/tree-sitter/go-tree-sitter"
)

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
func (b *builder) ownerIsType(id ir.DeclarationID) bool {
	i, ok := b.declarationIndex[id]
	return ok && b.file.Declarations[i].Type != nil
}
func (b *builder) baseDeclaration(n, name *sitter.Node, env environment, kind ir.DeclarationKind) ir.Declaration {
	d := ir.Declaration{ID: ir.DeclarationID(b.id("decl", n)), Kind: kind, Span: b.span(n), DeclaringScopeID: env.scope, OwnerID: env.owner}
	if valid(name) {
		d.Name = b.syntaxText(name)
		span := b.span(name)
		d.NameSpan = &span
	}
	end := n.EndByte()
	if body := field(n, "body"); body != nil {
		end = body.StartByte()
	}
	d.SignatureText = strings.TrimSpace(b.rawSlice(n.StartByte(), end))
	d.DocComment = b.documentation(n)
	return d
}

func (b *builder) documentation(n *sitter.Node) string {
	if n == nil {
		return ""
	}
	if previous := n.PrevNamedSibling(); previous != nil && previous.Kind() == "block_comment" && strings.HasPrefix(b.syntaxText(previous), "/**") && strings.TrimSpace(b.translated.text(previous.EndByte(), n.StartByte())) == "" {
		return b.spelling(previous)
	}
	return ""
}

func (b *builder) declaration(n *sitter.Node, env environment) {
	switch n.Kind() {
	case "class_declaration":
		b.typeDeclaration(n, env, ir.DeclarationClass, false)
	case "interface_declaration":
		b.typeDeclaration(n, env, ir.DeclarationInterface, false)
	case "record_declaration":
		b.typeDeclaration(n, env, ir.DeclarationRecord, false)
	case "enum_declaration":
		b.typeDeclaration(n, env, ir.DeclarationEnum, false)
	case "annotation_type_declaration":
		b.typeDeclaration(n, env, ir.DeclarationAnnotationType, false)
	case "method_declaration", "annotation_type_element_declaration":
		b.callable(n, env, ir.DeclarationMethod)
	case "constructor_declaration", "compact_constructor_declaration":
		b.callable(n, env, ir.DeclarationConstructor)
	case "field_declaration", "constant_declaration", "local_variable_declaration":
		kind, role := ir.DeclarationField, ir.TypeUseField
		if n.Kind() == "local_variable_declaration" {
			kind, role = ir.DeclarationLocal, ir.TypeUseLocal
		}
		for _, declarator := range named(n) {
			if declarator.Kind() == "variable_declarator" {
				id := b.variable(declarator, field(declarator, "name"), field(n, "type"), field(declarator, "value"), field(declarator, "dimensions"), env, kind, role, false)
				if id != "" {
					i := b.declarationIndex[id]
					d := b.file.Declarations[i]
					b.modifiers(n, environment{env.scope, d.ID}, &d)
					d.DocComment = b.documentation(n)
					b.save(i, d)
				}
			}
		}
	case "enum_constant":
		b.enumConstant(n, env)
	}
}

func (b *builder) typeDeclaration(n *sitter.Node, env environment, kind ir.DeclarationKind, anonymous bool) ir.DeclarationID {
	name := field(n, "name")
	if !anonymous && !valid(name) {
		return ""
	}
	d := b.baseDeclaration(n, name, env, kind)
	form := ir.TypeTopLevel
	if env.owner != "" {
		form = ir.TypeLocal
		if b.ownerIsType(env.owner) {
			form = ir.TypeMember
		}
	}
	if anonymous {
		form = ir.TypeAnonymous
		d.Name, d.NameSpan, d.SignatureText = "", nil, ""
	}
	d.Type = &ir.TypeDeclaration{Form: form}
	i := b.reserve(d)
	inner := environment{owner: d.ID}
	inner.scope = b.scope(ir.ScopeType, b.span(n), environment{env.scope, d.ID})
	d.BodyScopeID = inner.scope
	b.modifiers(n, inner, &d)
	d.Type.TypeParameterIDs = b.typeParameters(field(n, "type_parameters"), inner)
	if kind == ir.DeclarationRecord {
		d.Type.RecordComponentIDs = b.parameters(field(n, "parameters"), inner, true)
	}
	for _, child := range named(n) {
		heritageKind, role := ir.HeritageExtends, ir.TypeUseHeritage
		switch child.Kind() {
		case "superclass", "extends_interfaces":
		case "super_interfaces":
			heritageKind = ir.HeritageImplements
		case "permits":
			heritageKind, role = ir.HeritagePermits, ir.TypeUsePermittedType
		default:
			continue
		}
		members := named(child)
		if len(members) == 1 && members[0].Kind() == "type_list" {
			members = named(members[0])
		}
		for _, member := range members {
			if id := b.typeRef(member, inner, role, ""); id != "" {
				d.Type.Heritage = append(d.Type.Heritage, ir.Heritage{Kind: heritageKind, TypeRefID: id, Span: b.span(member)})
			}
		}
	}
	body := field(n, "body")
	if anonymous {
		body = n
	}
	b.children(body, inner)
	b.save(i, d)
	return d.ID
}

func (b *builder) callable(n *sitter.Node, env environment, kind ir.DeclarationKind) {
	if !valid(field(n, "name")) {
		return
	}
	d := b.baseDeclaration(n, field(n, "name"), env, kind)
	d.Callable = &ir.CallableDeclaration{}
	if kind == ir.DeclarationConstructor {
		d.Callable.ConstructorForm = ir.ConstructorNormal
		if n.Kind() == "compact_constructor_declaration" {
			d.Callable.ConstructorForm = ir.ConstructorCompact
		}
	}
	i := b.reserve(d)
	inner := environment{owner: d.ID}
	inner.scope = b.scope(ir.ScopeCallable, b.span(n), environment{env.scope, d.ID})
	d.BodyScopeID = inner.scope
	b.modifiers(n, inner, &d)
	d.Callable.TypeParameterIDs = b.typeParameters(field(n, "type_parameters"), inner)
	d.Callable.ParameterIDs = b.parameters(field(n, "parameters"), inner, false)
	if kind != ir.DeclarationConstructor {
		d.Callable.ReturnTypeID = b.typeRef(field(n, "type"), inner, ir.TypeUseReturn, "")
		d.Callable.ReturnTypeID = b.arraySuffix(d.Callable.ReturnTypeID, field(n, "dimensions"), inner, ir.TypeUseReturn)
	}
	for _, thrown := range named(childKind(n, "throws")) {
		if id := b.typeRef(thrown, inner, ir.TypeUseThrows, ""); id != "" {
			d.Callable.ThrowsTypeIDs = append(d.Callable.ThrowsTypeIDs, id)
		}
	}
	if value := field(n, "value"); valid(value) {
		if n.Kind() == "annotation_type_element_declaration" {
			v := b.annotationValue(value, inner)
			d.Callable.AnnotationDefault = &v
		} else {
			d.Callable.DefaultValueID = b.expression(value, inner, ir.AccessRead)
		}
	}
	d.Callable.BodyStatementID = b.visit(field(n, "body"), inner)
	b.save(i, d)
}

func (b *builder) variable(n, name, typ, value, dimensions *sitter.Node, env environment, kind ir.DeclarationKind, role ir.TypeUseRole, variadic bool) ir.DeclarationID {
	if !valid(name) {
		return ""
	}
	d := b.baseDeclaration(n, name, env, kind)
	d.Variable = &ir.VariableDeclaration{Variadic: variadic}
	i := b.reserve(d)
	inner := environment{env.scope, d.ID}
	d.Variable.DeclaredTypeID = b.typeRef(typ, inner, role, "")
	d.Variable.DeclaredTypeID = b.arraySuffix(d.Variable.DeclaredTypeID, dimensions, inner, role)
	b.modifiers(n, inner, &d)
	if value != nil {
		if kind == ir.DeclarationField {
			inner.scope = b.scope(ir.ScopeInitializer, b.span(value), inner)
		}
		d.Variable.InitializerID = b.expression(value, inner, ir.AccessRead)
	}
	b.save(i, d)
	return d.ID
}

func (b *builder) parameters(n *sitter.Node, env environment, components bool) []ir.DeclarationID {
	var result []ir.DeclarationID
	for _, parameter := range named(n) {
		kind, role := ir.DeclarationParameter, ir.TypeUseParameter
		if components {
			kind, role = ir.DeclarationRecordComponent, ir.TypeUseRecordComponent
		}
		name, typ, dimensions := field(parameter, "name"), field(parameter, "type"), field(parameter, "dimensions")
		variadic := parameter.Kind() == "spread_parameter"
		if variadic {
			declarator := childKind(parameter, "variable_declarator")
			name, dimensions = field(declarator, "name"), field(declarator, "dimensions")
		}
		if parameter.Kind() == "receiver_parameter" {
			kind = ir.DeclarationReceiver
			name = childKind(parameter, "this")
		}
		if typ == nil {
			for _, child := range named(parameter) {
				if isType(child.Kind()) {
					typ = child
					break
				}
			}
		}
		if parameter.Kind() == "identifier" {
			name = parameter
		}
		if id := b.variable(parameter, name, typ, nil, dimensions, env, kind, role, variadic); id != "" {
			result = append(result, id)
		}
	}
	return result
}

func (b *builder) typeParameters(n *sitter.Node, env environment) []ir.DeclarationID {
	var ids []ir.DeclarationID
	for _, parameter := range named(n) {
		name := childKind(parameter, "type_identifier")
		if !valid(name) {
			continue
		}
		d := b.baseDeclaration(parameter, name, env, ir.DeclarationTypeParameter)
		d.TypeParameter = &ir.TypeParameterDeclaration{}
		i := b.reserve(d)
		inner := environment{env.scope, d.ID}
		b.modifiers(parameter, inner, &d)
		for _, bound := range named(childKind(parameter, "type_bound")) {
			if id := b.typeRef(bound, inner, ir.TypeUseBound, ""); id != "" {
				d.TypeParameter.BoundTypeIDs = append(d.TypeParameter.BoundTypeIDs, id)
			}
		}
		b.save(i, d)
		ids = append(ids, d.ID)
	}
	return ids
}

func (b *builder) initializer(n *sitter.Node, env environment, kind ir.InitializerKind) {
	d := b.baseDeclaration(n, nil, env, ir.DeclarationInitializer)
	d.SignatureText = ""
	d.Initializer = &ir.InitializerDeclaration{Kind: kind}
	i := b.reserve(d)
	inner := environment{owner: d.ID}
	inner.scope = b.scope(ir.ScopeInitializer, b.span(n), environment{env.scope, d.ID})
	d.BodyScopeID = inner.scope
	body := n
	if kind == ir.InitializerStatic {
		body = childKind(n, "block")
	}
	d.Initializer.BodyStatementID = b.statementBlock(body, inner)
	b.save(i, d)
}

func (b *builder) enumConstant(n *sitter.Node, env environment) {
	if !valid(field(n, "name")) {
		return
	}
	d := b.baseDeclaration(n, field(n, "name"), env, ir.DeclarationEnumConstant)
	d.Variable = &ir.VariableDeclaration{}
	i := b.reserve(d)
	inner := environment{env.scope, d.ID}
	b.modifiers(n, inner, &d)
	d.Variable.InitializerID = b.invocation(n, inner, ir.CallEnumConstant)
	b.save(i, d)
}

func (b *builder) modifiers(n *sitter.Node, env environment, d *ir.Declaration) {
	for _, child := range named(n) {
		if isAnnotation(child) {
			d.AnnotationIDs = b.appendAnnotation(d.AnnotationIDs, child, env)
		}
		if child.Kind() != "modifiers" {
			continue
		}
		for j := uint(0); j < child.ChildCount(); j++ {
			modifier := child.Child(j)
			if isAnnotation(modifier) {
				d.AnnotationIDs = b.appendAnnotation(d.AnnotationIDs, modifier, env)
			} else if !modifier.IsNamed() {
				d.Modifiers = append(d.Modifiers, ir.Modifier{Keyword: b.syntaxText(modifier), Span: b.span(modifier)})
			}
		}
	}
}
