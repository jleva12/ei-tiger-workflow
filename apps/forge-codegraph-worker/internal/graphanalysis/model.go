// Package graphanalysis composes the language-neutral stages that turn one
// run's semantic facts into the canonical graph: the Matcher assigns
// persistent identities per file lineage, the Projector emits nodes and edges,
// and the Differ turns that fact stream into versioned changes against the
// baseline generation. Every stage reads the in-process semantic.Workspace;
// none interprets language source text.
package graphanalysis

import (
	"encoding/json"
	"strings"

	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// Version fingerprints the identity and projection rules of this package.
const Version = "graph-analysis-v3-lineage"

// Site node kinds emitted for occurrences that no lookup covers.
const (
	NodeCallSite          = "call_site"
	NodeCallableReference = "callable_reference"
	NodeLambda            = "lambda"
	NodeReference         = "reference"
	NodeTypeUse           = "type_use"
	NodeImport            = "import"
	NodeAnnotation        = "annotation"
	NodeModuleDirective   = "module_directive"
)

func anchor(in semantic.SourceInput, span ir.Span) graph.SourceAnchor {
	return graph.SourceAnchor{Lineage: in.Lineage, ContentSHA256: in.Source.ContentSHA256, Span: span}
}

// fileAnchor is an explicit zero-length range at the beginning of the file:
// evidence of file membership, not an invented whole-file end position.
func fileAnchor(in semantic.SourceInput) graph.SourceAnchor {
	return anchor(in, ir.Span{Start: ir.Position{Line: 1}, End: ir.Position{Line: 1}})
}

// CanonicalKey is the JSON form of a declaration key. It is part of external
// entity IDs and is exposed for diagnostics; no parser signature is promoted.
func CanonicalKey(key *semantic.DeclarationKey) string {
	if key == nil {
		return ""
	}
	b, _ := json.Marshal(key)
	return string(b)
}

func keyDigest(key *semantic.DeclarationKey) string { return graph.Digest(key) }

// occurrences lists every identity-bearing site of a file in table order:
// calls, callable references, lambdas, references, type uses, imports,
// annotations and module directives.
func occurrences(file ir.SourceFile) []ir.Occurrence {
	out := make([]ir.Occurrence, 0, len(file.Calls)+len(file.CallableReferences)+len(file.Lambdas)+len(file.References)+len(file.TypeUses)+len(file.Imports)+len(file.Annotations))
	for _, v := range file.Calls {
		out = append(out, v.Occurrence)
	}
	for _, v := range file.CallableReferences {
		out = append(out, v.Occurrence)
	}
	for _, v := range file.Lambdas {
		out = append(out, v.Occurrence)
	}
	for _, v := range file.References {
		out = append(out, v.Occurrence)
	}
	for _, v := range file.TypeUses {
		out = append(out, v.Occurrence)
	}
	for _, v := range file.Imports {
		out = append(out, v.Occurrence)
	}
	for _, v := range file.Annotations {
		out = append(out, v.Occurrence)
	}
	if file.Module != nil {
		for _, v := range file.Module.Directives {
			out = append(out, v.Occurrence)
		}
	}
	return out
}

// siteKinds maps every occurrence to the node kind its standalone site takes.
func siteKinds(file ir.SourceFile) map[ir.OccurrenceID]string {
	kinds := make(map[ir.OccurrenceID]string, len(file.Calls)+len(file.References)+len(file.TypeUses))
	for _, v := range file.Calls {
		kinds[v.Occurrence.ID] = NodeCallSite
	}
	for _, v := range file.CallableReferences {
		kinds[v.Occurrence.ID] = NodeCallableReference
	}
	for _, v := range file.Lambdas {
		kinds[v.Occurrence.ID] = NodeLambda
	}
	for _, v := range file.References {
		kinds[v.Occurrence.ID] = NodeReference
	}
	for _, v := range file.TypeUses {
		kinds[v.Occurrence.ID] = NodeTypeUse
	}
	for _, v := range file.Imports {
		kinds[v.Occurrence.ID] = NodeImport
	}
	for _, v := range file.Annotations {
		kinds[v.Occurrence.ID] = NodeAnnotation
	}
	if file.Module != nil {
		for _, v := range file.Module.Directives {
			kinds[v.Occurrence.ID] = NodeModuleDirective
		}
	}
	return kinds
}

// siteNames maps every occurrence to the name it spells: the called or
// referenced name, the type spelling, the import text, or the directive. Sites
// without a name (lambdas) map to "". Together with the site kind and the
// enclosing entity this identifies a site across revisions of its file.
func siteNames(file ir.SourceFile) map[ir.OccurrenceID]string {
	types := make(map[ir.TypeRefID]string, len(file.Types))
	for _, t := range file.Types {
		types[t.ID] = t.Spelling
	}
	joined := func(n ir.Name) string {
		parts := make([]string, len(n.Segments))
		for i, seg := range n.Segments {
			parts[i] = seg.Text
		}
		return strings.Join(parts, ".")
	}
	names := make(map[ir.OccurrenceID]string, len(file.Calls)+len(file.References)+len(file.TypeUses))
	for _, v := range file.Calls {
		name := v.Name
		if name == "" && v.ConstructedTypeID != "" {
			name = "new " + types[v.ConstructedTypeID]
		}
		names[v.Occurrence.ID] = name
	}
	for _, v := range file.CallableReferences {
		names[v.Occurrence.ID] = v.Name
	}
	for _, v := range file.Lambdas {
		names[v.Occurrence.ID] = ""
	}
	for _, v := range file.References {
		names[v.Occurrence.ID] = joined(v.Name)
	}
	for _, v := range file.TypeUses {
		names[v.Occurrence.ID] = types[v.TypeRefID]
	}
	for _, v := range file.Imports {
		names[v.Occurrence.ID] = v.Spelling
	}
	for _, v := range file.Annotations {
		names[v.Occurrence.ID] = types[v.TypeRefID]
	}
	if file.Module != nil {
		for _, v := range file.Module.Directives {
			name := string(v.Kind)
			if v.Name != nil {
				name += " " + joined(*v.Name)
			}
			names[v.Occurrence.ID] = name
		}
	}
	return names
}
