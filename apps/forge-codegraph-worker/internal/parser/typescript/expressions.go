package typescript

import (
	"strings"

	"ei-aitiger-codegraph/pkg/ir"

	sitter "github.com/tree-sitter/go-tree-sitter"
)

func isExpression(kind string) bool {
	switch kind {
	case "identifier", "shorthand_property_identifier", "this", "super", "member_expression", "call_expression", "new_expression",
		"string", "template_string", "number", "true", "false", "null", "undefined", "regex",
		"arrow_function", "function_expression", "generator_function", "class",
		"as_expression", "satisfies_expression", "type_assertion", "non_null_expression", "parenthesized_expression",
		"assignment_expression", "augmented_assignment_expression", "binary_expression", "unary_expression", "update_expression",
		"ternary_expression", "await_expression", "yield_expression", "array", "object", "subscript_expression", "spread_element",
		"jsx_element", "jsx_self_closing_element", "jsx_fragment", "sequence_expression", "instantiation_expression", "meta_property", "import":
		return true
	}
	return false
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

// opaque retains syntax this adapter does not model as a dependency fact.
// It is not a coverage loss: the feature set declares such operands opaque.
func (b *builder) opaque(n *sitter.Node, env environment) ir.ExpressionID {
	x := b.baseExpression(n, env)
	x.Kind, x.SyntaxKind = ir.ExpressionUnknown, n.Kind()
	b.issue(n, "expression_syntax", "Unsupported expression syntax "+n.Kind(), ir.CoverageUnsupported)
	return b.saveExpression(n, env, x)
}

func (b *builder) reference(n *sitter.Node, env environment, x ir.Expression, name ir.Name, receiver ir.ExpressionID, access ir.AccessKind) {
	r := ir.Reference{Occurrence: b.occurrence(n, env), Kind: ir.ReferenceName, ExpressionID: x.ID, Name: name, ReceiverID: receiver, Access: access}
	if receiver != "" {
		r.Kind = ir.ReferenceMember
	}
	b.record()
	b.account(string(r.Occurrence.ID), r)
	b.file.References = append(b.file.References, r)
}

func (b *builder) simpleName(n *sitter.Node) ir.Name {
	span := b.span(n)
	return ir.Name{Segments: []ir.NameSegment{{Text: b.spelling(n), Span: &span}}}
}

func literalKind(kind, lexeme string) ir.LiteralKind {
	switch kind {
	case "string", "template_string":
		return ir.LiteralString
	case "number":
		lower := strings.ToLower(lexeme)
		if !strings.HasPrefix(lower, "0x") && !strings.HasPrefix(lower, "0b") && !strings.HasPrefix(lower, "0o") && strings.ContainsAny(lower, ".e") {
			return ir.LiteralFloating
		}
		return ir.LiteralInteger
	case "true", "false":
		return ir.LiteralBoolean
	case "null", "undefined":
		return ir.LiteralNull
	case "regex":
		// A regular expression literal is retained as a string literal
		// whose lexeme is the written pattern and flags.
		return ir.LiteralString
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
	case "call_expression":
		return b.call(n, env)
	case "new_expression":
		return b.construction(n, env)
	case "arrow_function", "function_expression", "generator_function", "method_definition":
		return b.lambda(n, env)
	case "class":
		b.class(n, env, !valid(field(n, "name")))
		x := b.baseExpression(n, env)
		x.Kind, x.SyntaxKind = ir.ExpressionClassLiteral, "class"
		return b.saveExpression(n, env, x)
	case "instantiation_expression":
		// expr<T> as a value: the type arguments are recorded and the
		// expression is the instantiated one.
		b.typeArgumentsOf(field(n, "type_arguments"), env, "")
		return b.expression(field(n, "function"), env, access)
	}
	x := b.baseExpression(n, env)
	operand := func(child *sitter.Node, access ir.AccessKind) {
		if id := b.expression(child, env, access); id != "" {
			x.OperandIDs = append(x.OperandIDs, id)
		}
	}
	if kind := literalKind(n.Kind(), x.Spelling); kind != "" {
		x.Kind, x.Literal = ir.ExpressionLiteral, &ir.Literal{Kind: kind, Lexeme: x.Spelling}
		if n.Kind() == "template_string" {
			for _, child := range named(n) {
				if child.Kind() == "template_substitution" {
					b.expression(firstNamed(child), env, ir.AccessRead)
				}
			}
		}
		return b.saveExpression(n, env, x)
	}
	switch n.Kind() {
	case "identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern":
		x.Kind = ir.ExpressionName
		name := b.simpleName(n)
		x.Name = &name
		b.reference(n, env, x, name, "", access)
	case "array_pattern":
		// A destructuring assignment target: its elements are written.
		x.Kind = ir.ExpressionArrayInitializer
		for _, child := range named(n) {
			operand(child, ir.AccessWrite)
		}
	case "object_pattern":
		x.Kind, x.SyntaxKind = ir.ExpressionObjectLiteral, n.Kind()
		for _, child := range named(n) {
			switch child.Kind() {
			case "shorthand_property_identifier_pattern":
				operand(child, ir.AccessWrite)
			case "pair_pattern":
				operand(field(child, "value"), ir.AccessWrite)
			case "rest_pattern":
				operand(firstNamed(child), ir.AccessWrite)
			case "object_assignment_pattern":
				operand(field(child, "left"), ir.AccessWrite)
				operand(field(child, "right"), ir.AccessRead)
			}
		}
	case "assignment_pattern":
		x.Kind, x.Operator = ir.ExpressionAssignment, "="
		operand(field(n, "left"), ir.AccessWrite)
		operand(field(n, "right"), ir.AccessRead)
	case "rest_pattern":
		x.Kind, x.Operator = ir.ExpressionUnary, "..."
		operand(firstNamed(n), access)
	case "this":
		x.Kind = ir.ExpressionThis
	case "super":
		x.Kind = ir.ExpressionSuper
	case "member_expression", "nested_identifier":
		object, property := field(n, "object"), field(n, "property")
		if n.Kind() == "nested_identifier" {
			if parts := named(n); len(parts) == 2 {
				object, property = parts[0], parts[1]
			}
		}
		receiver := b.expression(object, env, ir.AccessRead)
		if !valid(property) || receiver == "" {
			return b.opaque(n, env)
		}
		x.Kind = ir.ExpressionMemberAccess
		name := b.simpleName(property)
		x.Name = &name
		x.OperandIDs = []ir.ExpressionID{receiver}
		b.reference(n, env, x, name, receiver, access)
	case "parenthesized_expression", "non_null_expression":
		x.Kind = ir.ExpressionParenthesized
		for _, child := range named(n) {
			if child.Kind() == "type_annotation" {
				x.TypeRefID = b.typeRef(child, env, ir.TypeUseCast, "")
			} else {
				operand(child, access)
			}
		}
	case "as_expression", "satisfies_expression", "type_assertion":
		x.Kind = ir.ExpressionCast
		for _, child := range named(n) {
			if isExpression(child.Kind()) {
				operand(child, ir.AccessRead)
			} else if child.Kind() == "type_arguments" {
				if types := named(child); len(types) > 0 {
					x.TypeRefID = b.typeRef(types[0], env, ir.TypeUseCast, "")
				}
			} else {
				x.TypeRefID = b.typeRef(child, env, ir.TypeUseCast, "")
			}
		}
	case "assignment_expression":
		x.Kind, x.Operator = ir.ExpressionAssignment, "="
		operand(field(n, "left"), ir.AccessWrite)
		operand(field(n, "right"), ir.AccessRead)
	case "augmented_assignment_expression":
		x.Kind, x.Operator = ir.ExpressionAssignment, b.spelling(field(n, "operator"))
		operand(field(n, "left"), ir.AccessReadWrite)
		operand(field(n, "right"), ir.AccessRead)
	case "binary_expression":
		x.Kind, x.Operator = ir.ExpressionBinary, b.spelling(field(n, "operator"))
		operand(field(n, "left"), ir.AccessRead)
		operand(field(n, "right"), ir.AccessRead)
	case "unary_expression":
		x.Kind, x.Operator = ir.ExpressionUnary, b.spelling(field(n, "operator"))
		operand(field(n, "argument"), ir.AccessRead)
	case "update_expression":
		x.Kind = ir.ExpressionUnary
		for i := uint(0); i < n.ChildCount(); i++ {
			if child := n.Child(i); !child.IsNamed() {
				x.Operator = b.spelling(child)
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
	case "await_expression", "yield_expression":
		x.Kind, x.Operator = ir.ExpressionUnary, strings.TrimSuffix(n.Kind(), "_expression")
		for _, child := range named(n) {
			operand(child, ir.AccessRead)
		}
	case "spread_element":
		x.Kind, x.Operator = ir.ExpressionUnary, "..."
		operand(firstNamed(n), ir.AccessRead)
	case "array":
		x.Kind = ir.ExpressionArrayInitializer
		for _, child := range named(n) {
			operand(child, ir.AccessRead)
		}
	case "subscript_expression":
		x.Kind = ir.ExpressionArrayAccess
		operand(field(n, "object"), ir.AccessRead)
		operand(field(n, "index"), ir.AccessRead)
	case "object":
		x.Kind, x.SyntaxKind = ir.ExpressionObjectLiteral, n.Kind()
		for _, child := range named(n) {
			switch child.Kind() {
			case "pair":
				if key := field(child, "key"); key != nil && key.Kind() == "computed_property_name" {
					operand(firstNamed(key), ir.AccessRead)
				}
				operand(field(child, "value"), ir.AccessRead)
			case "shorthand_property_identifier", "spread_element", "method_definition":
				operand(child, ir.AccessRead)
			}
		}
	case "jsx_element", "jsx_fragment":
		x.Kind, x.SyntaxKind = ir.ExpressionMarkup, n.Kind()
		if open := field(n, "open_tag"); open != nil {
			x.OperandIDs = append(x.OperandIDs, b.jsxTag(open, env)...)
		}
		for _, child := range named(n) {
			switch child.Kind() {
			case "jsx_expression":
				operand(firstNamed(child), ir.AccessRead)
			case "jsx_element", "jsx_self_closing_element", "jsx_fragment":
				operand(child, ir.AccessRead)
			}
		}
	case "jsx_self_closing_element":
		x.Kind, x.SyntaxKind = ir.ExpressionMarkup, n.Kind()
		x.OperandIDs = append(x.OperandIDs, b.jsxTag(n, env)...)
	case "sequence_expression":
		x.Kind, x.Operator = ir.ExpressionBinary, ","
		for _, child := range named(n) {
			operand(child, ir.AccessRead)
		}
	case "meta_property", "import":
		// import.meta, new.target and a dynamic import callee name no
		// declaration, so no reference is recorded.
		x.Kind = ir.ExpressionName
		name := b.simpleName(n)
		x.Name = &name
	default:
		x.Kind, x.SyntaxKind = ir.ExpressionUnknown, n.Kind()
		b.issue(n, "expression_syntax", "Unsupported expression syntax "+n.Kind(), ir.CoverageUnsupported)
		for _, child := range named(n) {
			if isExpression(child.Kind()) {
				operand(child, ir.AccessRead)
			}
		}
	}
	return b.saveExpression(n, env, x)
}

// jsxTag references the component a JSX tag names (capitalized or
// qualified names; lowercase tags are intrinsic elements) and extracts
// attribute expressions.
func (b *builder) jsxTag(tag *sitter.Node, env environment) []ir.ExpressionID {
	var ids []ir.ExpressionID
	add := func(n *sitter.Node) {
		if id := b.expression(n, env, ir.AccessRead); id != "" {
			ids = append(ids, id)
		}
	}
	if name := field(tag, "name"); valid(name) {
		text := b.spelling(name)
		if name.Kind() == "member_expression" || name.Kind() == "nested_identifier" || (name.Kind() == "identifier" && text != "" && strings.ToUpper(text[:1]) == text[:1] && strings.ToLower(text[:1]) != text[:1]) {
			add(name)
		}
	}
	for i := uint(0); i < tag.ChildCount(); i++ {
		child := tag.Child(i)
		switch child.Kind() {
		case "jsx_attribute":
			for _, value := range named(child) {
				if value.Kind() == "jsx_expression" {
					add(firstNamed(value))
				} else if value.Kind() == "jsx_element" || value.Kind() == "jsx_self_closing_element" {
					add(value)
				}
			}
		case "jsx_expression":
			add(firstNamed(child))
		}
	}
	b.typeArgumentsOf(field(tag, "type_arguments"), env, "")
	return ids
}

// call records a call expression. The callee's last name is the call name;
// a member callee's object is the receiver, and super(...) is the base
// constructor call.
func (b *builder) call(n *sitter.Node, env environment) ir.ExpressionID {
	x := b.baseExpression(n, env)
	x.Kind = ir.ExpressionCall
	c := ir.Call{Occurrence: b.occurrence(n, env), Kind: ir.CallMethod, ExpressionID: x.ID, Arguments: []ir.Argument{}}
	x.OccurrenceID = c.Occurrence.ID
	callee := field(n, "function")
	// The grammar attaches a preceding await to the callee of a call with
	// explicit type arguments; the await applies to the call's result.
	for callee != nil && callee.Kind() == "await_expression" {
		callee = firstNamed(callee)
	}
	if callee != nil && callee.Kind() == "instantiation_expression" {
		c.TypeArgumentIDs = b.typeArgumentsOf(field(callee, "type_arguments"), env, "")
		callee = field(callee, "function")
	}
	switch {
	case !valid(callee):
		c.Name = "()"
	case callee.Kind() == "identifier":
		c.Name = b.spelling(callee)
	case callee.Kind() == "member_expression":
		c.Name = b.spelling(field(callee, "property"))
		c.ReceiverID = b.expression(field(callee, "object"), env, ir.AccessRead)
	case callee.Kind() == "super":
		c.Name = "constructor"
		c.ReceiverID = b.expression(callee, env, ir.AccessRead)
	case callee.Kind() == "import":
		c.Name = "import"
	default:
		// A computed callee (a call result, an element, a parenthesized
		// expression) is the receiver of an unnamed invocation.
		c.Name = "()"
		c.ReceiverID = b.expression(callee, env, ir.AccessRead)
	}
	if ids := b.typeArgumentsOf(field(n, "type_arguments"), env, ""); len(ids) > 0 {
		c.TypeArgumentIDs = ids
	}
	for _, argument := range b.argumentNodes(field(n, "arguments")) {
		if id := b.expression(argument, env, ir.AccessRead); id != "" {
			c.Arguments = append(c.Arguments, ir.Argument{ExpressionID: id})
		}
	}
	b.record()
	b.account(string(c.Occurrence.ID), c)
	b.file.Calls = append(b.file.Calls, c)
	return b.saveExpression(n, env, x)
}

// argumentNodes lists call arguments; a tagged template's template string
// is the single argument.
func (b *builder) argumentNodes(arguments *sitter.Node) []*sitter.Node {
	if arguments == nil {
		return nil
	}
	if arguments.Kind() == "template_string" {
		return []*sitter.Node{arguments}
	}
	return named(arguments)
}

// construction records new X(...) with the constructed type written as a
// name chain.
func (b *builder) construction(n *sitter.Node, env environment) ir.ExpressionID {
	x := b.baseExpression(n, env)
	x.Kind = ir.ExpressionCall
	c := ir.Call{Occurrence: b.occurrence(n, env), Kind: ir.CallObjectCreation, ExpressionID: x.ID, Arguments: []ir.Argument{}}
	x.OccurrenceID = c.Occurrence.ID
	constructor := field(n, "constructor")
	c.ConstructedTypeID = b.typeFromExpression(constructor, env, ir.TypeUseConstruction)
	if c.ConstructedTypeID == "" {
		c.ConstructedTypeID = b.computedType(constructor, env, ir.TypeUseConstruction)
		b.expression(constructor, env, ir.AccessRead)
	}
	c.TypeArgumentIDs = b.typeArgumentsOf(field(n, "type_arguments"), env, "")
	for _, argument := range b.argumentNodes(field(n, "arguments")) {
		if id := b.expression(argument, env, ir.AccessRead); id != "" {
			c.Arguments = append(c.Arguments, ir.Argument{ExpressionID: id})
		}
	}
	b.record()
	b.account(string(c.Occurrence.ID), c)
	b.file.Calls = append(b.file.Calls, c)
	return b.saveExpression(n, env, x)
}

// lambda records a function literal as a lambda site with its own scope,
// parameters and body.
func (b *builder) lambda(n *sitter.Node, env environment) ir.ExpressionID {
	x := b.baseExpression(n, env)
	x.Kind = ir.ExpressionLambda
	site := ir.LambdaSite{Occurrence: b.occurrence(n, env), ExpressionID: x.ID}
	x.OccurrenceID = site.Occurrence.ID
	inner := environment{owner: env.owner}
	inner.scope = b.scope(ir.ScopeLambda, b.span(n), env)
	x.Lambda = &ir.Lambda{ScopeID: inner.scope}
	if single := field(n, "parameter"); valid(single) {
		if id := b.variable(single, single, nil, nil, inner, ir.DeclarationParameter, ir.TypeUseParameter, ""); id != "" {
			x.Lambda.ParameterIDs = []ir.DeclarationID{id}
		}
	} else {
		x.Lambda.ParameterIDs = b.parameters(field(n, "parameters"), inner)
	}
	b.typeParameters(field(n, "type_parameters"), inner)
	b.returnType(field(n, "return_type"), inner)
	body := field(n, "body")
	switch {
	case !valid(body):
		return b.opaque(n, env)
	case body.Kind() == "statement_block":
		s := ir.Statement{ID: ir.StatementID(b.id("stmt", body)), Kind: ir.StatementBlock, Span: b.span(body), ScopeID: inner.scope, EnclosingDeclarationID: env.owner}
		b.record()
		b.account(string(s.ID), s)
		b.file.Statements = append(b.file.Statements, s)
		x.Lambda.BodyStatementID, x.Lambda.BodyScopeID = s.ID, inner.scope
		b.nodes(body, inner)
	default:
		x.Lambda.BodyExpressionID = b.expression(body, inner, ir.AccessRead)
		if x.Lambda.BodyExpressionID == "" {
			return b.opaque(n, env)
		}
	}
	site.ParameterIDs, site.BodyScopeID = x.Lambda.ParameterIDs, x.Lambda.BodyScopeID
	b.record()
	b.account(string(site.Occurrence.ID), site)
	b.file.Lambdas = append(b.file.Lambdas, site)
	return b.saveExpression(n, env, x)
}
