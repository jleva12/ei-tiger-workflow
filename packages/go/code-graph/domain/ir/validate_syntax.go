package ir

import "fmt"

func (v *validator) registerSyntax() {
	for i, s := range v.file.Statements {
		v.register("statement", string(s.ID), fmt.Sprintf("statements[%d].id", i))
	}
	for i, p := range v.file.Patterns {
		v.register("pattern", string(p.ID), fmt.Sprintf("patterns[%d].id", i))
	}
	if m := v.file.Module; m != nil {
		for i, d := range m.Directives {
			v.register("occurrence", string(d.Occurrence.ID), fmt.Sprintf("module.directives[%d].occurrence.id", i))
		}
	}
}

func (v *validator) validateSyntax() {
	graph := map[string][]string{}
	declarations := make(map[DeclarationID]Declaration, len(v.file.Declarations))
	for _, d := range v.file.Declarations {
		declarations[d.ID] = d
	}
	link := func(from, table, id string) {
		if id != "" {
			graph[from] = append(graph[from], table+":"+id)
		}
	}
	for i, s := range v.file.Statements {
		p := fmt.Sprintf("statements[%d]", i)
		key := "statement:" + string(s.ID)
		v.choice(p+".kind", string(s.Kind), "block", "expression", "declaration", "return", "yield", "throw", "break", "continue", "if", "while", "do", "for", "enhanced_for", "try", "catch", "synchronized", "assert", "labeled", "switch", "switch_arm", "switch_label", "empty", "unknown")
		v.span(p+".span", s.Span)
		v.ref("scope", string(s.ScopeID), p+".scope_id", true)
		v.ref("declaration", string(s.EnclosingDeclarationID), p+".enclosing_declaration_id", false)
		if s.Kind == StatementUnknown && v.file.Coverage.Status == ExtractionComplete {
			v.problem(p, "unknown statement requires incomplete coverage")
		}
		refs(v, "declaration", p+".declaration_ids", s.DeclarationIDs)
		for _, group := range []struct {
			name string
			ids  []StatementID
		}{{"child_ids", s.ChildIDs}, {"initializer_ids", s.InitializerIDs}, {"resource_ids", s.ResourceIDs}, {"catch_ids", s.CatchIDs}} {
			refs(v, "statement", p+"."+group.name, group.ids)
			for _, id := range group.ids {
				link(key, "statement", string(id))
			}
		}
		for _, value := range []struct {
			name string
			id   StatementID
		}{{"body_id", s.BodyID}, {"alternative_id", s.AlternativeID}, {"finally_id", s.FinallyID}} {
			v.ref("statement", string(value.id), p+"."+value.name, false)
			link(key, "statement", string(value.id))
		}
		for _, group := range []struct {
			name string
			ids  []ExpressionID
		}{{"expression_ids", s.ExpressionIDs}, {"update_ids", s.UpdateIDs}} {
			refs(v, "expression", p+"."+group.name, group.ids)
			for _, id := range group.ids {
				link(key, "expression", string(id))
			}
		}
		v.ref("expression", string(s.ConditionID), p+".condition_id", false)
		link(key, "expression", string(s.ConditionID))
		refs(v, "pattern", p+".pattern_ids", s.PatternIDs)
		for _, id := range s.PatternIDs {
			link(key, "pattern", string(id))
		}
		if s.Label != nil {
			v.name(p+".label", *s.Label)
		}
		if s.Default && s.Kind != StatementSwitchLabel {
			v.problem(p, "default requires switch label")
		}
		if s.Arrow && s.Kind != StatementSwitchArm {
			v.problem(p, "arrow requires switch arm")
		}
		if (s.Kind == StatementReturn || s.Kind == StatementYield || s.Kind == StatementThrow || s.Kind == StatementExpression) && len(s.ExpressionIDs) > 1 {
			v.problem(p, "at most one result expression required")
		}
	}
	for i, patt := range v.file.Patterns {
		p := fmt.Sprintf("patterns[%d]", i)
		key := "pattern:" + string(patt.ID)
		v.choice(p+".kind", string(patt.Kind), "type", "record", "unnamed", "unknown")
		v.span(p+".span", patt.Span)
		v.ref("scope", string(patt.ScopeID), p+".scope_id", true)
		v.ref("declaration", string(patt.EnclosingDeclarationID), p+".enclosing_declaration_id", false)
		v.ref("type", string(patt.TypeRefID), p+".type_ref_id", patt.Kind == PatternType || patt.Kind == PatternRecord)
		v.ref("declaration", string(patt.DeclarationID), p+".declaration_id", patt.Kind == PatternType)
		refs(v, "pattern", p+".component_ids", patt.ComponentIDs)
		for _, id := range patt.ComponentIDs {
			link(key, "pattern", string(id))
		}
		if patt.Kind != PatternRecord && len(patt.ComponentIDs) > 0 {
			v.problem(p, "only record patterns have components")
		}
		if patt.Kind != PatternType && patt.DeclarationID != "" {
			v.problem(p, "only type patterns declare a variable")
		}
		if patt.Kind == PatternUnknown && v.file.Coverage.Status == ExtractionComplete {
			v.problem(p, "unknown pattern requires incomplete coverage")
		}
		if d, ok := declarations[patt.DeclarationID]; ok {
			if d.Kind != DeclarationPatternVariable || d.DeclaringScopeID != patt.ScopeID || d.OwnerID != patt.EnclosingDeclarationID || d.Variable == nil || d.Variable.DeclaredTypeID != patt.TypeRefID {
				v.problem(p, "pattern declaration must have pattern kind and matching scope, owner and type")
			}
		}
	}
	// Cross-table containment: catches cycles that stay hidden if each table is
	// checked independently (e.g. lambda -> block -> lambda).
	for _, x := range v.file.Expressions {
		key := "expression:" + string(x.ID)
		for _, id := range x.OperandIDs {
			link(key, "expression", string(id))
		}
		link(key, "pattern", string(x.PatternID))
		if x.Lambda != nil {
			link(key, "statement", string(x.Lambda.BodyStatementID))
			link(key, "expression", string(x.Lambda.BodyExpressionID))
		}
		if x.Switch != nil {
			link(key, "statement", string(x.Switch.BodyStatementID))
		}
	}
	for _, c := range v.file.Calls {
		key := "expression:" + string(c.ExpressionID)
		link(key, "expression", string(c.CalleeID))
		link(key, "expression", string(c.ReceiverID))
		for _, a := range c.Arguments {
			link(key, "expression", string(a.ExpressionID))
		}
	}
	for _, r := range v.file.CallableReferences {
		link("expression:"+string(r.ExpressionID), "expression", string(r.QualifierID))
	}
	v.cycles("syntax containment", graph)
	if m := v.file.Module; m != nil {
		v.module(m)
	}
	if l := v.file.LanguageValidation; l != nil {
		v.choice("language_validation.status", string(l.Status), "not_checked", "invalid")
		v.required("language_validation.method", l.Method)
		v.required("language_validation.release", l.Release)
	}
}

func (v *validator) module(m *ModuleDeclaration) {
	v.name("module.name", m.Name)
	v.span("module.span", m.Span)
	refs(v, "annotation", "module.annotation_ids", m.AnnotationIDs)
	for i, d := range m.Directives {
		p := fmt.Sprintf("module.directives[%d]", i)
		v.occurrence(p+".occurrence", d.Occurrence)
		v.choice(p+".kind", string(d.Kind), "requires", "exports", "opens", "uses", "provides")
		named := d.Kind == ModuleRequires || d.Kind == ModuleExports || d.Kind == ModuleOpens
		if (d.Name != nil) != named {
			v.problem(p+".name", "name required only on requires/exports/opens")
		}
		if d.Name != nil {
			v.name(p+".name", *d.Name)
		}
		v.ref("type", string(d.ServiceTypeID), p+".service_type_id", d.Kind == ModuleUses || d.Kind == ModuleProvides)
		if named && d.ServiceTypeID != "" {
			v.problem(p, "named directive cannot carry service type")
		}
		refs(v, "type", p+".provider_type_ids", d.ProviderTypeIDs)
		if d.Kind == ModuleProvides && len(d.ProviderTypeIDs) == 0 {
			v.problem(p, "provides needs provider types")
		}
		if d.Kind != ModuleProvides && len(d.ProviderTypeIDs) > 0 {
			v.problem(p, "providers only valid on provides")
		}
		for j, n := range d.TargetModules {
			v.name(fmt.Sprintf("%s.target_modules[%d]", p, j), n)
		}
		if len(d.TargetModules) > 0 && d.Kind != ModuleExports && d.Kind != ModuleOpens {
			v.problem(p, "target modules require exports/opens")
		}
		for _, m := range d.Modifiers {
			v.choice(p+".modifiers.keyword", m.Keyword, "static", "transitive")
			v.span(p+".modifiers.span", m.Span)
		}
		if len(d.Modifiers) > 0 && d.Kind != ModuleRequires {
			v.problem(p, "modifiers require requires")
		}
	}
}
