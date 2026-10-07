package python

import (
	"strings"
	"unicode"

	"ei-aitiger-codegraph/pkg/ir"
	sitter "github.com/tree-sitter/go-tree-sitter"
)

func (b *builder) program(n *sitter.Node) { b.walk(n, environment{scope: b.file.RootScopeID}) }

func (b *builder) addDeclaration(d ir.Declaration) ir.DeclarationID {
	b.record()
	b.declarationIndex[d.ID] = len(b.file.Declarations)
	b.file.Declarations = append(b.file.Declarations, d)
	b.account(string(d.ID), d)
	return d.ID
}
func (b *builder) update(d ir.Declaration) {
	b.file.Declarations[b.declarationIndex[d.ID]] = d
	b.account(string(d.ID), d)
}
func (b *builder) name(n *sitter.Node) ir.Name {
	s := b.span(n)
	return ir.Name{Segments: []ir.NameSegment{{Text: b.spelling(n), Span: &s}}}
}
func (b *builder) decl(n, name *sitter.Node, env environment, kind ir.DeclarationKind) ir.Declaration {
	s := b.span(name)
	return ir.Declaration{ID: ir.DeclarationID(b.id("decl", name)), Name: b.spelling(name), NameSpan: &s,
		Span: b.span(n), Kind: kind, DeclaringScopeID: env.scope, OwnerID: env.owner}
}

// walk follows statements without inventing block scopes. The analyzer owns
// flow and binding rules; executable expressions remain linked syntax facts.
func (b *builder) walk(n *sitter.Node, env environment) {
	if !valid(n) {
		return
	}
	b.check()
	switch n.Kind() {
	case "function_definition", "class_definition":
		b.definition(n, env, nil)
	case "decorated_definition":
		var decorators []*sitter.Node
		for _, c := range named(n) {
			if c.Kind() == "decorator" {
				decorators = append(decorators, firstNamed(c))
			}
		}
		b.definition(field(n, "definition"), env, decorators)
	case "import_statement", "import_from_statement", "future_import_statement":
		b.imports(n, env)
	case "assignment", "augmented_assignment", "named_expression":
		b.assignment(n, env)
	case "for_statement", "for_in_clause":
		b.expression(field(n, "right"), env, ir.AccessRead)
		b.bindTarget(field(n, "left"), env, "", "")
		for _, c := range named(n) {
			if !same(c, field(n, "left")) && !same(c, field(n, "right")) {
				b.walk(c, env)
			}
		}
	case "with_item":
		for _, c := range named(n) {
			if c.Kind() == "as_pattern" {
				for _, v := range named(c) {
					if v.Kind() == "as_pattern_target" {
						b.bindTarget(v, env, "", "")
					} else {
						b.expression(v, env, ir.AccessRead)
					}
				}
			} else {
				b.expression(c, env, ir.AccessRead)
			}
		}
	case "except_clause":
		for _, c := range named(n) {
			if c.Kind() == "as_pattern" {
				for _, v := range named(c) {
					if v.Kind() == "as_pattern_target" {
						b.bindTarget(v, env, "", "")
					} else {
						b.expression(v, env, ir.AccessRead)
					}
				}
			} else {
				b.walk(c, env)
			}
		}
	case "global_statement", "nonlocal_statement":
		// Preserve the directive without treating its names as runtime reads.
		b.structural(n, env, nil)
	case "delete_statement":
		for _, c := range named(n) {
			b.expression(c, env, ir.AccessWrite)
		}
	case "type_alias_statement":
		left := field(n, "left")
		name := left
		for valid(name) && name.Kind() != "identifier" {
			name = firstNamed(name)
		}
		if valid(name) {
			d := b.decl(n, name, env, ir.DeclarationTypeAlias)
			d.Type = &ir.TypeDeclaration{Form: ir.TypeTopLevel}
			b.addDeclaration(d)
			b.typeRef(field(n, "right"), env, ir.TypeUseAlias)
			if strings.Contains(b.spelling(left), "[") {
				b.issue(left, "alias_parameters", "generic type-alias parameter extraction is not supported", ir.CoverageUnsupported)
			}
		}
		if b.input.Source.LanguageVersion < "3.12" {
			b.issue(n, "version", "type statements require Python 3.12", ir.CoverageUnsupported)
		}
	case "module", "block", "expression_statement", "if_statement", "elif_clause", "else_clause", "while_statement", "try_statement", "finally_clause", "with_statement", "with_clause", "return_statement", "raise_statement", "assert_statement", "match_statement", "case_clause":
		for _, c := range named(n) {
			b.walk(c, env)
		}
	case "pass_statement", "break_statement", "continue_statement", "comment":
	case "case_pattern":
		b.pattern(n, env)
	default:
		b.expression(n, env, ir.AccessRead)
	}
}
func same(a, c *sitter.Node) bool { return a != nil && c != nil && a.Id() == c.Id() }

func (b *builder) definition(n *sitter.Node, env environment, decorators []*sitter.Node) {
	if !valid(n) || !valid(field(n, "name")) {
		return
	}
	name := field(n, "name")
	kind := ir.DeclarationFunction
	if n.Kind() == "class_definition" {
		kind = ir.DeclarationClass
	} else if i, ok := b.declarationIndex[env.owner]; ok && b.file.Declarations[i].Kind == ir.DeclarationClass {
		kind = ir.DeclarationMethod
	}
	d := b.decl(n, name, env, kind)
	if kind == ir.DeclarationClass {
		d.Type = &ir.TypeDeclaration{Form: ir.TypeTopLevel}
	} else {
		d.Callable = &ir.CallableDeclaration{}
	}
	if strings.HasPrefix(b.spelling(n), "async ") {
		d.Modifiers = append(d.Modifiers, ir.Modifier{Keyword: "async", Span: b.rawSpan(n.StartByte(), n.StartByte()+5)})
	}
	body := field(n, "body")
	if c := firstNamed(body); c != nil && c.Kind() == "expression_statement" {
		if text := firstNamed(c); text != nil && text.Kind() == "string" {
			d.DocComment = b.spelling(text)
		}
	}
	b.addDeclaration(d)
	for _, c := range decorators {
		d.DecoratorExpressionIDs = append(d.DecoratorExpressionIDs, b.expression(c, env, ir.AccessRead))
	}
	scopeKind := ir.ScopeCallable
	if kind == ir.DeclarationClass {
		scopeKind = ir.ScopeType
	}
	inner := environment{scope: b.scope(scopeKind, b.span(n), environment{scope: env.scope, owner: d.ID}), owner: d.ID}
	d.BodyScopeID = inner.scope
	if params := field(n, "type_parameters"); valid(params) {
		var ids []ir.DeclarationID
		for _, param := range named(params) {
			name := param
			for valid(name) && name.Kind() != "identifier" {
				name = firstNamed(name)
			}
			if valid(name) {
				td := b.decl(param, name, inner, ir.DeclarationTypeParameter)
				td.TypeParameter = &ir.TypeParameterDeclaration{}
				if strings.TrimLeft(b.spelling(param), "*") != b.spelling(name) {
					b.issue(param, "type_parameter_bounds", "generic parameter bounds and defaults are not fully extracted", ir.CoverageUnsupported)
				}
				ids = append(ids, b.addDeclaration(td))
			}
		}
		if d.Type != nil {
			d.Type.TypeParameterIDs = ids
		} else {
			d.Callable.TypeParameterIDs = ids
		}
		if b.input.Source.LanguageVersion < "3.12" {
			b.issue(params, "version", "generic parameter lists require Python 3.12", ir.CoverageUnsupported)
		}
	}
	if kind == ir.DeclarationClass {
		// Base expressions are resolved in the enclosing scope, but the
		// inheritance belongs to the class: its heritage occurrences name the
		// class as their enclosing declaration, as the Java parser does, so
		// the projected inherits edge starts at the subclass rather than at
		// the module or the function that defines it.
		heritage := environment{scope: env.scope, owner: d.ID}
		for _, c := range named(field(n, "superclasses")) {
			if c.Kind() == "keyword_argument" {
				b.expression(field(c, "value"), env, ir.AccessRead)
				continue
			}
			t := b.typeRef(c, heritage, ir.TypeUseHeritage)
			if t != "" {
				d.Type.Heritage = append(d.Type.Heritage, ir.Heritage{Kind: ir.HeritageExtends, TypeRefID: t, Span: b.span(c)})
			}
		}
	} else {
		d.Callable.ParameterIDs = b.parameters(field(n, "parameters"), inner, env)
		d.Callable.ReturnTypeID = b.typeRef(field(n, "return_type"), inner, ir.TypeUseReturn)
	}
	b.update(d)
	b.walk(body, inner)
}

func (b *builder) parameters(n *sitter.Node, inner, outer environment) []ir.DeclarationID {
	var ids []ir.DeclarationID
	keywordOnly := false
	for _, c := range named(n) {
		if c.Kind() == "positional_separator" {
			for _, id := range ids {
				d := b.file.Declarations[b.declarationIndex[id]]
				d.Variable.ParameterKind = "positional_only"
				b.update(d)
			}
			continue
		}
		if c.Kind() == "keyword_separator" {
			keywordOnly = true
			continue
		}
		name := field(c, "name")
		if name == nil {
			name = c
			for valid(name) && name.Kind() != "identifier" {
				name = firstNamed(name)
			}
		}
		if !valid(name) {
			continue
		}
		d := b.decl(c, name, inner, ir.DeclarationParameter)
		d.Variable = &ir.VariableDeclaration{ParameterKind: "positional_or_keyword"}
		if keywordOnly {
			d.Variable.ParameterKind = "keyword_only"
		}
		spelling := b.spelling(c)
		if strings.HasPrefix(spelling, "**") {
			d.Variable.ParameterKind = "var_keyword"
			d.Variable.Variadic = true
			keywordOnly = true
		} else if strings.HasPrefix(spelling, "*") {
			d.Variable.ParameterKind = "var_positional"
			d.Variable.Variadic = true
			keywordOnly = true
		}
		d.Variable.InitializerID = b.expression(field(c, "value"), outer, ir.AccessRead)
		d.Variable.DeclaredTypeID = b.typeRef(field(c, "type"), inner, ir.TypeUseParameter)
		ids = append(ids, b.addDeclaration(d))
	}
	return ids
}

func (b *builder) assignment(n *sitter.Node, env environment) ir.ExpressionID {
	right := field(n, "right")
	if right == nil {
		right = field(n, "value")
	}
	left := field(n, "left")
	if left == nil {
		left = field(n, "name")
	}
	var value ir.ExpressionID
	if right != nil && right.Kind() == "assignment" {
		value = b.assignment(right, env)
	} else {
		value = b.expression(right, env, ir.AccessRead)
	}
	typ := b.typeRef(field(n, "type"), env, ir.TypeUseLocal)
	b.bindTarget(left, env, value, typ)
	return b.structural(n, env, nonempty(value))
}
func nonempty(ids ...ir.ExpressionID) []ir.ExpressionID {
	var out []ir.ExpressionID
	for _, id := range ids {
		if id != "" {
			out = append(out, id)
		}
	}
	return out
}
func (b *builder) bindTarget(n *sitter.Node, env environment, value ir.ExpressionID, typ ir.TypeRefID) {
	if !valid(n) {
		return
	}
	if n.Kind() == "identifier" {
		kind := ir.DeclarationVariable
		if i, ok := b.declarationIndex[env.owner]; ok {
			if b.file.Declarations[i].Kind == ir.DeclarationClass {
				kind = ir.DeclarationField
			} else {
				kind = ir.DeclarationLocal
			}
		}
		d := b.decl(n, n, env, kind)
		d.Variable = &ir.VariableDeclaration{InitializerID: value, DeclaredTypeID: typ}
		b.addDeclaration(d)
		b.expression(n, env, ir.AccessWrite)
	} else if n.Kind() == "attribute" || n.Kind() == "subscript" {
		b.expression(n, env, ir.AccessWrite)
	} else {
		for _, c := range named(n) {
			b.bindTarget(c, env, "", typ)
		}
	}
}

func (b *builder) imports(n *sitter.Node, env environment) {
	module := field(n, "module_name")
	moduleText := b.spelling(module)
	level := len(moduleText) - len(strings.TrimLeft(moduleText, "."))
	for _, c := range named(n) {
		if same(c, module) {
			continue
		}
		name, alias := c, field(c, "alias")
		if c.Kind() == "aliased_import" {
			name = field(c, "name")
		}
		if !valid(name) {
			continue
		}
		binding := b.spelling(alias)
		if binding == "" {
			binding = b.spelling(name)
			if module == nil {
				binding = strings.Split(binding, ".")[0]
			}
		}
		writtenModule := strings.TrimLeft(moduleText, ".")
		if module == nil {
			writtenModule = b.spelling(name)
		}
		b.record()
		im := ir.Import{Occurrence: b.occurrence(name, env), Kind: ir.ImportModule, Name: b.name(name), Alias: b.spelling(alias), BindingName: binding, RelativeLevel: uint32(level), Spelling: b.spelling(n), Module: writtenModule}
		b.file.Imports = append(b.file.Imports, im)
		b.account(string(im.Occurrence.ID), im)
	}
}

func (b *builder) typeRef(n *sitter.Node, env environment, role ir.TypeUseRole) ir.TypeRefID {
	if !valid(n) {
		return ""
	}
	if n.Kind() == "type" && n.NamedChildCount() == 1 {
		return b.typeRef(firstNamed(n), env, role)
	}
	b.record()
	t := ir.TypeRef{ID: ir.TypeRefID(b.id("type", n)), Kind: ir.TypeOperator, Span: b.span(n), ScopeID: env.scope, Spelling: b.spelling(n)}
	if n.Kind() == "identifier" || n.Kind() == "attribute" || n.Kind() == "dotted_name" {
		span := b.span(n)
		t.Kind = ir.TypeNamed
		t.Named = &ir.NamedType{Segments: []ir.TypeSegment{{Name: b.spelling(n), Span: &span}}}
	}
	b.file.Types = append(b.file.Types, t)
	b.account(string(t.ID), t)
	if t.Kind == ir.TypeNamed {
		b.record()
		name := n
		if n.Kind() == "attribute" {
			// Pyright binds the terminal Name in its original member-access
			// context. Keep the whole spelling in the type record.
			name = field(n, "attribute")
			b.expression(field(n, "object"), env, ir.AccessRead)
		} else if n.Kind() == "dotted_name" {
			children := named(n)
			name = children[len(children)-1]
		}
		u := ir.TypeUse{Occurrence: b.occurrence(name, env), Role: role, TypeRefID: t.ID}
		b.file.TypeUses = append(b.file.TypeUses, u)
		b.account(string(u.Occurrence.ID), u)
	} else if n.Kind() == "string" {
		if !b.forwardType(n, env, role) {
			b.issue(n, "forward_annotation", "complex or escaped string annotation target extraction is not supported", ir.CoverageUnsupported)
		}
	} else {
		children := named(n)
		// Literal's arguments are values, and so are Annotated's after its
		// type: a string there is no forward reference, a call no type.
		values := 0
		if (n.Kind() == "subscript" || n.Kind() == "generic_type") && len(children) > 0 {
			switch terminalName(b, children[0]) {
			case "Literal":
				values = 1
			case "Annotated":
				values = 2
			}
		}
		if values > 0 {
			children = typeArguments(children)
		}
		for i, c := range children {
			if values > 0 && i >= values {
				b.valueArgument(c, env)
				continue
			}
			childRole := ir.TypeUseComponent
			if i == 0 && (n.Kind() == "subscript" || n.Kind() == "generic_type") {
				// Base[T] inherits from Base; T is a type argument, not a
				// second base class. Preserve the outer usage on the head.
				childRole = role
			}
			start := len(b.file.TypeUses)
			b.typeRef(c, env, childRole)
			for j := start; j < len(b.file.TypeUses); j++ {
				if b.file.TypeUses[j].ParentTypeID == "" {
					b.file.TypeUses[j].ParentTypeID = t.ID
					b.account(string(b.file.TypeUses[j].Occurrence.ID), b.file.TypeUses[j])
				}
			}
		}
	}
	return t.ID
}

func (b *builder) structural(n *sitter.Node, env environment, operands []ir.ExpressionID) ir.ExpressionID {
	b.record()
	e := ir.Expression{ID: ir.ExpressionID(b.id("expr", n)), Kind: ir.ExpressionPython, Span: b.span(n), ScopeID: env.scope, Spelling: ir.BoundSpelling(b.spelling(n)), SyntaxKind: n.Kind(), OperandIDs: operands}
	b.file.Expressions = append(b.file.Expressions, e)
	b.account(string(e.ID), e)
	return e.ID
}

func (b *builder) expression(n *sitter.Node, env environment, access ir.AccessKind) ir.ExpressionID {
	if !valid(n) {
		return ""
	}
	if n.Kind() == "assignment" || n.Kind() == "named_expression" {
		return b.assignment(n, env)
	}
	key := nodeKey(n, env) + string(access)
	if id := b.expressions[key]; id != "" {
		return id
	}
	e := ir.Expression{ID: ir.ExpressionID(b.id("expr", n)), Kind: ir.ExpressionPython, Span: b.span(n), ScopeID: env.scope, Spelling: ir.BoundSpelling(b.spelling(n)), SyntaxKind: n.Kind()}
	b.expressions[key] = e.ID
	switch n.Kind() {
	case "identifier":
		e.Kind = ir.ExpressionName
		name := b.name(n)
		e.Name = &name
		r := ir.Reference{Occurrence: b.occurrence(n, env), Kind: ir.ReferenceName, ExpressionID: e.ID, Name: name, Access: access}
		b.record()
		b.file.References = append(b.file.References, r)
		b.account(string(r.Occurrence.ID), r)
	case "attribute":
		e.Kind = ir.ExpressionMemberAccess
		receiver := b.expression(field(n, "object"), env, ir.AccessRead)
		e.OperandIDs = nonempty(receiver)
		nameNode := field(n, "attribute")
		if !valid(nameNode) {
			break
		}
		name := b.name(nameNode)
		e.Name = &name
		r := ir.Reference{Occurrence: b.occurrence(nameNode, env), Kind: ir.ReferenceMember, ExpressionID: e.ID, Name: name, ReceiverID: receiver, Access: access}
		b.record()
		b.file.References = append(b.file.References, r)
		b.account(string(r.Occurrence.ID), r)
	case "call":
		e.Kind = ir.ExpressionCall
		callee := field(n, "function")
		c := ir.Call{Occurrence: b.occurrence(n, env), Kind: ir.CallInvocation, ExpressionID: e.ID, CalleeID: b.expression(callee, env, ir.AccessRead)}
		if valid(callee) {
			if callee.Kind() == "identifier" {
				c.Name = b.spelling(callee)
			} else if callee.Kind() == "attribute" {
				c.Name = b.spelling(field(callee, "attribute"))
				c.ReceiverID = b.expression(field(callee, "object"), env, ir.AccessRead)
			}
		}
		args := field(n, "arguments")
		arguments := named(args)
		if valid(args) && args.Kind() == "generator_expression" {
			arguments = []*sitter.Node{args}
		}
		for _, arg := range arguments {
			a := ir.Argument{}
			value := arg
			switch arg.Kind() {
			case "keyword_argument":
				a.Keyword = b.spelling(field(arg, "name"))
				value = field(arg, "value")
			case "list_splat":
				a.Expansion = "iterable"
				value = firstNamed(arg)
			case "dictionary_splat":
				a.Expansion = "mapping"
				value = firstNamed(arg)
			}
			a.ExpressionID = b.expression(value, env, ir.AccessRead)
			if a.ExpressionID != "" {
				c.Arguments = append(c.Arguments, a)
			}
		}
		e.OccurrenceID = c.Occurrence.ID
		e.OperandIDs = nonempty(c.CalleeID)
		for _, a := range c.Arguments {
			e.OperandIDs = append(e.OperandIDs, a.ExpressionID)
		}
		b.record()
		b.file.Calls = append(b.file.Calls, c)
		b.account(string(c.Occurrence.ID), c)
	case "lambda":
		e.Kind = ir.ExpressionLambda
		inner := environment{scope: b.scope(ir.ScopeLambda, b.span(n), env), owner: env.owner}
		params := b.parameters(field(n, "parameters"), inner, env)
		e.Lambda = &ir.Lambda{ScopeID: inner.scope, ParameterIDs: params, BodyExpressionID: b.expression(field(n, "body"), inner, ir.AccessRead)}
		l := ir.LambdaSite{Occurrence: b.occurrence(n, env), ExpressionID: e.ID, ParameterIDs: params}
		e.OccurrenceID = l.Occurrence.ID
		b.record()
		b.file.Lambdas = append(b.file.Lambdas, l)
		b.account(string(l.Occurrence.ID), l)
	case "list_comprehension", "set_comprehension", "dictionary_comprehension", "generator_expression":
		inner := environment{scope: b.scope(ir.ScopeComprehension, b.span(n), env), owner: env.owner}
		first := true
		for _, c := range named(n) {
			if c.Kind() == "for_in_clause" {
				iterEnv := inner
				if first {
					iterEnv = env
					first = false
				}
				e.OperandIDs = append(e.OperandIDs, nonempty(b.expression(field(c, "right"), iterEnv, ir.AccessRead))...)
				b.bindTarget(field(c, "left"), inner, "", "")
			} else if c.Kind() == "if_clause" {
				for _, v := range named(c) {
					e.OperandIDs = append(e.OperandIDs, nonempty(b.expression(v, inner, ir.AccessRead))...)
				}
			}
		}
		e.OperandIDs = append(e.OperandIDs, nonempty(b.expression(field(n, "body"), inner, ir.AccessRead))...)
	case "integer", "float", "true", "false", "none", "string":
		e.Kind = ir.ExpressionLiteral
		kind := ir.LiteralString
		switch n.Kind() {
		case "integer":
			kind = ir.LiteralInteger
		case "float":
			kind = ir.LiteralFloating
		case "true", "false":
			kind = ir.LiteralBoolean
		case "none":
			kind = ir.LiteralNull
		}
		e.Literal = &ir.Literal{Kind: kind, Lexeme: e.Spelling}
		// f-string interpolation is executable, even inside a string token.
		for _, c := range named(n) {
			if c.Kind() == "interpolation" {
				for _, v := range named(c) {
					e.OperandIDs = append(e.OperandIDs, nonempty(b.expression(v, env, ir.AccessRead))...)
				}
			}
		}
	case "parenthesized_expression":
		e.Kind = ir.ExpressionParenthesized
		e.OperandIDs = nonempty(b.expression(firstNamed(n), env, access))
	case "subscript":
		e.Kind = ir.ExpressionArrayAccess
		for _, c := range named(n) {
			e.OperandIDs = append(e.OperandIDs, nonempty(b.expression(c, env, ir.AccessRead))...)
		}
	case "type_conversion":
		// f-string !r/!s/!a is a conversion flag, not a name reference.
	case "list", "tuple", "set", "dictionary", "pair", "binary_operator", "boolean_operator", "comparison_operator", "unary_operator", "not_operator", "conditional_expression", "await", "yield", "list_splat", "dictionary_splat", "slice", "ellipsis", "expression_list", "pattern_list", "list_pattern", "tuple_pattern", "concatenated_string", "interpolation", "format_expression", "format_specifier", "as_pattern", "as_pattern_target":
		for _, c := range named(n) {
			e.OperandIDs = append(e.OperandIDs, nonempty(b.expression(c, env, ir.AccessRead))...)
		}
	default:
		b.issue(n, "expression", "unsupported expression: "+n.Kind(), ir.CoverageUnsupported)
		for _, c := range named(n) {
			e.OperandIDs = append(e.OperandIDs, nonempty(b.expression(c, env, ir.AccessRead))...)
		}
	}
	b.record()
	b.file.Expressions = append(b.file.Expressions, e)
	b.account(string(e.ID), e)
	return e.ID
}

// terminalName is the last name of a type's head: Literal for Literal and
// typing.Literal.
func terminalName(b *builder, n *sitter.Node) string {
	for n.Kind() == "type" && n.NamedChildCount() == 1 {
		n = firstNamed(n)
	}
	switch n.Kind() {
	case "identifier":
		return b.spelling(n)
	case "attribute":
		return b.spelling(field(n, "attribute"))
	}
	return ""
}

// typeArguments are a subscripted type's head and arguments, with the
// argument list of a generic_type flattened.
func typeArguments(children []*sitter.Node) []*sitter.Node {
	var out []*sitter.Node
	for _, c := range children {
		if c.Kind() == "type_parameter" {
			out = append(out, named(c)...)
			continue
		}
		out = append(out, c)
	}
	return out
}

// valueArgument walks a type argument that is a value: Literal["a"],
// Literal[Color.RED], Annotated[int, Depends(get_db)].
func (b *builder) valueArgument(n *sitter.Node, env environment) {
	for n.Kind() == "type" && n.NamedChildCount() == 1 {
		n = firstNamed(n)
	}
	b.expression(n, env, ir.AccessRead)
}

// forwardType handles unescaped quoted qualified names with exact original
// offsets. Composite/escaped annotations remain explicit coverage losses.
func (b *builder) forwardType(n *sitter.Node, env environment, role ir.TypeUseRole) bool {
	s := b.spelling(n)
	if len(s) < 2 {
		return false
	}
	quote := s[0]
	if (quote != '\'' && quote != '"') || s[len(s)-1] != quote {
		return false
	}
	width := 1
	if len(s) >= 6 && s[:3] == strings.Repeat(string(quote), 3) && s[len(s)-3:] == s[:3] {
		width = 3
	}
	value := s[width : len(s)-width]
	segments := strings.Split(value, ".")
	for _, part := range segments {
		if part == "" {
			return false
		}
		for i, r := range part {
			if r != '_' && !unicode.IsLetter(r) && (i == 0 || !unicode.IsDigit(r)) {
				return false
			}
		}
	}
	start := n.StartByte() + uint(width)
	var named ir.NamedType
	for _, part := range segments {
		span := b.rawSpan(start, start+uint(len(part)))
		named.Segments = append(named.Segments, ir.TypeSegment{Name: part, Span: &span})
		start += uint(len(part) + 1)
	}
	span := b.rawSpan(n.StartByte()+uint(width), n.EndByte()-uint(width))
	t := ir.TypeRef{ID: ir.TypeRefID(b.id("type", n)), Kind: ir.TypeNamed, Span: span, ScopeID: env.scope, Spelling: value, Named: &named}
	b.record()
	b.file.Types = append(b.file.Types, t)
	b.account(string(t.ID), t)
	occ := b.occurrence(n, env)
	occ.Span = *named.Segments[len(named.Segments)-1].Span
	u := ir.TypeUse{Occurrence: occ, Role: role, TypeRefID: t.ID}
	b.record()
	b.file.TypeUses = append(b.file.TypeUses, u)
	b.account(string(occ.ID), u)
	return true
}

func (b *builder) pattern(n *sitter.Node, env environment) {
	if !valid(n) {
		return
	}
	switch n.Kind() {
	case "case_pattern", "list_pattern", "tuple_pattern", "union_pattern", "splat_pattern", "as_pattern", "as_pattern_target":
		for _, c := range named(n) {
			b.pattern(c, env)
		}
	case "dotted_name":
		children := named(n)
		if len(children) == 1 {
			b.pattern(children[0], env)
		} else {
			b.typeRef(n, env, ir.TypeUsePattern)
		}
	case "identifier":
		if b.spelling(n) == "_" {
			return
		}
		d := b.decl(n, n, env, ir.DeclarationPatternVariable)
		d.Variable = &ir.VariableDeclaration{}
		b.addDeclaration(d)
	case "class_pattern":
		for _, c := range named(n) {
			if c.Kind() == "dotted_name" {
				b.typeRef(c, env, ir.TypeUsePattern)
			} else {
				b.pattern(c, env)
			}
		}
	case "keyword_pattern":
		children := named(n)
		if len(children) > 1 {
			b.pattern(children[len(children)-1], env)
		}
	case "dict_pattern":
		for i := uint(0); i < n.ChildCount(); i++ {
			c := n.Child(i)
			if !c.IsNamed() {
				continue
			}
			if n.FieldNameForChild(uint32(i)) == "key" {
				b.expression(c, env, ir.AccessRead)
			} else {
				b.pattern(c, env)
			}
		}
	default:
		b.expression(n, env, ir.AccessRead)
	}
}
