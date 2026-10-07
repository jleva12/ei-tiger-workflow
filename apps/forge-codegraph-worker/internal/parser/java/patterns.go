package java

import (
	"ei-aitiger-codegraph/pkg/ir"

	sitter "github.com/tree-sitter/go-tree-sitter"
)

// pattern generates an IR pattern from a syntax tree node and associates it with an environment, returning its ID.
func (b *builder) pattern(n *sitter.Node, env environment) ir.PatternID {
	if !valid(n) {
		return ""
	}
	b.check()
	if n.Kind() == "pattern" {
		for _, child := range named(n) {
			return b.pattern(child, env)
		}
		return ""
	}
	if id := b.patterns[nodeKey(n, env)]; id != "" {
		return id
	}
	p := ir.Pattern{ID: ir.PatternID(b.id("pattern", n)), Kind: ir.PatternType, Span: b.span(n), ScopeID: env.scope, EnclosingDeclarationID: env.owner}
	switch n.Kind() {
	case "record_pattern":
		p.Kind = ir.PatternRecord
		p.TypeRefID = b.typeRef(field(n, "type"), env, ir.TypeUsePattern, "")
		for _, component := range named(field(n, "body")) {
			if id := b.pattern(component, env); id != "" {
				p.ComponentIDs = append(p.ComponentIDs, id)
			}
		}
	case "underscore_pattern":
		p.Kind = ir.PatternUnnamed
	case "instanceof_expression", "type_pattern", "record_pattern_component":
		typ := field(n, "type")
		if n.Kind() == "instanceof_expression" {
			typ = field(n, "right")
		}
		name := field(n, "name")
		if valid(name) && valid(typ) {
			p.DeclarationID = b.variable(n, name, typ, nil, nil, env, ir.DeclarationPatternVariable, ir.TypeUsePattern, false)
			p.TypeRefID = b.file.Declarations[b.declarationIndex[p.DeclarationID]].Variable.DeclaredTypeID
		}
	default:
		p.Kind = ir.PatternUnknown
	}
	if (p.Kind == ir.PatternType && (p.DeclarationID == "" || p.TypeRefID == "")) || (p.Kind == ir.PatternRecord && p.TypeRefID == "") {
		p.Kind = ir.PatternUnknown
		p.DeclarationID = ""
		p.ComponentIDs = nil
	}
	if p.Kind == ir.PatternUnknown {
		b.issue(n, "patterns", "Incomplete or unsupported pattern syntax", ir.CoverageUnsupported)
	}
	b.record()
	b.account(string(p.ID), p)
	b.patternIndex[p.ID] = len(b.file.Patterns)
	b.file.Patterns = append(b.file.Patterns, p)
	b.patterns[nodeKey(n, env)] = p.ID
	return p.ID
}
