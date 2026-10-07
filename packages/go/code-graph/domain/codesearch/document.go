// Package codesearch defines stable, source-backed documents for semantic
// retrieval. Embeddings are derived indexes, independent of graph history.
package codesearch

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
	"unicode"
	"unicode/utf8"

	"ei-aitiger-codegraph/pkg/graph"
)

const DocumentVersion = 1
const MaxChunkBytes = 32 << 10

type Document struct {
	NodeID  string `json:"node_id"`
	Text    string `json:"text"`
	Hash    string `json:"hash"`
	Version int    `json:"version"`
}

func Property(n graph.Node, key string) string {
	v := n.Properties[key]
	if v.String != nil {
		return *v.String
	}
	return ""
}

// FromNode excludes run IDs, commits, source offsets and stored vectors. A
// location-only update therefore does not invalidate an unchanged code document.
// The text is reconstructed from persisted fields, not duplicated in the node.
func FromNode(n graph.Node) (Document, bool) {
	eligible := false
	switch n.Kind {
	case "class", "interface", "enum", "record", "annotation_type", "type_alias", "namespace", "method", "constructor", "function", "field", "variable", "enum_constant", "initializer", "code_chunk":
		eligible = true
	}
	if !eligible {
		return Document{}, false
	}
	var b strings.Builder
	fmt.Fprintf(&b, "%s %s\nQualified name: %s", n.Kind, n.Name, n.QualifiedName)
	for _, entry := range []struct{ key, label string }{{"file_path", "File"}, {"language", "Language"}, {"owner_key", "Owner"}, {"signature", "Signature"}, {"canonical_signature", "Resolved signature"}, {"docstring", "Documentation"}, {"members", "Members"}, {"source_text", "Code"}} {
		if value := Property(n, entry.key); value != "" {
			fmt.Fprintf(&b, "\n%s:\n%s", entry.label, value)
		}
	}
	text := b.String()
	h := sha256.Sum256([]byte(fmt.Sprintf("code-document-v%d\n%s", DocumentVersion, text)))
	return Document{NodeID: n.ID, Text: text, Hash: hex.EncodeToString(h[:]), Version: DocumentVersion}, true
}

// Terms splits query or document text into lower-case lexical terms: runs of
// letters, digits and underscores of at least two characters, deduplicated,
// at most 24. It is the shared tokenizer of the lexical branch, the matched-
// term report and the snippet chooser.
func Terms(text string) []string { return collectTerms(text, nil) }

const maxTerms = 24

// collectTerms tokenizes like Terms and keeps only terms the filter accepts,
// so the cap applies to the terms that will be used, not to the raw text.
func collectTerms(text string, keep func(string) bool) []string {
	var out []string
	seen := map[string]bool{}
	for _, term := range strings.FieldsFunc(strings.ToLower(text), func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsNumber(r) && r != '_' }) {
		if len(term) < 2 || seen[term] || (keep != nil && !keep(term)) {
			continue
		}
		seen[term] = true
		out = append(out, term)
		if len(out) == maxTerms {
			break
		}
	}
	return out
}

// IdentifierTerms renders the lexical forms of a symbol name for indexing:
// the name and qualified name as written, every dotted, camelCase, snake_case
// and digit-boundary fragment of them, and the concatenated lower-case name.
// A query for "async", "AsyncImpl" or "run async" then reaches runAsyncImpl,
// and "llm agent" reaches com.google.adk.agents.LlmAgent.
func IdentifierTerms(name, qualifiedName string) string {
	var out []string
	seen := map[string]bool{}
	add := func(s string) {
		if s == "" || seen[s] {
			return
		}
		seen[s] = true
		out = append(out, s)
	}
	for _, source := range []string{name, qualifiedName} {
		add(source)
		for _, segment := range strings.FieldsFunc(source, func(r rune) bool {
			return !unicode.IsLetter(r) && !unicode.IsNumber(r) && r != '_'
		}) {
			add(segment)
			for _, fragment := range SplitIdentifier(segment) {
				add(fragment)
			}
		}
	}
	return strings.Join(out, " ")
}

// SplitIdentifier breaks one identifier into its camelCase, snake_case and
// digit-boundary fragments, in order: "runAsyncImpl" is run, Async, Impl;
// "HTTPServer2" is HTTP, Server, 2; "max_heap_mib" is max, heap, mib.
func SplitIdentifier(identifier string) []string {
	var out []string
	runes := []rune(identifier)
	start := 0
	kind := func(r rune) int {
		switch {
		case unicode.IsUpper(r):
			return 1
		case unicode.IsLower(r):
			return 2
		case unicode.IsNumber(r):
			return 3
		}
		return 0
	}
	flush := func(end int) {
		if end > start {
			out = append(out, string(runes[start:end]))
		}
		start = end
	}
	for i := 1; i <= len(runes); i++ {
		if i == len(runes) {
			flush(i)
			break
		}
		prev, cur := kind(runes[i-1]), kind(runes[i])
		switch {
		case cur == 0:
			flush(i)
			start = i + 1
		case prev == 0:
			continue
		case prev != cur && !(prev == 1 && cur == 2):
			// lower→Upper, letter↔digit: a boundary before the current rune.
			flush(i)
		case prev == 1 && cur == 2 && i-1 > start && kind(runes[i-2]) == 1:
			// "HTTPServer": the last upper-case letter starts the next word.
			flush(i - 1)
		}
	}
	return out
}

// Snippet returns a bounded window of text around the first occurrence of any
// term, on whole lines where possible, for showing an agent why a hit matched.
func Snippet(text string, terms []string, maxBytes int) string {
	if maxBytes <= 0 || text == "" {
		return ""
	}
	lower := strings.ToLower(text)
	at := -1
	for _, term := range terms {
		if i := strings.Index(lower, term); i >= 0 && (at < 0 || i < at) {
			at = i
		}
	}
	if at < 0 {
		at = 0
	}
	start := at - maxBytes/3
	if start < 0 {
		start = 0
	}
	if i := strings.LastIndexByte(text[:at], '\n'); i >= 0 && i >= start {
		start = i + 1
	}
	end := start + maxBytes
	if end >= len(text) {
		end = len(text)
	} else {
		if i := strings.IndexByte(text[end:], '\n'); i >= 0 && i < maxBytes/4 {
			end += i
		}
		for end > start && !utf8.RuneStart(text[end]) {
			end--
		}
	}
	for start < end && !utf8.RuneStart(text[start]) {
		start++
	}
	return strings.TrimSpace(text[start:end])
}
