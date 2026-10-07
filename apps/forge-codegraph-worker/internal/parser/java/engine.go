// Package java implements syntax extraction with the original application's
// Tree-sitter Go bindings, Java grammar, cached SCM queries, and owned sessions.
package java

import (
	"context"
	"embed"
	"errors"
	"fmt"
	"runtime"
	"strconv"
	"sync"
	"sync/atomic"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"

	sitter "github.com/tree-sitter/go-tree-sitter"
	tsjava "github.com/tree-sitter/tree-sitter-java/bindings/go"
)

const Version = "0.2.1"
const FeatureSet = "java-syntax/2"
const GrammarVersion = "0.23.5-ei.1"

//go:embed queries/*.scm
var queryFS embed.FS

var shared struct {
	sync.Once
	language *sitter.Language
	queries  []*sitter.Query
	err      error
}

func initialize() {
	shared.language = sitter.NewLanguage(tsjava.Language())
	for _, name := range []string{"definitions", "calls", "imports", "references"} {
		data, err := queryFS.ReadFile("queries/" + name + ".scm")
		if err != nil {
			shared.err = err
			break
		}
		query, queryErr := structuralQuery(shared.language, string(data))
		if queryErr != nil {
			shared.err = fmt.Errorf("compile Java %s query: %w", name, queryErr)
			break
		}
		shared.queries = append(shared.queries, query)
	}
	if shared.err != nil {
		for _, query := range shared.queries {
			query.Close()
		}
		shared.queries = nil
	}
}

// QueryMatch's text predicates index a UTF-8 byte buffer, whereas this adapter
// parses translated UTF-16. Keep SCM selection structural; text comparisons use
// builder.syntaxText after source mapping. Fail startup if queries violate this.
func structuralQuery(language *sitter.Language, source string) (*sitter.Query, error) {
	query, err := sitter.NewQuery(language, source)
	if err != nil {
		return nil, err
	}
	for i := uint(0); i < query.PatternCount(); i++ {
		if len(query.TextPredicates[i]) != 0 || len(query.PropertyPredicates(i)) != 0 || len(query.GeneralPredicates(i)) != 0 {
			query.Close()
			return nil, errors.New("Java extraction queries must be structural; evaluate text through translated source mapping")
		}
	}
	return query, nil
}

var ErrClosed = errors.New("java parser: closed")

// Parser owns one reusable native session, like an original pipeline worker.
// Calls on an instance serialize with cancellable admission. Use one instance
// per worker for parallelism; immutable grammar/queries are cached process-wide.
// Construct with New and release the session with Close. Do not copy a Parser.
// The zero value is not usable.
type Parser struct {
	gate   chan struct{}
	native *sitter.Parser
	closed bool // guarded by gate
}

var _ parser.Parser = (*Parser)(nil)

func New() (*Parser, error) {
	shared.Do(initialize)
	if shared.err != nil {
		return nil, shared.err
	}
	p := sitter.NewParser()
	if err := p.SetLanguage(shared.language); err != nil {
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
	release, _ := strconv.Atoi(input.Source.LanguageVersion)
	defer func() {
		if recovered := recover(); recovered != nil {
			if stopped, ok := recovered.(stop); ok {
				file, err = ir.SourceFile{}, stopped.err
			} else {
				panic(recovered)
			}
		}
	}()
	translated, err := translateJava(ctx, input.Content)
	if err != nil {
		return file, err
	}
	tree := parseNative(ctx, p.native, translated.units)
	if err := ctx.Err(); err != nil {
		if tree != nil {
			tree.Close()
		}
		return file, err
	}
	if tree == nil {
		return file, errors.New("Java Tree-sitter parse returned no tree")
	}
	defer tree.Close()
	b := newBuilder(ctx, input, release, translated)
	b.scan(tree.RootNode())
	b.capture(tree.RootNode())
	b.children(tree.RootNode(), environment{scope: b.file.RootScopeID})
	return b.finish(), nil
}

// v0.25.0 retains a Go handle for non-nil ParseWithOptions options. Use its
// supported cancellation flag instead. Pin the scalar for C, join the watcher
// before unpin/close, and reset the session after an interrupted parse.
func parseNative(ctx context.Context, p *sitter.Parser, content []uint16) *sitter.Tree {
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
	return p.ParseUTF16LE(content, nil)
}
