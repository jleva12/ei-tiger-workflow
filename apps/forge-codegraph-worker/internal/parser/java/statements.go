package java

import (
	"ei-aitiger-codegraph/pkg/ir"
	sitter "github.com/tree-sitter/go-tree-sitter"
)

func (b *builder) children(n *sitter.Node, env environment) []ir.StatementID {
	var ids []ir.StatementID
	for _, child := range named(n) {
		if id := b.visit(child, env); id != "" {
			ids = append(ids, id)
		}
	}
	return ids
}

func (b *builder) statementChildren(n *sitter.Node, env environment) []ir.StatementID {
	var ids []ir.StatementID
	if n == nil {
		return nil
	}
	for i := uint(0); i < n.ChildCount(); i++ {
		child := n.Child(i)
		if child.Kind() == "line_comment" || child.Kind() == "block_comment" {
			continue
		}
		if child.IsNamed() || child.Kind() == ";" {
			if id := b.visit(child, env); id != "" {
				ids = append(ids, id)
			}
		}
	}
	return ids
}

func (b *builder) baseStatement(n *sitter.Node, env environment, kind ir.StatementKind) ir.Statement {
	return ir.Statement{ID: ir.StatementID(b.id("stmt", n)), Kind: kind, Span: b.span(n), ScopeID: env.scope, EnclosingDeclarationID: env.owner}
}
func (b *builder) saveStatement(n *sitter.Node, env environment, s ir.Statement) ir.StatementID {
	if len(s.ExpressionIDs) > 1 && (s.Kind == ir.StatementExpression || s.Kind == ir.StatementReturn || s.Kind == ir.StatementYield || s.Kind == ir.StatementThrow) {
		s.Kind, s.SyntaxKind = ir.StatementUnknown, n.Kind()
		b.issue(n, "statement_recovery", "Recovered statement contains multiple result expressions", ir.CoverageParseError)
	}
	b.record()
	b.account(string(s.ID), s)
	b.statementIndex[s.ID] = len(b.file.Statements)
	b.file.Statements = append(b.file.Statements, s)
	b.statements[nodeKey(n, env)] = s.ID
	return s.ID
}
func (b *builder) statementBlock(n *sitter.Node, env environment) ir.StatementID {
	s := b.baseStatement(n, env, ir.StatementBlock)
	s.ChildIDs = b.statementChildren(n, env)
	return b.saveStatement(n, env, s)
}
func (b *builder) expressionStatement(n *sitter.Node, env environment, kind ir.StatementKind) ir.StatementID {
	s := b.baseStatement(n, env, kind)
	if id := b.expression(n, env, ir.AccessRead); id != "" {
		s.ExpressionIDs = []ir.ExpressionID{id}
	}
	return b.saveStatement(n, env, s)
}

func (b *builder) visit(n *sitter.Node, env environment) ir.StatementID {
	b.check()
	if !valid(n) || n.IsError() {
		return ""
	}
	if id := b.statements[nodeKey(n, env)]; id != "" {
		return id
	}
	if env.scope == b.file.RootScopeID {
		switch n.Kind() {
		case "class_declaration", "interface_declaration", "record_declaration", "enum_declaration", "annotation_type_declaration", "package_declaration", "import_declaration", "module_declaration":
		default:
			b.releaseViolation(n, "Top-level statements and callables are outside this Java release profile")
		}
	}
	switch b.captures[n.Id()] {
	case "definition":
		before := len(b.file.Declarations)
		b.declaration(n, env)
		if b.ownerIsType(env.owner) || env.scope == b.file.RootScopeID {
			return ""
		}
		s := b.baseStatement(n, env, ir.StatementDeclaration)
		for _, d := range b.file.Declarations[before:] {
			if d.OwnerID == env.owner && d.DeclaringScopeID == env.scope {
				s.DeclarationIDs = append(s.DeclarationIDs, d.ID)
			}
		}
		return b.saveStatement(n, env, s)
	case "import":
		b.importDeclaration(n, env)
		return ""
	case "call", "callable_reference":
		return b.expressionStatement(n, env, ir.StatementExpression)
	}
	s := b.baseStatement(n, env, ir.StatementUnknown)
	addExpression := func(n *sitter.Node) {
		if id := b.expression(n, env, ir.AccessRead); id != "" {
			s.ExpressionIDs = append(s.ExpressionIDs, id)
		}
	}
	switch n.Kind() {
	case "package_declaration":
		if b.file.Package != nil {
			b.issue(n, "package", "Multiple package declarations", ir.CoverageParseError)
			return ""
		}
		p := &ir.PackageClause{Span: b.span(n)}
		for _, child := range named(n) {
			if isAnnotation(child) {
				p.AnnotationIDs = b.appendAnnotation(p.AnnotationIDs, child, env)
			} else {
				p.Name = b.name(child)
			}
		}
		if len(p.Name.Segments) == 0 {
			return ""
		}
		b.record()
		b.account("package", p)
		b.file.Package = p
		return ""
	case "module_declaration":
		b.moduleDeclaration(n, env)
		return ""
	case "block", "constructor_body":
		if b.ownerIsType(env.owner) {
			b.initializer(n, env, ir.InitializerInstance)
			return ""
		}
		inner := env
		inner.scope = b.scope(ir.ScopeBlock, b.span(n), env)
		return b.statementBlock(n, inner)
	case "static_initializer":
		b.initializer(n, env, ir.InitializerStatic)
		return ""
	case "class_body", "enum_body", "enum_body_declarations", "interface_body", "annotation_type_body", "program":
		b.children(n, env)
		return ""
	case ";":
		s.Kind = ir.StatementEmpty
	case "expression_statement", "return_statement", "throw_statement", "yield_statement":
		kinds := map[string]ir.StatementKind{"expression_statement": ir.StatementExpression, "return_statement": ir.StatementReturn, "throw_statement": ir.StatementThrow, "yield_statement": ir.StatementYield}
		s.Kind = kinds[n.Kind()]
		for _, child := range named(n) {
			addExpression(child)
		}
	case "if_statement":
		s.Kind = ir.StatementIf
		s.ConditionID = b.expression(field(n, "condition"), env, ir.AccessRead)
		s.BodyID = b.visit(field(n, "consequence"), env)
		s.AlternativeID = b.visit(field(n, "alternative"), env)
	case "while_statement", "do_statement", "for_statement":
		inner := env
		inner.scope = b.scope(ir.ScopeLoop, b.span(n), env)
		s.ScopeID = inner.scope
		if n.Kind() == "while_statement" {
			s.Kind = ir.StatementWhile
		} else if n.Kind() == "do_statement" {
			s.Kind = ir.StatementDo
		} else {
			s.Kind = ir.StatementFor
		}
		if s.Kind == ir.StatementDo {
			s.BodyID = b.visit(field(n, "body"), inner)
		}
		for _, init := range fields(n, "init") {
			if id := b.visit(init, inner); id != "" {
				s.InitializerIDs = append(s.InitializerIDs, id)
			}
		}
		s.ConditionID = b.expression(field(n, "condition"), inner, ir.AccessRead)
		for _, update := range fields(n, "update") {
			if id := b.expression(update, inner, ir.AccessRead); id != "" {
				s.UpdateIDs = append(s.UpdateIDs, id)
			}
		}
		if s.Kind != ir.StatementDo {
			s.BodyID = b.visit(field(n, "body"), inner)
		}
	case "enhanced_for_statement":
		s.Kind = ir.StatementEnhancedFor
		addExpression(field(n, "value"))
		inner := env
		inner.scope = b.scope(ir.ScopeLoop, b.span(n), env)
		s.ScopeID = inner.scope
		if id := b.variable(n, field(n, "name"), field(n, "type"), nil, field(n, "dimensions"), inner, ir.DeclarationLocal, ir.TypeUseLocal, false); id != "" {
			s.DeclarationIDs = []ir.DeclarationID{id}
		}
		s.BodyID = b.visit(field(n, "body"), inner)
	case "try_statement", "try_with_resources_statement":
		s.Kind = ir.StatementTry
		inner := env
		if resources := field(n, "resources"); resources != nil {
			inner.scope = b.scope(ir.ScopeResource, b.span(n), env)
			i := len(b.file.Scopes) - 1
			for _, part := range []*sitter.Node{resources, field(n, "body")} {
				if part != nil {
					b.file.Scopes[i].Regions = append(b.file.Scopes[i].Regions, b.span(part))
				}
			}
			b.account(string(inner.scope), b.file.Scopes[i])
			s.ResourceIDs = b.children(resources, inner)
		}
		s.BodyID = b.visit(field(n, "body"), inner)
		for _, child := range named(n) {
			switch child.Kind() {
			case "catch_clause":
				if id := b.visit(child, env); id != "" {
					s.CatchIDs = append(s.CatchIDs, id)
				}
			case "finally_clause":
				s.FinallyID = b.visit(child, env)
			}
		}
	case "resource":
		if field(n, "name") == nil {
			s.Kind = ir.StatementExpression
			for _, child := range named(n) {
				addExpression(child)
			}
		} else {
			s.Kind = ir.StatementDeclaration
			if id := b.variable(n, field(n, "name"), field(n, "type"), field(n, "value"), field(n, "dimensions"), env, ir.DeclarationLocal, ir.TypeUseLocal, false); id != "" {
				s.DeclarationIDs = []ir.DeclarationID{id}
			}
		}
	case "catch_clause":
		s.Kind = ir.StatementCatch
		inner := env
		inner.scope = b.scope(ir.ScopeCatch, b.span(n), env)
		s.ScopeID = inner.scope
		param := childKind(n, "catch_formal_parameter")
		if id := b.variable(param, field(param, "name"), childKind(param, "catch_type"), nil, field(param, "dimensions"), inner, ir.DeclarationLocal, ir.TypeUseCatch, false); id != "" {
			s.DeclarationIDs = []ir.DeclarationID{id}
		}
		s.BodyID = b.visit(field(n, "body"), inner)
	case "finally_clause":
		return b.visit(childKind(n, "block"), env)
	case "synchronized_statement":
		s.Kind = ir.StatementSynchronized
		addExpression(childKind(n, "parenthesized_expression"))
		s.BodyID = b.visit(field(n, "body"), env)
	case "assert_statement":
		s.Kind = ir.StatementAssert
		children := named(n)
		if len(children) > 0 {
			s.ConditionID = b.expression(children[0], env, ir.AccessRead)
		}
		if len(children) > 1 {
			addExpression(children[1])
		}
	case "labeled_statement":
		s.Kind = ir.StatementLabeled
		label := childKind(n, "identifier")
		if valid(label) {
			name := b.name(label)
			s.Label = &name
		}
		for i := uint(0); i < n.ChildCount(); i++ {
			child := n.Child(i)
			if label != nil && child.Id() == label.Id() {
				continue
			}
			if child.IsNamed() || child.Kind() == ";" {
				s.BodyID = b.visit(child, env)
			}
		}
	case "break_statement", "continue_statement":
		s.Kind = ir.StatementBreak
		if n.Kind() == "continue_statement" {
			s.Kind = ir.StatementContinue
		}
		if label := childKind(n, "identifier"); valid(label) {
			name := b.name(label)
			s.Label = &name
		}
	case "switch_expression":
		return b.expressionStatement(n, env, ir.StatementSwitch)
	case "switch_block":
		inner := env
		inner.scope = b.scope(ir.ScopeBlock, b.span(n), env)
		return b.statementBlock(n, inner)
	case "switch_rule", "switch_block_statement_group":
		s.Kind = ir.StatementSwitchArm
		s.Arrow = n.Kind() == "switch_rule"
		inner := env
		if s.Arrow {
			inner.scope = b.scope(ir.ScopeBlock, b.span(n), env)
			s.ScopeID = inner.scope
		}
		s.ChildIDs = b.statementChildren(n, inner)
		// An arrow expression statement supplies a switch result without an explicit
		// yield token; retaining Arrow + child kind preserves this distinction.
	case "switch_label":
		s.Kind = ir.StatementSwitchLabel
		for i := uint(0); i < n.ChildCount(); i++ {
			child := n.Child(i)
			if child.Kind() == "default" {
				s.Default = true
				continue
			}
			switch child.Kind() {
			case "pattern", "type_pattern", "record_pattern":
				if id := b.pattern(child, env); id != "" {
					s.PatternIDs = append(s.PatternIDs, id)
				}
			case "guard":
				for _, condition := range named(child) {
					s.ConditionID = b.expression(condition, env, ir.AccessRead)
				}
			default:
				if child.IsNamed() && child.Kind() != "line_comment" && child.Kind() != "block_comment" {
					addExpression(child)
				}
			}
		}
	default:
		if isExpression(n.Kind()) {
			return b.expressionStatement(n, env, ir.StatementExpression)
		}
		s.SyntaxKind = n.Kind()
		b.issue(n, n.Kind(), "Unsupported statement syntax: "+n.Kind(), ir.CoverageUnsupported)
	}
	return b.saveStatement(n, env, s)
}

func fields(n *sitter.Node, name string) []*sitter.Node {
	var result []*sitter.Node
	if n == nil {
		return nil
	}
	for i := uint(0); i < n.ChildCount(); i++ {
		if n.FieldNameForChild(uint32(i)) == name {
			result = append(result, n.Child(i))
		}
	}
	return result
}
func (b *builder) importDeclaration(n *sitter.Node, env environment) {
	var name *sitter.Node
	static, wildcard := false, false
	for i := uint(0); i < n.ChildCount(); i++ {
		child := n.Child(i)
		switch child.Kind() {
		case "static":
			static = true
		case "asterisk":
			wildcard = true
		case "identifier", "scoped_identifier":
			name = child
		}
	}
	if !valid(name) {
		return
	}
	kind := ir.ImportSingleType
	if static && wildcard {
		kind = ir.ImportStaticOnDemand
	} else if static {
		kind = ir.ImportSingleStatic
	} else if wildcard {
		kind = ir.ImportTypeOnDemand
	}
	value := ir.Import{Occurrence: b.occurrence(n, env), Kind: kind, Name: b.name(name), Spelling: b.spelling(n)}
	b.record()
	b.account(string(value.Occurrence.ID), value)
	b.file.Imports = append(b.file.Imports, value)
}

func (b *builder) name(n *sitter.Node) ir.Name {
	var result ir.Name
	if n == nil {
		return result
	}
	switch n.Kind() {
	case "identifier", "type_identifier", "this", "super":
		if valid(n) {
			span := b.span(n)
			result.Segments = append(result.Segments, ir.NameSegment{Text: b.syntaxText(n), Span: &span})
		}
	default:
		for _, child := range named(n) {
			if !isAnnotation(child) {
				result.Segments = append(result.Segments, b.name(child).Segments...)
			}
		}
	}
	return result
}
