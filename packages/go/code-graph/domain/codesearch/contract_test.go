package codesearch

import (
	"bytes"
	"encoding/json"
	"flag"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
)

var update = flag.Bool("update", false, "rewrite testdata/reader_contract.json")

// readerContract is what the readers (the Python package
// forge_codegraph_query) must reproduce from what ingestion writes: the
// document of a node decoded from its CGRecords payload, whose hash a search
// row must match or the row is dropped as stale; graph IDs; and the windowed,
// length-weighted embedding transform applied to queries.
type readerContract struct {
	DocumentVersion int                `json:"document_version"`
	Documents       []contractDocument `json:"documents"`
	GraphIDs        []contractID       `json:"graph_ids"`
	Embedding       contractEmbedding  `json:"embedding"`
}

type contractDocument struct {
	// Node is the node as the store serializes it into a CGRecords payload,
	// json.Marshal(graph.Fact{Node: &n}), whose "node" member this is.
	Node     json.RawMessage `json:"node"`
	Document *struct {
		NodeID string `json:"node_id"`
		Text   string `json:"text"`
		Hash   string `json:"hash"`
	} `json:"document"`
}

type contractID struct {
	Kind  string   `json:"kind"`
	Parts []string `json:"parts"`
	ID    string   `json:"id"`
}

type contractEmbedding struct {
	MaxInputBytes int         `json:"max_input_bytes"`
	Dimensions    int         `json:"dimensions"`
	Text          string      `json:"text"`
	Windows       []string    `json:"windows"`
	WindowVectors [][]float64 `json:"window_vectors"`
	Vector        []float64   `json:"vector"`
}

func contractNodes() []graph.Node {
	repo := graph.ID("repo", "https://github.com/acme/shop")
	servicePath := "core/src/main/java/shop/OrderService.java"
	lineage := graph.Lineage(repo, "core", "main", servicePath)
	return []graph.Node{
		{
			// A method with every text property, a source anchor and
			// properties the document ignores.
			ID: graph.ID("entity", repo, "run-1", lineage, "decl-7"), Kind: "method", Name: "placeOrder", QualifiedName: "shop.OrderService.placeOrder(shop.Cart)",
			Source: &graph.SourceAnchor{Lineage: lineage, ContentSHA256: strings.Repeat("ab", 32), Span: ir.Span{Start: ir.Position{ByteOffset: 120, Line: 9, Column: 5}, End: ir.Position{ByteOffset: 260, Line: 13, Column: 6}}},
			Properties: map[string]graph.PropertyValue{
				"file_path":           graph.StringValue(servicePath),
				"language":            graph.StringValue("java"),
				"owner_key":           graph.StringValue("shop.OrderService"),
				"signature":           graph.StringValue("public Order placeOrder(Cart cart)"),
				"canonical_signature": graph.StringValue("shop.OrderService.placeOrder(shop.Cart)"),
				"docstring":           graph.StringValue("Places an order & reserves its stock <atomically>."),
				"source_text":         graph.StringValue("public Order placeOrder(Cart cart) {\n    inventory.reserve(cart);\n    return orders.save(cart);\n}"),
				"start_line":          graph.Int64Value(9),
				"static":              graph.BoolValue(false),
				"modifiers":           graph.StringsValue([]string{"public"}),
			},
		},
		{
			// A class lists its members; an empty docstring is left out.
			ID: graph.ID("entity", repo, "run-1", lineage, "decl-1"), Kind: "class", Name: "OrderService", QualifiedName: "shop.OrderService",
			Properties: map[string]graph.PropertyValue{
				"file_path": graph.StringValue(servicePath),
				"language":  graph.StringValue("java"),
				"docstring": graph.StringValue(""),
				"members":   graph.StringValue("method placeOrder\nfield inventory\nfield orders"),
			},
		},
		{
			// Unicode everywhere, including a line separator the JSON
			// encoding escapes.
			ID: graph.ID("python-symbol", "file-9", "decl-2"), Kind: "function", Name: "grüßen", QualifiedName: "app.grüße.grüßen",
			Properties: map[string]graph.PropertyValue{
				"file_path":   graph.StringValue("app/grüße.py"),
				"language":    graph.StringValue("python"),
				"docstring":   graph.StringValue("Begrüßt die 世界 — λ ✓ zweite Zeile"),
				"source_text": graph.StringValue("def grüßen(name: str) -> str:\n    return f\"👋 {name}\""),
			},
		},
		{
			// No name, no qualified name and no properties still make a
			// document; a text key holding a non-string value is left out.
			ID: graph.ID("entity", repo, "run-1", lineage, "decl-9"), Kind: "variable",
			Properties: map[string]graph.PropertyValue{"signature": graph.StringsValue([]string{"int", "count"})},
		},
		{
			ID: graph.ID(graph.NodeCodeChunk, graph.ID("entity", repo, "run-1", lineage, "decl-7"), "0"), Kind: graph.NodeCodeChunk, Name: "placeOrder#0", QualifiedName: "shop.OrderService.placeOrder(shop.Cart)#0",
			Properties: map[string]graph.PropertyValue{"file_path": graph.StringValue(servicePath), "source_text": graph.StringValue("inventory.reserve(cart);")},
		},
		// Kinds without a document.
		{ID: lineage, Kind: graph.NodeSourceFile, Name: servicePath, Properties: map[string]graph.PropertyValue{"file_path": graph.StringValue(servicePath), "language": graph.StringValue("java")}},
		{ID: graph.ID("external", repo, "maven:java.base", "jdk-21", "java.util.List"), Kind: graph.NodeExternalSymbol, Name: "List", QualifiedName: "java.util.List"},
	}
}

func contractIDs() []contractID {
	repo := graph.ID("repo", "https://github.com/acme/shop")
	ids := []contractID{
		{Kind: "repo", Parts: []string{"https://github.com/acme/shop"}},
		{Kind: "file", Parts: []string{repo, "core", "main", "core/src/main/java/shop/OrderService.java"}},
		{Kind: "edge", Parts: []string{graph.EdgeCalls, graph.ID("entity", "a"), graph.ID("entity", "b"), ""}},
		// Go's JSON escapes <, > and & (and U+2028, U+2029) before hashing.
		{Kind: "entity", Parts: []string{"List<String> & Map<K,V>"}},
		{Kind: "sym", Parts: []string{"grüßen", "世界", "line separator", "para graph"}},
		{Kind: "lookup", Parts: []string{"quote\" backslash\\ newline\n tab\t control\x01 bell\b feed\f"}},
		{Kind: graph.NodeCodeChunk, Parts: []string{graph.ID("entity", "a"), "0"}},
	}
	for i := range ids {
		ids[i].ID = graph.ID(ids[i].Kind, ids[i].Parts...)
	}
	return ids
}

// contractEmbeddingOf windows text, gives each window a fixed vector and
// combines them as the embedding pass does.
func contractEmbeddingOf(t *testing.T) contractEmbedding {
	t.Helper()
	e := contractEmbedding{MaxInputBytes: 64, Dimensions: 4, Text: strings.Repeat("Grüße aus 世界: λ-Kalkül → ✓; ", 5)}
	ws, err := windows(e.Text, e.MaxInputBytes)
	if err != nil {
		t.Fatal(err)
	}
	backedOff := false
	for i, w := range ws[:len(ws)-1] {
		backedOff = backedOff || len(w) < e.MaxInputBytes
		if len(w) > e.MaxInputBytes || strings.Join(ws, "") != e.Text {
			t.Fatalf("window %d: %q", i, w)
		}
	}
	if len(ws) < 3 || !backedOff {
		t.Fatalf("the text must span three or more windows and split before a multi-byte rune: %q", ws)
	}
	e.Windows = ws
	for i := range ws {
		v := make([]float64, e.Dimensions)
		for j := range v {
			v[j] = math.Round(math.Sin(float64(1+i*e.Dimensions+j))*1000) / 1000
		}
		e.WindowVectors = append(e.WindowVectors, v)
	}
	if e.Vector, err = combine(ws, e.WindowVectors, e.Dimensions); err != nil {
		t.Fatal(err)
	}
	return e
}

func buildReaderContract(t *testing.T) []byte {
	t.Helper()
	c := readerContract{DocumentVersion: DocumentVersion, GraphIDs: contractIDs(), Embedding: contractEmbeddingOf(t)}
	for _, n := range contractNodes() {
		payload, err := json.Marshal(graph.Fact{Node: &n})
		if err != nil {
			t.Fatal(err)
		}
		var fact struct {
			Node json.RawMessage `json:"node"`
		}
		if err = json.Unmarshal(payload, &fact); err != nil {
			t.Fatal(err)
		}
		entry := contractDocument{Node: fact.Node}
		if d, ok := FromNode(n); ok {
			entry.Document = &struct {
				NodeID string `json:"node_id"`
				Text   string `json:"text"`
				Hash   string `json:"hash"`
			}{d.NodeID, d.Text, d.Hash}
		}
		c.Documents = append(c.Documents, entry)
	}
	out, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(out, '\n')
}

// TestReaderContract keeps testdata/reader_contract.json, the fixture the
// Python readers test against, in step with ingestion. When this fails
// because ingestion changed on purpose, regenerate it with
//
//	go test ./domain/codesearch -run TestReaderContract -update
//
// and make the readers pass against the new file.
func TestReaderContract(t *testing.T) {
	got := buildReaderContract(t)
	path := filepath.Join("testdata", "reader_contract.json")
	if *update {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, got, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v; generate it with -update", err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("%s is out of date: ingestion now hashes documents, derives IDs or embeds text differently. If that is intended, regenerate it with -update and update the readers (forge_codegraph_query) to match", path)
	}
}
