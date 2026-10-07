package java

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
}

// builder owns all per-file extraction state; no native node escapes Parse.
type builder struct {
	// issuesCut is set once the diagnostic budget cut the issues.
	issuesCut        bool
	statements       map[string]ir.StatementID
	statementIndex   map[ir.StatementID]int
	patterns         map[string]ir.PatternID
	patternIndex     map[ir.PatternID]int
	ctx              context.Context
	input            parser.Input
	release          int
	text             string
	translated       translatedSource
	lines            []uint
	file             ir.SourceFile
	serial           uint64
	records          uint64
	bytes            uint64
	sizes            map[string]uint64
	captures         map[uintptr]string
	expressions      map[string]ir.ExpressionID
	annotations      map[string]ir.AnnotationID
	declarationIndex map[ir.DeclarationID]int
}

func newBuilder(ctx context.Context, input parser.Input, release int, translated translatedSource) *builder {
	configuration, _ := json.Marshal(struct {
		Language, Release string
		Options           parser.Options
	}{input.Source.Language, input.Source.LanguageVersion, input.Options})
	digest := sha256.Sum256(configuration)
	b := &builder{
		statements: map[string]ir.StatementID{}, statementIndex: map[ir.StatementID]int{}, patterns: map[string]ir.PatternID{}, patternIndex: map[ir.PatternID]int{},
		ctx:              ctx,
		input:            input,
		release:          release,
		text:             string(input.Content),
		translated:       translated,
		lines:            []uint{0},
		sizes:            map[string]uint64{},
		captures:         map[uintptr]string{},
		expressions:      map[string]ir.ExpressionID{},
		annotations:      map[string]ir.AnnotationID{},
		declarationIndex: map[ir.DeclarationID]int{},
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
		Producer: ir.Producer{
			Name:           "ei-java-tree-sitter",
			Version:        Version,
			Grammar:        "tree-sitter-java",
			GrammarVersion: GrammarVersion,
			ConfigDigest:   hex.EncodeToString(digest[:]),
		},
		Coverage:           ir.ExtractionCoverage{Status: ir.ExtractionComplete, FeatureSet: FeatureSet},
		LanguageValidation: &ir.LanguageValidation{Status: ir.LanguageNotChecked, Method: "java-syntax-profile", Release: input.Source.LanguageVersion}}
	b.file.RootScopeID = b.scope(ir.ScopeFile, b.rawSpan(0, uint(len(b.text))), environment{})
	for _, invalid := range translated.errors {
		span := b.rawSpan(invalid.start, invalid.end)
		b.issueSpan(&span, "unicode_escape", "Malformed eligible Java Unicode escape", ir.CoverageParseError)
		b.file.LanguageValidation.Status = ir.LanguageInvalid
	}
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
	return fmt.Sprintf("%s:%d:%d:%d", kind, b.translated.rawOffset(n.StartByte()), b.translated.rawOffset(n.EndByte()), b.serial)
}
func (b *builder) position(offset uint) ir.Position {
	i := sort.Search(len(b.lines), func(i int) bool { return b.lines[i] > offset }) - 1
	return ir.Position{ByteOffset: uint64(offset), Line: uint32(i + 1), Column: uint32(offset - b.lines[i])}
}
func (b *builder) rawSpan(start, end uint) ir.Span {
	return ir.Span{Start: b.position(start), End: b.position(end)}
}
func (b *builder) span(n *sitter.Node) ir.Span { return b.spanBytes(n.StartByte(), n.EndByte()) }
func (b *builder) spanBytes(start, end uint) ir.Span {
	return b.rawSpan(b.translated.rawOffset(start), b.translated.rawOffset(end))
}
func (b *builder) rawSlice(start, end uint) string {
	return b.text[b.translated.rawOffset(start):b.translated.rawOffset(end)]
}
func (b *builder) spelling(n *sitter.Node) string {
	if n == nil {
		return ""
	}
	return b.rawSlice(n.StartByte(), n.EndByte())
}
func (b *builder) syntaxText(n *sitter.Node) string {
	if n == nil {
		return ""
	}
	return b.translated.text(n.StartByte(), n.EndByte())
}
func valid(n *sitter.Node) bool { return n != nil && !n.IsMissing() && n.EndByte() > n.StartByte() }
func named(n *sitter.Node) []*sitter.Node {
	if n == nil {
		return nil
	}
	var result []*sitter.Node
	for i := uint(0); i < n.NamedChildCount(); i++ {
		child := n.NamedChild(i)
		if child.Kind() != "line_comment" && child.Kind() != "block_comment" {
			result = append(result, child)
		}
	}
	return result
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

// Count records before adding them. Account each completed record (and updates
// to reserved declarations) before retaining it. Finish checks the full envelope.
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
	b.issueSpan(span, feature, message, reason)
}
func (b *builder) issueSpan(span *ir.Span, feature, message string, reason ir.CoverageReason) {
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
	d := ir.Diagnostic{Code: "java." + feature, Severity: severity, Message: message, Span: span, Feature: feature}
	c := ir.CoverageIssue{Feature: feature, Reason: reason, Message: message, Span: span}
	b.account(b.id("diagnostic", nil), d)
	b.account(b.id("coverage", nil), c)
	b.file.Diagnostics = append(b.file.Diagnostics, d)
	b.file.Coverage.Issues = append(b.file.Coverage.Issues, c)
	b.file.Coverage.Status = ir.ExtractionPartial
}

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
		b.releaseCheck(n)
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

func (b *builder) releaseCheck(n *sitter.Node) {
	minimum := 0
	switch n.Kind() {
	case "module_declaration":
		minimum = 9
	case "resource":
		if field(n, "name") == nil {
			minimum = 9
		}
	case "method_declaration":
		if parent := n.Parent(); parent != nil && parent.Kind() == "interface_body" {
			if modifiers := childKind(n, "modifiers"); modifiers != nil {
				for i := uint(0); i < modifiers.ChildCount(); i++ {
					if modifiers.Child(i).Kind() == "private" {
						minimum = 9
					}
				}
			}
		}
	case "record_declaration", "compact_constructor_declaration":
		minimum = 16
	case "sealed", "non-sealed", "permits":
		minimum = 17
	case "record_pattern", "type_pattern":
		minimum = 21
	case "guard":
		minimum = 21
	case "switch_label":
		if childKind(n, "null_literal") != nil {
			minimum = 21
		}
	case "switch_rule", "yield_statement":
		minimum = 14
	case "string_literal":
		if strings.HasPrefix(b.syntaxText(n), `"""`) {
			minimum = 15
		}
	case "instanceof_expression":
		if field(n, "name") != nil {
			minimum = 16
		}
	case "template_expression", "string_interpolation", "underscore_pattern":
		b.issue(n, "preview_syntax", "This preview/newer syntax is outside the supported release profile", ir.CoverageUnsupported)
	}
	if minimum > b.release {
		b.releaseViolation(n, fmt.Sprintf("%s requires Java %d or later; requested %d", n.Kind(), minimum, b.release))
	}
}

func (b *builder) capture(root *sitter.Node) {
	for _, query := range shared.queries {
		func() {
			cursor := sitter.NewQueryCursor()
			defer cursor.Close()
			names := query.CaptureNames()
			matches := cursor.Matches(query, root, nil)
			for match := matches.Next(); match != nil; match = matches.Next() {
				b.check()
				for _, capture := range match.Captures {
					b.captures[capture.Node.Id()] = names[capture.Index]
				}
			}
			if cursor.DidExceedMatchLimit() {
				b.limit("Tree-sitter query match limit")
			}
		}()
	}
}

func (b *builder) finish() ir.SourceFile {
	b.check()
	if b.file.Coverage.Status == ir.ExtractionPartial && len(b.file.Declarations) == 0 && b.file.Package == nil && len(b.file.Imports) == 0 && len(b.file.Expressions) == 0 && b.file.Module == nil {
		b.file.Coverage.Status = ir.ExtractionFailed
	}
	if err := b.file.Validate(); err != nil {
		panic(stop{fmt.Errorf("invalid Java parser output: %w", err)})
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

func (b *builder) releaseViolation(n *sitter.Node, message string) {
	b.file.LanguageValidation.Status = ir.LanguageInvalid
	b.issue(n, "release", message, ir.CoverageParseError)
}
