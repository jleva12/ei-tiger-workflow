package graphanalysis

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"unicode/utf8"

	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// Signatures and documentation are summaries of a declaration, bounded below
// the graph's property limit: a Java field's signature carries its
// initializer, which can be a whole table of data. The full text stays in
// source_text and its chunks.
const (
	maxSignatureBytes = 4 << 10
	maxDocstringBytes = 16 << 10
)

func searchProperties(n *graph.Node, source ir.Source, d ir.Declaration, key *semantic.DeclarationKey) {
	n.Properties = map[string]graph.PropertyValue{"file_path": graph.StringValue(source.Path), "language": graph.StringValue(source.Language)}
	if d.SignatureText != "" {
		n.Properties["signature"] = graph.StringValue(summaryText(d.SignatureText, maxSignatureBytes))
	}
	if d.DocComment != "" {
		n.Properties["docstring"] = graph.StringValue(summaryText(d.DocComment, maxDocstringBytes))
	}
	if key != nil {
		n.Properties["owner_key"] = graph.StringValue(key.OwnerKey)
		n.Properties["canonical_signature"] = graph.StringValue(key.CanonicalSignature)
	}
}

// Names, keys and reasons are bounded too. A name is indexed for exact
// lookup (Spanner caps an index key at 8 KiB, and name search accepts at most
// 1 KiB), and an inferred or generated type can have a canonical signature
// of any length. A source file's name is its path, which discovery already
// bounds at 4 KiB. Everything else is only cleaned and held to the graph's
// property limit.
const (
	maxNameBytes   = 1 << 10
	maxKeyBytes    = 4 << 10
	maxReasonBytes = 4 << 10
)

var propertyBytes = map[string]int{
	"owner_key":           maxKeyBytes,
	"canonical_signature": maxKeyBytes,
	"reason":              maxReasonBytes,
	"signature":           maxSignatureBytes,
	"docstring":           maxDocstringBytes,
}

// cleanNode makes a node's text storable whatever the source bytes were:
// valid UTF-8 without NUL bytes, within the bounds above. It runs before a
// node's search document is sealed, so the sealed hash describes the stored
// text, and again on emit, where it no longer changes anything.
func cleanNode(n *graph.Node) {
	nameBytes := maxNameBytes
	if n.Kind == graph.NodeSourceFile {
		nameBytes = maxKeyBytes
	}
	n.Name = summaryText(n.Name, nameBytes)
	n.QualifiedName = summaryText(n.QualifiedName, maxKeyBytes)
	cleanProperties(n.Properties)
}

func cleanProperties(properties map[string]graph.PropertyValue) {
	for k, v := range properties {
		max, ok := propertyBytes[k]
		if !ok {
			max = graph.MaxTextBytes
		}
		switch {
		case v.String != nil:
			if text := summaryText(*v.String, max); text != *v.String {
				properties[k] = graph.StringValue(text)
			}
		case v.Strings != nil:
			list, changed := *v.Strings, false
			for i, s := range list {
				if text := summaryText(s, max); text != s {
					if !changed {
						list, changed = append([]string(nil), list...), true
					}
					list[i] = text
				}
			}
			if changed {
				properties[k] = graph.StringsValue(list)
			}
		}
	}
}

// summaryText is source text a graph property can hold: valid UTF-8 without
// NUL bytes, cut to max bytes on a character boundary with an ellipsis.
func summaryText(text string, max int) string {
	text = strings.ReplaceAll(strings.ToValidUTF8(text, "�"), "\x00", "")
	if len(text) <= max {
		return text
	}
	const more = "…"
	cut := max - len(more)
	for cut > 0 && !utf8.RuneStart(text[cut]) {
		cut--
	}
	return text[:cut] + more
}

// Member lists are bounded so a type's document stays a summary of what it
// declares, not a second copy of the file.
const (
	maxMemberEntries = 200
	maxMemberBytes   = 8 << 10
)

// containerMembers lists, per type declaration, the members it declares as
// "kind name" lines in source order: what a class is made of, so a search
// for a concept finds the type that groups its methods. Anonymous members
// and type parameters are skipped; the list is cut at the bounds above.
func containerMembers(declarations []ir.Declaration) map[ir.DeclarationID]string {
	kinds := map[ir.DeclarationID]ir.DeclarationKind{}
	for _, d := range declarations {
		switch d.Kind {
		case ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationRecord, ir.DeclarationAnnotationType:
			kinds[d.ID] = d.Kind
		}
	}
	lists := map[ir.DeclarationID][]string{}
	sizes := map[ir.DeclarationID]int{}
	for _, d := range declarations {
		if d.OwnerID == "" || d.Name == "" || d.Kind == ir.DeclarationTypeParameter {
			continue
		}
		if _, ok := kinds[d.OwnerID]; !ok {
			continue
		}
		entry := string(d.Kind) + " " + d.Name
		if len(lists[d.OwnerID]) >= maxMemberEntries || sizes[d.OwnerID]+len(entry)+1 > maxMemberBytes {
			continue
		}
		lists[d.OwnerID] = append(lists[d.OwnerID], entry)
		sizes[d.OwnerID] += len(entry) + 1
	}
	out := make(map[ir.DeclarationID]string, len(lists))
	for id, list := range lists {
		out[id] = strings.Join(list, "\n")
	}
	return out
}

// sealSearchDocument records the hash of the retrieval document a node
// reconstructs, so a location-only update never re-embeds unchanged code.
func sealSearchDocument(n *graph.Node) {
	if doc, ok := codesearch.FromNode(*n); ok {
		if n.Properties == nil {
			n.Properties = map[string]graph.PropertyValue{}
		}
		n.Properties["search_document_hash"] = graph.StringValue(doc.Hash)
		n.Properties["search_document_version"] = graph.Int64Value(codesearch.DocumentVersion)
	}
}

// lineIndex maps byte offsets of one file to positions. It is built once per
// file and only when a declaration is large enough to be chunked.
type lineIndex struct {
	content []byte
	starts  []uint64
}

func (l *lineIndex) position(offset uint64) ir.Position {
	if l.starts == nil {
		l.starts = append(l.starts, 0)
		for i, c := range l.content {
			if c == '\n' {
				l.starts = append(l.starts, uint64(i+1))
			}
		}
	}
	i := sort.Search(len(l.starts), func(i int) bool { return l.starts[i] > offset }) - 1
	return ir.Position{ByteOffset: offset, Line: uint32(i + 1), Column: uint32(offset - l.starts[i])}
}

// retainDeclarationText keeps callable and field bodies once on their entity,
// or in bounded child chunks for large bodies. Type nodes use their signature
// and documentation instead of repeating their members' implementations.
func retainDeclarationText(ctx context.Context, n *graph.Node, in semantic.SourceInput, d ir.Declaration, lines *lineIndex, emit semantic.Emit) error {
	switch d.Kind {
	case ir.DeclarationMethod, ir.DeclarationConstructor, ir.DeclarationFunction, ir.DeclarationInitializer, ir.DeclarationField, ir.DeclarationEnumConstant:
	default:
		return nil
	}
	content := lines.content
	start, end := d.Span.Start.ByteOffset, d.Span.End.ByteOffset
	if end > uint64(len(content)) || start > end {
		return fmt.Errorf("%w: declaration %s spans outside its file", semantic.ErrIntegrity, d.ID)
	}
	text := content[start:end]
	if len(text) <= codesearch.MaxChunkBytes {
		n.Properties["source_text"] = graph.StringValue(summaryText(string(text), graph.MaxTextBytes))
		return nil
	}
	for ordinal, offset := 0, start; offset < end; ordinal++ {
		stop := min(end, offset+codesearch.MaxChunkBytes)
		if stop < end {
			// Cut on a character boundary. Bytes that are not UTF-8 may
			// have none nearby; the chunk is then cut where it is.
			cut := stop
			for back := 0; back < utf8.UTFMax && cut > offset && !utf8.RuneStart(content[cut]); back++ {
				cut--
			}
			if cut > offset && utf8.RuneStart(content[cut]) {
				stop = cut
			}
		}
		a := anchor(in, ir.Span{Start: lines.position(offset), End: lines.position(stop)})
		id := graph.ID(graph.NodeCodeChunk, n.ID, fmt.Sprint(ordinal))
		chunk := graph.Node{ID: id, Kind: graph.NodeCodeChunk, Name: n.Name, QualifiedName: n.QualifiedName, Source: &a, Properties: map[string]graph.PropertyValue{
			"file_path":     graph.StringValue(in.Source.Path),
			"language":      graph.StringValue(in.Source.Language),
			"source_text":   graph.StringValue(string(content[offset:stop])),
			"entity_id":     graph.StringValue(n.ID),
			"chunk_ordinal": graph.Int64Value(int64(ordinal)),
		}}
		cleanNode(&chunk)
		sealSearchDocument(&chunk)
		if err := emit(ctx, graph.Fact{Node: &chunk}); err != nil {
			return err
		}
		edge := graph.Edge{ID: graph.ID("edge", graph.EdgeHasChunk, n.ID, id, ""), Kind: graph.EdgeHasChunk, SourceID: n.ID, TargetID: id, Source: &a}
		if err := emit(ctx, graph.Fact{Edge: &edge}); err != nil {
			return err
		}
		offset = stop
	}
	n.Properties["source_text_chunked"] = graph.BoolValue(true)
	return nil
}
