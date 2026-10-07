package java

import (
	"fmt"

	"ei-aitiger-codegraph/pkg/ir"

	sitter "github.com/tree-sitter/go-tree-sitter"
)

func isAnnotation(n *sitter.Node) bool {
	return n != nil && (n.Kind() == "annotation" || n.Kind() == "marker_annotation")
}
func nodeKey(n *sitter.Node, env environment) string {
	return fmt.Sprintf("%d/%s/%s", n.Id(), env.scope, env.owner)
}
func (b *builder) appendAnnotation(ids []ir.AnnotationID, n *sitter.Node, env environment) []ir.AnnotationID {
	if id := b.annotation(n, env); id != "" {
		return append(ids, id)
	}
	return ids
}

func (b *builder) annotation(n *sitter.Node, env environment) ir.AnnotationID {
	if !valid(n) || !valid(field(n, "name")) {
		return ""
	}
	key := nodeKey(n, env)
	if id := b.annotations[key]; id != "" {
		return id
	}
	a := ir.Annotation{ID: ir.AnnotationID(b.id("annotation", n)), Occurrence: b.occurrence(n, env), TypeRefID: b.typeRef(field(n, "name"), env, ir.TypeUseAnnotation, "")}
	for _, argument := range named(field(n, "arguments")) {
		value := argument
		entry := ir.AnnotationArgument{}
		if argument.Kind() == "element_value_pair" {
			name := field(argument, "key")
			value = field(argument, "value")
			if valid(name) {
				span := b.span(name)
				entry.Name, entry.NameSpan = b.syntaxText(name), &span
			}
		}
		if !valid(value) {
			continue
		}
		entry.Value = b.annotationValue(value, env)
		a.Arguments = append(a.Arguments, entry)
	}
	b.record()
	b.account(string(a.ID), a)
	b.file.Annotations = append(b.file.Annotations, a)
	b.annotations[key] = a.ID
	return a.ID
}

func (b *builder) annotationValue(n *sitter.Node, env environment) ir.AnnotationValue {
	value := ir.AnnotationValue{Span: b.span(n)}
	if n.Kind() == "element_value_array_initializer" {
		value.Kind = ir.AnnotationArray
		for _, child := range named(n) {
			if valid(child) {
				value.Elements = append(value.Elements, b.annotationValue(child, env))
			}
		}
	} else if isAnnotation(n) {
		value.Kind, value.AnnotationID = ir.AnnotationNested, b.annotation(n, env)
		if value.AnnotationID == "" {
			value.Kind = ir.AnnotationExpression
			value.ExpressionID = b.unknown(n, env, "Incomplete nested annotation")
		}
	} else {
		value.Kind, value.ExpressionID = ir.AnnotationExpression, b.expression(n, env, ir.AccessRead)
	}
	return value
}
