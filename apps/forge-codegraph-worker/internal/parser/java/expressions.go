package java

import (
	"ei-aitiger-codegraph/pkg/ir"

	sitter "github.com/tree-sitter/go-tree-sitter"
)

func isExpression(kind string) bool {
	switch kind {
	case "identifier", "scoped_identifier", "this", "super", "field_access", "method_invocation", "object_creation_expression", "explicit_constructor_invocation", "method_reference", "parenthesized_expression", "cast_expression", "array_access", "array_creation_expression", "array_initializer", "element_value_array_initializer", "assignment_expression", "unary_expression", "update_expression", "binary_expression", "ternary_expression", "lambda_expression", "class_literal", "instanceof_expression", "switch_expression", "template_expression":
		return true
	}
	return literalKind(kind) != ""
}

func literalKind(kind string) ir.LiteralKind {
	switch kind {
	case "decimal_integer_literal", "hex_integer_literal", "octal_integer_literal", "binary_integer_literal":
		return ir.LiteralInteger
	case "decimal_floating_point_literal", "hex_floating_point_literal":
		return ir.LiteralFloating
	case "string_literal":
		return ir.LiteralString
	case "character_literal":
		return ir.LiteralCharacter
	case "true", "false":
		return ir.LiteralBoolean
	case "null_literal":
		return ir.LiteralNull
	}
	return ""
}

func (b *builder) expression(n *sitter.Node, env environment, access ir.AccessKind) ir.ExpressionID {
	b.check()
	if !valid(n) {
		return ""
	}
	if id := b.expressions[nodeKey(n, env)]; id != "" {
		return id
	}
	switch n.Kind() {
	case "method_invocation":
		return b.invocation(n, env, ir.CallMethod)
	case "object_creation_expression":
		return b.invocation(n, env, ir.CallObjectCreation)
	case "explicit_constructor_invocation":
		kind := ir.CallThisConstructor
		if b.syntaxText(field(n, "constructor")) == "super" {
			kind = ir.CallSuperConstructor
		}
		return b.invocation(n, env, kind)
	case "method_reference":
		return b.callableReference(n, env)
	}
	x := b.baseExpression(n, env)
	operand := func(child *sitter.Node, access ir.AccessKind) {
		if id := b.expression(child, env, access); id != "" {
			x.OperandIDs = append(x.OperandIDs, id)
		}
	}
	if kind := literalKind(n.Kind()); kind != "" {
		x.Kind, x.Literal = ir.ExpressionLiteral, &ir.Literal{Kind: kind, Lexeme: x.Spelling}
	} else {
		switch n.Kind() {
		case "identifier", "scoped_identifier":
			x.Kind = ir.ExpressionName
			name := b.name(n)
			x.Name = &name
			b.reference(n, env, x, name, "", access)
		case "this":
			x.Kind = ir.ExpressionThis
		case "super":
			x.Kind = ir.ExpressionSuper
		case "field_access":
			nameNode := field(n, "field")
			receiver := b.receiver(n, env)
			if !valid(nameNode) || receiver == "" {
				return b.unknown(n, env, "Incomplete member access")
			}
			x.Kind = ir.ExpressionMemberAccess
			name := b.name(nameNode)
			x.Name = &name
			x.OperandIDs = []ir.ExpressionID{receiver}
			if nameNode.Kind() != "this" {
				b.reference(n, env, x, name, receiver, access)
			}
		case "parenthesized_expression":
			x.Kind = ir.ExpressionParenthesized
			for _, child := range named(n) {
				operand(child, access)
			}
		case "cast_expression":
			x.Kind = ir.ExpressionCast
			var types []*sitter.Node
			for i := uint(0); i < n.ChildCount(); i++ {
				if n.FieldNameForChild(uint32(i)) == "type" {
					types = append(types, n.Child(i))
				}
			}
			x.TypeRefID = b.intersection(types, env)
			operand(field(n, "value"), ir.AccessRead)
		case "array_access":
			x.Kind = ir.ExpressionArrayAccess
			operand(field(n, "array"), ir.AccessRead)
			operand(field(n, "index"), ir.AccessRead)
		case "assignment_expression":
			x.Kind, x.Operator = ir.ExpressionAssignment, b.syntaxText(field(n, "operator"))
			leftAccess := ir.AccessReadWrite
			if x.Operator == "=" {
				leftAccess = ir.AccessWrite
			}
			operand(field(n, "left"), leftAccess)
			operand(field(n, "right"), ir.AccessRead)
		case "binary_expression":
			x.Kind, x.Operator = ir.ExpressionBinary, b.syntaxText(field(n, "operator"))
			operand(field(n, "left"), ir.AccessRead)
			operand(field(n, "right"), ir.AccessRead)
		case "unary_expression":
			x.Kind, x.Operator = ir.ExpressionUnary, b.syntaxText(field(n, "operator"))
			operand(field(n, "operand"), ir.AccessRead)
		case "update_expression":
			x.Kind = ir.ExpressionUnary
			for i := uint(0); i < n.ChildCount(); i++ {
				child := n.Child(i)
				if child.Kind() == "++" || child.Kind() == "--" {
					x.Operator = b.syntaxText(child)
				}
			}
			for _, child := range named(n) {
				operand(child, ir.AccessReadWrite)
			}
		case "ternary_expression":
			x.Kind = ir.ExpressionConditional
			operand(field(n, "condition"), ir.AccessRead)
			operand(field(n, "consequence"), ir.AccessRead)
			operand(field(n, "alternative"), ir.AccessRead)
		case "lambda_expression":
			body := field(n, "body")
			if !valid(body) {
				return b.unknown(n, env, "Incomplete lambda body")
			}
			x.Kind = ir.ExpressionLambda
			// The site occurrence is allocated directly after the expression ID,
			// exactly as invocation and callableReference do for their tables.
			site := ir.LambdaSite{Occurrence: b.occurrence(n, env), ExpressionID: x.ID}
			x.OccurrenceID = site.Occurrence.ID
			inner := environment{owner: env.owner}
			inner.scope = b.scope(ir.ScopeLambda, b.span(n), env)
			x.Lambda = &ir.Lambda{ScopeID: inner.scope}
			parameters := field(n, "parameters")
			if valid(parameters) && parameters.Kind() == "identifier" {
				if id := b.variable(parameters, parameters, nil, nil, nil, inner, ir.DeclarationParameter, ir.TypeUseParameter, false); id != "" {
					x.Lambda.ParameterIDs = []ir.DeclarationID{id}
				}
			} else {
				x.Lambda.ParameterIDs = b.parameters(parameters, inner, false)
			}
			if body.Kind() == "block" {
				x.Lambda.BodyStatementID = b.visit(body, inner)
				x.Lambda.BodyScopeID = b.file.Statements[b.statementIndex[x.Lambda.BodyStatementID]].ScopeID
			} else {
				x.Lambda.BodyExpressionID = b.expression(body, inner, ir.AccessRead)
			}
			site.ParameterIDs, site.BodyScopeID = x.Lambda.ParameterIDs, x.Lambda.BodyScopeID
			b.record()
			b.account(string(site.Occurrence.ID), site)
			b.file.Lambdas = append(b.file.Lambdas, site)
		case "array_creation_expression":
			x.Kind = ir.ExpressionArrayCreation
			x.TypeRefID = b.arrayCreationType(n, env)
			for _, child := range named(n) {
				if child.Kind() == "dimensions_expr" {
					for _, value := range named(child) {
						if !isAnnotation(value) {
							operand(value, ir.AccessRead)
						}
					}
				}
			}
			operand(field(n, "value"), ir.AccessRead)
		case "array_initializer", "element_value_array_initializer":
			x.Kind = ir.ExpressionArrayInitializer
			for _, child := range named(n) {
				operand(child, ir.AccessRead)
			}
		case "class_literal":
			x.Kind = ir.ExpressionClassLiteral
			for _, child := range named(n) {
				if isType(child.Kind()) {
					x.TypeRefID = b.typeRef(child, env, ir.TypeUseClassLiteral, "")
					break
				}
			}
		case "instanceof_expression":
			x.Kind, x.Operator = ir.ExpressionInstanceOf, "instanceof"
			operand(field(n, "left"), ir.AccessRead)
			if field(n, "name") != nil {
				x.PatternID = b.pattern(n, env)
			} else if pattern := field(n, "pattern"); pattern != nil {
				x.PatternID = b.pattern(pattern, env)
			}
			if x.PatternID != "" {
				x.TypeRefID = b.file.Patterns[b.patternIndex[x.PatternID]].TypeRefID
			} else {
				x.TypeRefID = b.typeRef(field(n, "right"), env, ir.TypeUsePattern, "")
			}
		case "switch_expression":
			x.Kind = ir.ExpressionSwitch
			operand(field(n, "condition"), ir.AccessRead)
			body := b.visit(field(n, "body"), env)
			if body == "" {
				return b.unknown(n, env, "Incomplete switch body")
			}
			x.Switch = &ir.Switch{BodyStatementID: body}
		default:
			return b.unknown(n, env, "Unsupported expression syntax: "+n.Kind())
		}
	}
	return b.saveExpression(n, env, x)
}

func (b *builder) baseExpression(n *sitter.Node, env environment) ir.Expression {
	return ir.Expression{ID: ir.ExpressionID(b.id("expr", n)), Span: b.span(n), ScopeID: env.scope, Spelling: ir.BoundSpelling(b.spelling(n))}
}
func (b *builder) saveExpression(n *sitter.Node, env environment, x ir.Expression) ir.ExpressionID {
	b.record()
	b.account(string(x.ID), x)
	b.file.Expressions = append(b.file.Expressions, x)
	b.expressions[nodeKey(n, env)] = x.ID
	return x.ID
}
func (b *builder) unknown(n *sitter.Node, env environment, message string) ir.ExpressionID {
	if !valid(n) {
		return ""
	}
	if id := b.expressions[nodeKey(n, env)]; id != "" {
		return id
	}
	x := b.baseExpression(n, env)
	x.Kind, x.SyntaxKind = ir.ExpressionUnknown, n.Kind()
	b.issue(n, "expression_syntax", message, ir.CoverageUnsupported)
	if isAnnotation(n) {
		b.annotation(n, env)
	}
	return b.saveExpression(n, env, x)
}
func (b *builder) reference(n *sitter.Node, env environment, x ir.Expression, name ir.Name, receiver ir.ExpressionID, access ir.AccessKind) {
	if len(name.Segments) == 0 {
		return
	}
	r := ir.Reference{Occurrence: b.occurrence(n, env), Kind: ir.ReferenceName, ExpressionID: x.ID, Name: name, ReceiverID: receiver, Access: access}
	if receiver != "" {
		r.Kind = ir.ReferenceMember
	}
	b.record()
	b.account(string(r.Occurrence.ID), r)
	b.file.References = append(b.file.References, r)
}

// Java's qualified-super production puts `super` beside, not inside, object.
// Preserve Outer.super as a full receiver expression instead of dropping super.
func (b *builder) receiver(n *sitter.Node, env environment) ir.ExpressionID {
	object := field(n, "object")
	if !valid(object) {
		return ""
	}
	id := b.expression(object, env, ir.AccessRead)
	if n.Kind() != "field_access" && n.Kind() != "method_invocation" {
		return id
	}
	for _, child := range named(n) {
		if child.Kind() == "super" && child.Id() != object.Id() {
			x := b.baseExpression(child, env)
			x.Kind = ir.ExpressionSuper
			x.Span, x.Spelling = b.spanBytes(object.StartByte(), child.EndByte()), b.rawSlice(object.StartByte(), child.EndByte())
			x.OperandIDs = []ir.ExpressionID{id}
			return b.saveExpression(child, env, x)
		}
	}
	return id
}

func (b *builder) invocation(n *sitter.Node, env environment, kind ir.CallKind) ir.ExpressionID {
	if id := b.expressions[nodeKey(n, env)]; id != "" {
		return id
	}
	if kind == ir.CallMethod && !valid(field(n, "name")) {
		return b.unknown(n, env, "Incomplete method invocation")
	}
	if kind == ir.CallObjectCreation && !valid(field(n, "type")) {
		return b.unknown(n, env, "Incomplete constructor type")
	}
	x := b.baseExpression(n, env)
	x.Kind = ir.ExpressionCall
	c := ir.Call{Occurrence: b.occurrence(n, env), Kind: kind, ExpressionID: x.ID, Arguments: []ir.Argument{}}
	x.OccurrenceID = c.Occurrence.ID
	c.Name = b.syntaxText(field(n, "name"))
	c.ReceiverID = b.receiver(n, env)
	if kind == ir.CallThisConstructor {
		c.Name = "this"
	}
	if kind == ir.CallSuperConstructor {
		c.Name = "super"
	}
	if kind == ir.CallObjectCreation {
		c.ConstructedTypeID = b.typeRef(field(n, "type"), env, ir.TypeUseConstruction, "")
		b.decorateType(c.ConstructedTypeID, n, env)
		// The qualifier in primary.new Inner() has no named field in this grammar.
		for i := uint(0); i < n.ChildCount(); i++ {
			child := n.Child(i)
			if child.Kind() == "new" {
				break
			}
			if child.IsNamed() && isExpression(child.Kind()) {
				c.ReceiverID = b.expression(child, env, ir.AccessRead)
			}
		}
	}
	c.TypeArgumentIDs = b.typeArguments(field(n, "type_arguments"), env)
	for _, argument := range named(field(n, "arguments")) {
		if id := b.expression(argument, env, ir.AccessRead); id != "" {
			c.Arguments = append(c.Arguments, ir.Argument{ExpressionID: id})
		}
	}
	if body := childKind(n, "class_body"); body != nil {
		c.AnonymousTypeDeclarationID = b.typeDeclaration(body, env, ir.DeclarationClass, true)
	}
	b.record()
	b.account(string(c.Occurrence.ID), c)
	b.file.Calls = append(b.file.Calls, c)
	return b.saveExpression(n, env, x)
}

func (b *builder) typeArguments(n *sitter.Node, env environment) []ir.TypeRefID {
	var ids []ir.TypeRefID
	for _, argument := range named(n) {
		if id := b.typeRef(argument, env, ir.TypeUseGenericArgument, ""); id != "" {
			ids = append(ids, id)
		}
	}
	return ids
}

func (b *builder) callableReference(n *sitter.Node, env environment) ir.ExpressionID {
	var qualifier, name, arguments *sitter.Node
	afterColon := false
	for i := uint(0); i < n.ChildCount(); i++ {
		child := n.Child(i)
		if child.Kind() == "::" {
			afterColon = true
			continue
		}
		if !afterColon && child.IsNamed() && child.Kind() != "line_comment" && child.Kind() != "block_comment" {
			qualifier = child
		}
		if afterColon && child.Kind() == "type_arguments" {
			arguments = child
		} else if afterColon && (child.Kind() == "identifier" || child.Kind() == "new") {
			name = child
		}
	}
	if !valid(qualifier) || !valid(name) {
		return b.unknown(n, env, "Incomplete callable reference")
	}
	x := b.baseExpression(n, env)
	x.Kind = ir.ExpressionCallableReference
	r := ir.CallableReference{Occurrence: b.occurrence(n, env), Kind: ir.CallableMethodReference, ExpressionID: x.ID, Name: b.syntaxText(name)}
	x.OccurrenceID = r.Occurrence.ID
	if r.Name == "new" {
		r.Kind = ir.CallableConstructorReference
	}
	if b.qualifiedSuper(qualifier) {
		q := b.baseExpression(qualifier, env)
		q.Kind = ir.ExpressionSuper
		prefix := field(qualifier, "scope")
		if prefix == nil {
			parts := named(qualifier)
			if len(parts) > 1 {
				prefix = parts[0]
			}
		}
		if valid(prefix) {
			q.TypeRefID = b.typeRef(prefix, env, ir.TypeUseCallableReference, "")
		}
		r.QualifierID = b.saveExpression(qualifier, env, q)
	} else if isType(qualifier.Kind()) {
		q := b.baseExpression(qualifier, env)
		q.Kind = ir.ExpressionName
		q.TypeRefID = b.typeRef(qualifier, env, ir.TypeUseCallableReference, "")
		// TypeRefID preserves array/generic syntax without guessing a value binding.
		r.QualifierID = b.saveExpression(qualifier, env, q)
	} else {
		r.QualifierID = b.expression(qualifier, env, ir.AccessRead)
	}
	r.TypeArgumentIDs = b.typeArguments(arguments, env)
	b.record()
	b.account(string(r.Occurrence.ID), r)
	b.file.CallableReferences = append(b.file.CallableReferences, r)
	return b.saveExpression(n, env, x)
}

func (b *builder) decorateType(id ir.TypeRefID, n *sitter.Node, env environment) {
	var annotations []ir.AnnotationID
	for _, child := range named(n) {
		if isAnnotation(child) {
			annotations = b.appendAnnotation(annotations, child, env)
		}
	}
	if len(annotations) == 0 {
		return
	}
	for i := len(b.file.Types) - 1; i >= 0; i-- {
		if b.file.Types[i].ID == id {
			b.file.Types[i].AnnotationIDs = append(b.file.Types[i].AnnotationIDs, annotations...)
			b.account(string(id), b.file.Types[i])
			return
		}
	}
}

func (b *builder) arrayCreationType(n *sitter.Node, env environment) ir.TypeRefID {
	typ := field(n, "type")
	if !valid(typ) {
		return ""
	}
	t := ir.TypeRef{ID: ir.TypeRefID(b.id("type", n)), Kind: ir.TypeArray, ScopeID: env.scope}
	t.ElementTypeID = b.typeRef(typ, env, ir.TypeUseConstruction, t.ID)
	end := typ.EndByte()
	for _, child := range named(n) {
		if child.Kind() == "dimensions" || child.Kind() == "dimensions_expr" {
			t.Dimensions = append(t.Dimensions, b.dimensions(child, env)...)
			end = child.EndByte()
		}
		if isAnnotation(child) {
			t.AnnotationIDs = b.appendAnnotation(t.AnnotationIDs, child, env)
		}
	}
	if len(t.Dimensions) == 0 {
		t.Kind, t.ElementTypeID = ir.TypeUnknown, ""
		b.issue(n, "array_dimensions", "Incomplete array dimensions", ir.CoverageParseError)
	}
	t.Span, t.Spelling = b.spanBytes(typ.StartByte(), end), b.rawSlice(typ.StartByte(), end)
	b.record()
	b.account(string(t.ID), t)
	b.file.Types = append(b.file.Types, t)
	b.typeUse(t, env, ir.TypeUseConstruction, "")
	return t.ID
}

func (b *builder) qualifiedSuper(n *sitter.Node) bool {
	children := named(n)
	return len(children) > 1 && b.syntaxText(children[len(children)-1]) == "super"
}
