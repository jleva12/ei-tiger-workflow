package java

import (
	"ei-aitiger-codegraph/pkg/ir"
	sitter "github.com/tree-sitter/go-tree-sitter"
)

func (b *builder) moduleDeclaration(n *sitter.Node, env environment) {
	if b.file.Module != nil {
		b.issue(n, "module", "Multiple module declarations", ir.CoverageParseError)
		return
	}
	if !valid(field(n, "name")) {
		return
	}
	m := ir.ModuleDeclaration{Name: b.name(field(n, "name")), Span: b.span(n)}
	b.record()
	for i := uint(0); i < n.ChildCount(); i++ {
		child := n.Child(i)
		if child.Kind() == "open" {
			m.Open = true
		}
		if isAnnotation(child) {
			m.AnnotationIDs = b.appendAnnotation(m.AnnotationIDs, child, env)
		}
	}
	for _, node := range named(field(n, "body")) {
		if !valid(node) || node.IsError() {
			continue
		}
		d := ir.ModuleDirective{Occurrence: b.occurrence(node, env)}
		var name *sitter.Node
		switch node.Kind() {
		case "requires_module_directive":
			d.Kind = ir.ModuleRequires
			name = field(node, "module")
			for _, modifier := range fields(node, "modifiers") {
				d.Modifiers = append(d.Modifiers, ir.Modifier{Keyword: b.syntaxText(modifier), Span: b.span(modifier)})
			}
		case "exports_module_directive", "opens_module_directive":
			d.Kind = ir.ModuleExports
			if node.Kind() == "opens_module_directive" {
				d.Kind = ir.ModuleOpens
			}
			name = field(node, "package")
			for _, target := range fields(node, "modules") {
				if valid(target) {
					d.TargetModules = append(d.TargetModules, b.name(target))
				}
			}
		case "uses_module_directive":
			d.Kind = ir.ModuleUses
			d.ServiceTypeID = b.typeRef(field(node, "type"), env, ir.TypeUseModuleService, "")
		case "provides_module_directive":
			d.Kind = ir.ModuleProvides
			d.ServiceTypeID = b.typeRef(field(node, "provided"), env, ir.TypeUseModuleService, "")
			for _, provider := range fields(node, "provider") {
				if id := b.typeRef(provider, env, ir.TypeUseModuleProvider, ""); id != "" {
					d.ProviderTypeIDs = append(d.ProviderTypeIDs, id)
				}
			}
		default:
			b.issue(node, "module_directive", "Unsupported module directive", ir.CoverageUnsupported)
			continue
		}
		if valid(name) {
			value := b.name(name)
			d.Name = &value
		}
		if (d.Kind == ir.ModuleRequires || d.Kind == ir.ModuleExports || d.Kind == ir.ModuleOpens) && d.Name == nil {
			continue
		}
		if (d.Kind == ir.ModuleUses || d.Kind == ir.ModuleProvides) && d.ServiceTypeID == "" {
			continue
		}
		if d.Kind == ir.ModuleProvides && len(d.ProviderTypeIDs) == 0 {
			continue
		}
		b.record()
		m.Directives = append(m.Directives, d)
		b.account("module", m)
	}
	b.account("module", m)
	b.file.Module = &m
}
