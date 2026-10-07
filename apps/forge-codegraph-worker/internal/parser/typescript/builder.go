package typescript

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"

	sitter "github.com/tree-sitter/go-tree-sitter"
)

type stop struct{ err error }

type environment struct {
	scope ir.ScopeID
	owner ir.DeclarationID
	// returns collects what the return statements of the nearest named
	// function or method return; nil inside anonymous function literals,
	// whose returns are theirs, not the enclosing function's.
	returns *[]ir.ExpressionID
}

// exportContext carries what an export statement contributes to the
// declaration it wraps: the export and default modifiers, decorators written
// before the export keyword, and the documentation comment above it.
type exportContext struct {
	span       ir.Span
	defaultKw  *ir.Span
	doc        string
	decorators []*sitter.Node
}

// builder owns all per-file extraction state; no native node escapes Parse.
type builder struct {
	// issuesCut is set once the diagnostic budget cut the issues.
	issuesCut        bool
	ctx              context.Context
	input            parser.Input
	dialect          string
	text             string
	lines            []uint
	file             ir.SourceFile
	serial           uint64
	records          uint64
	bytes            uint64
	sizes            map[string]uint64
	expressions      map[string]ir.ExpressionID
	declarationIndex map[ir.DeclarationID]int
	exporting        *exportContext
	pendingDecorator []*sitter.Node
	exports          []exportedName
}

func newBuilder(ctx context.Context, input parser.Input, dialect string) *builder {
	configuration, _ := json.Marshal(struct {
		Language, Version string
		Options           parser.Options
	}{input.Source.Language, input.Source.LanguageVersion, input.Options})
	digest := sha256.Sum256(configuration)
	b := &builder{
		ctx: ctx, input: input, dialect: dialect, text: string(input.Content), lines: []uint{0},
		sizes: map[string]uint64{}, expressions: map[string]ir.ExpressionID{}, declarationIndex: map[ir.DeclarationID]int{},
	}
	for i := 0; i < len(input.Content); i++ {
		if input.Content[i] == '\r' {
			if i+1 < len(input.Content) && input.Content[i+1] == '\n' {
				i++
			}
			b.lines = append(b.lines, uint(i+1))
		} else if input.Content[i] == '\n' {
			b.lines = append(b.lines, uint(i+1))
		}
	}
	b.file = ir.SourceFile{
		SchemaVersion: ir.SchemaVersion, Source: input.Source,
		Producer: ir.Producer{Name: "ei-typescript-tree-sitter", Version: Version, Grammar: "tree-sitter-" + dialect, GrammarVersion: GrammarVersion, ConfigDigest: hex.EncodeToString(digest[:])},
		Coverage: ir.ExtractionCoverage{Status: ir.ExtractionComplete, FeatureSet: FeatureSet},
		// Syntax extraction never type-checks; a compiler-backed validation
		// is a later stage.
		LanguageValidation: &ir.LanguageValidation{Status: ir.LanguageNotChecked, Method: "typescript-syntax-profile", Release: input.Source.LanguageVersion},
	}
	b.file.RootScopeID = b.scope(ir.ScopeFile, b.rawSpan(0, uint(len(b.text))), environment{})
	return b
}

func (b *builder) check() {
	if err := b.ctx.Err(); err != nil {
		panic(stop{err})
	}
}
func (b *builder) limit(message string) {
	panic(stop{fmt.Errorf("%w: %s", parser.ErrLimitExceeded, message)})
}
func (b *builder) id(kind string, n *sitter.Node) string {
	b.serial++
	if n == nil {
		return fmt.Sprintf("%s:%d", kind, b.serial)
	}
	return fmt.Sprintf("%s:%d:%d:%d", kind, n.StartByte(), n.EndByte(), b.serial)
}
func (b *builder) position(offset uint) ir.Position {
	i := sort.Search(len(b.lines), func(i int) bool { return b.lines[i] > offset }) - 1
	return ir.Position{ByteOffset: uint64(offset), Line: uint32(i + 1), Column: uint32(offset - b.lines[i])}
}
func (b *builder) rawSpan(start, end uint) ir.Span {
	return ir.Span{Start: b.position(start), End: b.position(end)}
}
func (b *builder) span(n *sitter.Node) ir.Span { return b.rawSpan(n.StartByte(), n.EndByte()) }
func (b *builder) spelling(n *sitter.Node) string {
	if n == nil {
		return ""
	}
	return b.text[n.StartByte():n.EndByte()]
}
func valid(n *sitter.Node) bool { return n != nil && !n.IsMissing() && n.EndByte() > n.StartByte() }

// named lists the named children, without comments.
func named(n *sitter.Node) []*sitter.Node {
	if n == nil {
		return nil
	}
	var result []*sitter.Node
	for i := uint(0); i < n.NamedChildCount(); i++ {
		child := n.NamedChild(i)
		if child.Kind() != "comment" {
			result = append(result, child)
		}
	}
	return result
}
func firstNamed(n *sitter.Node) *sitter.Node {
	children := named(n)
	if len(children) == 0 {
		return nil
	}
	return children[0]
}
func childKind(n *sitter.Node, kind string) *sitter.Node {
	for _, child := range named(n) {
		if child.Kind() == kind {
			return child
		}
	}
	return nil
}
func field(n *sitter.Node, name string) *sitter.Node {
	if n == nil {
		return nil
	}
	return n.ChildByFieldName(name)
}
func nodeKey(n *sitter.Node, env environment) string {
	return fmt.Sprintf("%d:%s", n.Id(), env.scope)
}

// Count records before adding them and account each completed record's
// serialized size; finish checks the full envelope.
func (b *builder) record() {
	b.check()
	b.records++
	if b.records > b.input.Limits.MaxIRRecords {
		b.limit("IR record count")
	}
}
func (b *builder) account(key string, record any) {
	b.check()
	data, err := json.Marshal(record)
	if err != nil {
		panic(stop{fmt.Errorf("serialize extracted record: %w", err)})
	}
	b.bytes -= b.sizes[key]
	b.sizes[key] = uint64(len(data))
	b.bytes += uint64(len(data))
	if b.bytes > b.input.Limits.MaxOutputBytes {
		b.limit("IR serialized bytes")
	}
}
func (b *builder) scope(kind ir.ScopeKind, span ir.Span, env environment) ir.ScopeID {
	b.record()
	id := ir.ScopeID(b.id("scope", nil))
	scope := ir.Scope{ID: id, Kind: kind, Span: span, ParentID: env.scope, OwnerDeclarationID: env.owner}
	b.account(string(id), scope)
	b.file.Scopes = append(b.file.Scopes, scope)
	return id
}
func (b *builder) occurrence(n *sitter.Node, env environment) ir.Occurrence {
	return ir.Occurrence{ID: ir.OccurrenceID(b.id("occ", n)), Span: b.span(n), ScopeID: env.scope, EnclosingDeclarationID: env.owner}
}
func (b *builder) issue(n *sitter.Node, feature, message string, reason ir.CoverageReason) {
	var span *ir.Span
	if n != nil {
		s := b.span(n)
		span = &s
	}
	b.check()
	// A file with more issues than the budget keeps its declarations: the
	// last room says the rest were not recorded, and later ones are dropped.
	if recorded := uint64(len(b.file.Diagnostics) + len(b.file.Coverage.Issues)); recorded+4 > uint64(b.input.Limits.MaxDiagnostics) {
		if recorded+2 > uint64(b.input.Limits.MaxDiagnostics) && recorded == 0 {
			b.limit("diagnostic and coverage count") // no room even to say so
		}
		b.file.Coverage.Status = ir.ExtractionPartial
		if b.issuesCut || recorded+2 > uint64(b.input.Limits.MaxDiagnostics) {
			return
		}
		b.issuesCut = true
		span, feature, message, reason = nil, "diagnostic_limit", "further issues in this file were not recorded", ir.CoverageLimit
	}
	severity := ir.SeverityWarning
	if reason == ir.CoverageParseError {
		severity = ir.SeverityError
	}
	d := ir.Diagnostic{Code: "typescript." + feature, Severity: severity, Message: message, Span: span, Feature: feature}
	c := ir.CoverageIssue{Feature: feature, Reason: reason, Message: message, Span: span}
	b.account(b.id("diagnostic", nil), d)
	b.account(b.id("coverage", nil), c)
	b.file.Diagnostics = append(b.file.Diagnostics, d)
	b.file.Coverage.Issues = append(b.file.Coverage.Issues, c)
	b.file.Coverage.Status = ir.ExtractionPartial
}

// scan checks the node and depth budgets and reports every error or missing
// token before extraction starts.
func (b *builder) scan(root *sitter.Node) {
	cursor := root.Walk()
	defer cursor.Close()
	var count uint64
	depth := uint32(1)
	for {
		b.check()
		count++
		if count > b.input.Limits.MaxSyntaxNodes {
			b.limit("syntax node count")
		}
		if depth > b.input.Limits.MaxSyntaxDepth {
			b.limit("syntax depth")
		}
		n := cursor.Node()
		if n.IsError() || n.IsMissing() {
			b.issue(n, "syntax", "Tree-sitter reported an error or missing token", ir.CoverageParseError)
		}
		if cursor.GotoFirstChild() {
			depth++
			continue
		}
		for !cursor.GotoNextSibling() {
			if !cursor.GotoParent() {
				return
			}
			depth--
		}
	}
}

func (b *builder) finish() ir.SourceFile {
	b.check()
	b.applyExportedNames()
	if b.file.Coverage.Status == ir.ExtractionPartial && len(b.file.Declarations) == 0 && len(b.file.Imports) == 0 && len(b.file.Expressions) == 0 {
		b.file.Coverage.Status = ir.ExtractionFailed
	}
	if err := b.file.Validate(); err != nil {
		panic(stop{fmt.Errorf("invalid TypeScript parser output: %w", err)})
	}
	data, err := json.Marshal(b.file)
	if err != nil {
		panic(stop{err})
	}
	if uint64(len(data)) > b.input.Limits.MaxOutputBytes {
		b.limit("IR envelope bytes")
	}
	b.check()
	return b.file
}

// documentation returns a /** */ comment immediately above the node.
func (b *builder) documentation(n *sitter.Node) string {
	if n == nil {
		return ""
	}
	if previous := n.PrevNamedSibling(); previous != nil && previous.Kind() == "comment" && strings.HasPrefix(b.spelling(previous), "/**") && strings.TrimSpace(b.text[previous.EndByte():n.StartByte()]) == "" {
		return b.spelling(previous)
	}
	return ""
}

// stringValue is the content of a string literal node without its quotes.
func (b *builder) stringValue(n *sitter.Node) string {
	s := b.spelling(n)
	if len(s) >= 2 {
		switch s[0] {
		case '"', '\'', '`':
			if s[len(s)-1] == s[0] {
				return s[1 : len(s)-1]
			}
		}
	}
	return s
}

// memberName is the written name of a class or object member.
func (b *builder) memberName(n *sitter.Node) string {
	if n == nil {
		return ""
	}
	if n.Kind() == "string" {
		return b.stringValue(n)
	}
	return b.spelling(n)
}

// take consumes the pending export context, if any.
func (b *builder) take() *exportContext {
	ex := b.exporting
	b.exporting = nil
	return ex
}

// applyExport adds the export statement's contribution to a declaration.
func (b *builder) applyExport(d *ir.Declaration, ex *exportContext) {
	if ex == nil {
		return
	}
	d.Modifiers = append(d.Modifiers, ir.Modifier{Keyword: "export", Span: ex.span})
	if ex.defaultKw != nil {
		d.Modifiers = append(d.Modifiers, ir.Modifier{Keyword: "default", Span: *ex.defaultKw})
	}
	if d.DocComment == "" {
		d.DocComment = ex.doc
	}
	b.pendingDecorator = append(b.pendingDecorator, ex.decorators...)
}
