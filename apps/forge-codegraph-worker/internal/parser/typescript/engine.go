// Package typescript implements syntax extraction for TypeScript and
// JavaScript with the upstream Tree-sitter grammars: typescript for .ts,
// .mts and .cts, tsx for .tsx and .jsx, and javascript for .js, .mjs and
// .cjs, all under the single language id "typescript". It extracts
// declarations (classes, interfaces, enums, type aliases, namespaces,
// functions, methods, fields, module-level and local variables, parameters,
// type parameters), imports and re-exports with their module specifiers,
// calls and constructions, name and member references, type uses,
// decorators as annotations and function literals as lambda sites. Other
// expression and type forms are retained as opaque records without a
// coverage loss: they are operands, never dependency facts, and the feature
// set declares that.
package typescript

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	pathpkg "path"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"

	sitter "github.com/tree-sitter/go-tree-sitter"
	tsjs "github.com/tree-sitter/tree-sitter-javascript/bindings/go"
	tsts "github.com/tree-sitter/tree-sitter-typescript/bindings/go"
)

// Language is the registered language id; JavaScript files belong to it.
const Language = "typescript"
const Version = "0.3.4"
const FeatureSet = "typescript-syntax/1"
const GrammarVersion = "typescript-0.23.2+javascript-0.23.1"

// Extensions are the exact final extensions this adapter claims.
var Extensions = []string{".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}

var shared struct {
	sync.Once
	typescript, tsx, javascript *sitter.Language
}

func initialize() {
	shared.typescript = sitter.NewLanguage(tsts.LanguageTypescript())
	shared.tsx = sitter.NewLanguage(tsts.LanguageTSX())
	shared.javascript = sitter.NewLanguage(tsjs.Language())
}

// grammarFor selects the grammar by extension: JSX-bearing files use the
// tsx grammar, plain JavaScript the javascript grammar, everything else the
// typescript grammar.
func grammarFor(path string) (*sitter.Language, string) {
	switch strings.ToLower(pathpkg.Ext(path)) {
	case ".tsx", ".jsx":
		return shared.tsx, "tsx"
	case ".js", ".mjs", ".cjs":
		return shared.javascript, "javascript"
	default:
		return shared.typescript, "typescript"
	}
}

// Registration exposes the adapter without putting TypeScript policy in the
// worker.
func Registration() parser.Registration {
	return parser.Registration{
		Descriptor: parser.Descriptor{Language: Language, Extensions: append([]string(nil), Extensions...), Version: Version, GrammarVersion: GrammarVersion, FeatureSet: FeatureSet},
		New: func() (parser.Session, error) {
			engine, err := New()
			if err != nil {
				return nil, err
			}
			return engine, nil
		},
		Validate: validateProfile,
		WorkerMemory: func(limits parser.Limits) (uint64, error) {
			if err := validateLimits(limits); err != nil {
				return 0, err
			}
			if limits.MaxOutputBytes > (1<<63)/3 {
				return 0, fmt.Errorf("%w: output memory estimate overflow", parser.ErrUnsupportedConfig)
			}
			// Conservative scheduling estimate, not a hard native allocation cap.
			return 12*limits.MaxSourceBytes + 3*limits.MaxOutputBytes + (8 << 20), nil
		},
	}
}

func validateLimits(limits parser.Limits) error {
	if err := limits.Validate(); err != nil {
		return err
	}
	if limits.MaxSourceBytes > 16<<20 || limits.MaxSyntaxDepth > 16384 {
		return fmt.Errorf("%w: TypeScript adapter supports source budgets up to 16 MiB and depth up to 256", parser.ErrUnsupportedConfig)
	}
	return nil
}

// validateProfile accepts the TypeScript language level (4 or 5) and no
// preview features or adapter settings; JavaScript files parse under the
// same profile.
func validateProfile(profile parser.Profile, limits parser.Limits) error {
	if profile.Language != Language || profile.Options.EnablePreview {
		return fmt.Errorf("%w: TypeScript without preview features is required", parser.ErrUnsupportedConfig)
	}
	switch profile.Version {
	case "4", "5":
	default:
		return fmt.Errorf("%w: TypeScript language level must be 4 or 5", parser.ErrUnsupportedConfig)
	}
	for key, value := range profile.Options.Settings {
		switch key {
		case "projects", "packages", "installs":
			// Module-resolution settings and installed packages the project
			// discovery provider writes for the resolver; the parser only
			// requires valid JSON.
			if !json.Valid([]byte(value)) {
				return fmt.Errorf("%w: TypeScript setting %q is not JSON", parser.ErrUnsupportedConfig, key)
			}
		case "compiler":
			// The compiler tier's fingerprint: a SHA-256 in hex.
			if len(value) != 64 || strings.Trim(value, "0123456789abcdef") != "" {
				return fmt.Errorf("%w: TypeScript setting %q is not a digest", parser.ErrUnsupportedConfig, key)
			}
		default:
			return fmt.Errorf("%w: unknown TypeScript setting %q", parser.ErrUnsupportedConfig, key)
		}
	}
	return validateLimits(limits)
}

var ErrClosed = errors.New("typescript parser: closed")

// Parser owns one reusable native session. Calls on an instance serialize
// with cancellable admission; use one instance per worker for parallelism.
type Parser struct {
	gate   chan struct{}
	native *sitter.Parser
	closed bool // guarded by gate
}

var _ parser.Parser = (*Parser)(nil)

func New() (*Parser, error) {
	shared.Do(initialize)
	p := sitter.NewParser()
	if err := p.SetLanguage(shared.typescript); err != nil {
		p.Close()
		return nil, err
	}
	adapter := &Parser{native: p, gate: make(chan struct{}, 1)}
	adapter.gate <- struct{}{}
	return adapter, nil
}

func (p *Parser) Close(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-p.gate:
	}
	defer func() { p.gate <- struct{}{} }()
	if err := ctx.Err(); err != nil {
		return err
	}
	if !p.closed {
		p.native.Close()
		p.closed = true
	}
	return nil
}

func (p *Parser) Parse(ctx context.Context, input parser.Input) (file ir.SourceFile, err error) {
	if err := ctx.Err(); err != nil {
		return file, err
	}
	select {
	case <-ctx.Done():
		return file, ctx.Err()
	case <-p.gate:
	}
	defer func() { p.gate <- struct{}{} }()
	if p.closed {
		return file, ErrClosed
	}
	if err := input.Validate(); err != nil {
		return file, err
	}
	// Bytes that are not UTF-8 read as '?', one for one, so spans still
	// address the original file.
	input.Content = input.Text()
	if err := ctx.Err(); err != nil {
		return file, err
	}
	if err := validateProfile(parser.Profile{Language: input.Source.Language, Version: input.Source.LanguageVersion, Options: input.Options}, input.Limits); err != nil {
		return file, err
	}
	if reason := generatedFile(input.Source.Path, input.Content); reason != "" {
		return file, errGenerated(reason)
	}
	language, dialect := grammarFor(input.Source.Path)
	if err := p.native.SetLanguage(language); err != nil {
		return file, err
	}
	defer func() {
		if recovered := recover(); recovered != nil {
			if stopped, ok := recovered.(stop); ok {
				file, err = ir.SourceFile{}, stopped.err
			} else {
				panic(recovered)
			}
		}
	}()
	tree := parseNative(ctx, p.native, input.Content)
	if err := ctx.Err(); err != nil {
		if tree != nil {
			tree.Close()
		}
		return file, err
	}
	if tree == nil {
		return file, errors.New("TypeScript Tree-sitter parse returned no tree")
	}
	defer tree.Close()
	b := newBuilder(ctx, input, dialect)
	b.scan(tree.RootNode())
	b.program(tree.RootNode())
	return b.finish(), nil
}

// parseNative parses the raw UTF-8 bytes with cancellation through the
// parser's cancellation flag, resetting the session afterwards.
func parseNative(ctx context.Context, p *sitter.Parser, content []byte) *sitter.Tree {
	var flag uintptr
	var pin runtime.Pinner
	pin.Pin(&flag)
	p.SetCancellationFlag(&flag)
	finished, joined := make(chan struct{}), make(chan struct{})
	go func() {
		defer close(joined)
		select {
		case <-ctx.Done():
			atomic.StoreUintptr(&flag, 1)
		case <-finished:
		}
	}()
	defer func() {
		close(finished)
		<-joined
		p.SetCancellationFlag(nil)
		pin.Unpin()
		p.Reset()
	}()
	return p.Parse(content, nil)
}
