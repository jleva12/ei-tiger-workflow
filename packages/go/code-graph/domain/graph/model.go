// Package graph defines the canonical code graph: nodes, edges, source anchors,
// and the versioned record form in which they are stored. Every stored fact is
// a version bounded by the generations that opened and closed it, so the live
// graph, any historical generation, and a node's timeline are all views over the
// same rows. Nothing here depends on a storage engine.
package graph

import "ei-aitiger-codegraph/pkg/ir"

// PropertyValue is a closed union: exactly one field is non-nil. Missing map
// keys mean absent values; nested objects and non-finite floats are not
// supported. Int64 values survive a JSON round trip without float conversion.
type PropertyValue struct {
	String  *string   `json:"string,omitempty"`
	Int64   *int64    `json:"int64,omitempty"`
	Float64 *float64  `json:"float64,omitempty"`
	Bool    *bool     `json:"bool,omitempty"`
	Strings *[]string `json:"strings,omitempty"`
}

func StringValue(value string) PropertyValue   { return PropertyValue{String: &value} }
func Int64Value(value int64) PropertyValue     { return PropertyValue{Int64: &value} }
func Float64Value(value float64) PropertyValue { return PropertyValue{Float64: &value} }
func BoolValue(value bool) PropertyValue       { return PropertyValue{Bool: &value} }
func StringsValue(value []string) PropertyValue {
	cloned := append([]string{}, value...)
	return PropertyValue{Strings: &cloned}
}

// Text returns the string form of a property, or "" when absent or not a string.
func Text(properties map[string]PropertyValue, key string) string {
	if v, ok := properties[key]; ok && v.String != nil {
		return *v.String
	}
	return ""
}

// SourceAnchor points at exact bytes of one file variant: the file lineage,
// the content hash of the bytes the span was measured against, and a half-open
// byte span. The content hash is also the key of the retained source object,
// so an anchor is a complete evidence pointer.
type SourceAnchor struct {
	Lineage       string  `json:"lineage"`
	ContentSHA256 string  `json:"content_sha256"`
	Span          ir.Span `json:"span"`
}

// Node kinds emitted by the projector. Language adapters may add declaration
// kinds (class, method, field ...) taken directly from ir.DeclarationKind.
const (
	NodeSourceFile          = "source_file"
	NodeExternalSymbol      = "external_symbol"
	NodeIntrinsic           = "intrinsic"
	NodeConstructedType     = "constructed_type"
	NodeUnresolvedReference = "unresolved_reference"
	NodeCodeChunk           = "code_chunk"
)

// Edge kinds. Contains is structural; the rest are semantic relationships
// proven by the language resolver. Overrides and Implements let impact queries
// expand a static call target to its runtime candidates.
const (
	EdgeContains         = "contains"
	EdgeCalls            = "calls"
	EdgeUsesType         = "uses_type"
	EdgeReferences       = "references"
	EdgeInherits         = "inherits"
	EdgeOverrides        = "overrides"
	EdgeImplements       = "implements"
	EdgeFrameworkBinding = "framework_binding"
	EdgeDerivedFrom      = "derived_from"
	EdgeTypeComponent    = "type_component"
	EdgeHasChunk         = "has_chunk"
	EdgeSucceededBy      = "succeeded_by"
)

// Node is a canonical projected fact. IDs are persistent: the identity matcher
// continues them across commits wherever it can prove continuity.
type Node struct {
	ID            string                   `json:"id"`
	Kind          string                   `json:"kind"`
	Name          string                   `json:"name,omitempty"`
	QualifiedName string                   `json:"qualified_name,omitempty"`
	Source        *SourceAnchor            `json:"source,omitempty"`
	Properties    map[string]PropertyValue `json:"properties,omitempty"`
}

type Edge struct {
	ID         string                   `json:"id"`
	Kind       string                   `json:"kind"`
	SourceID   string                   `json:"source_id"`
	TargetID   string                   `json:"target_id"`
	Source     *SourceAnchor            `json:"source,omitempty"`
	Properties map[string]PropertyValue `json:"properties,omitempty"`
}

// Fact is a closed node/edge union.
type Fact struct {
	Node *Node `json:"node,omitempty"`
	Edge *Edge `json:"edge,omitempty"`
}

type RecordKind string

const (
	RecordNode RecordKind = "node"
	RecordEdge RecordKind = "edge"
)

type Key struct {
	Kind RecordKind `json:"kind"`
	ID   string     `json:"id"`
}

func (f Fact) Key() Key {
	if f.Node != nil {
		return Key{RecordNode, f.Node.ID}
	}
	if f.Edge != nil {
		return Key{RecordEdge, f.Edge.ID}
	}
	return Key{}
}

// Anchor returns the fact's own source anchor, if any.
func (f Fact) Anchor() *SourceAnchor {
	if f.Node != nil {
		return f.Node.Source
	}
	if f.Edge != nil {
		return f.Edge.Source
	}
	return nil
}

// Version is one stored version of a record. GenFrom is the generation that
// introduced this version; GenTo is zero while the version is open, otherwise
// the generation that replaced it (Retired=false) or retired it (Retired=true).
// CommitFrom/CommitTo are the merge commits behind those generations.
type Version struct {
	Fact       Fact   `json:"fact"`
	Lineage    string `json:"lineage,omitempty"`
	GenFrom    uint64 `json:"gen_from"`
	GenTo      uint64 `json:"gen_to,omitempty"`
	CommitFrom string `json:"commit_from"`
	CommitTo   string `json:"commit_to,omitempty"`
	Retired    bool   `json:"retired,omitempty"`
	FactDigest string `json:"fact_digest,omitempty"`
}

// OpenAt reports whether this version is the visible one at a generation.
func (v Version) OpenAt(generation uint64) bool {
	return v.GenFrom <= generation && (v.GenTo == 0 || v.GenTo > generation)
}

// Operation is one change the loader applies for generation G.
type Operation string

const (
	OpAdd    Operation = "add"    // new record: insert version (gen_from=G)
	OpUpdate Operation = "update" // changed record: close open version at G, insert new version
	OpRetire Operation = "retire" // record gone: close open version at G with Retired=true
	OpReopen Operation = "reopen" // record reappeared after retirement: insert version (gen_from=G)
)

// Change pairs an operation with the fact it concerns. Before is the open
// version being closed for update/retire; After is the new fact for add,
// update and reopen.
type Change struct {
	Op      Operation `json:"op"`
	Key     Key       `json:"key"`
	Lineage string    `json:"lineage,omitempty"`
	Before  *Version  `json:"before,omitempty"`
	After   *Fact     `json:"after,omitempty"`
}
