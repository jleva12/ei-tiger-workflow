package python

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"

	sitter "github.com/tree-sitter/go-tree-sitter"
	tspy "github.com/tree-sitter/tree-sitter-python/bindings/go"
)

// Language is the registered language id for .py and .pyi files.
const Language = "python"
const Version = "0.1.3"
const FeatureSet = "python-syntax/1"
const GrammarVersion = "python-0.25.0"

var Extensions = []string{".py", ".pyi"}
var shared struct {
	sync.Once
	language *sitter.Language
}

func initialize() { shared.language = sitter.NewLanguage(tspy.Language()) }

// Registration exposes the adapter without putting Python policy in the
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
		return fmt.Errorf("%w: Python adapter supports source budgets up to 16 MiB and depth up to 256", parser.ErrUnsupportedConfig)
	}
	return nil
}

// validateProfile accepts explicit Python 3.10–3.13 profiles. Environment
// settings are fingerprinted but do not change syntax extraction.
func validateProfile(profile parser.Profile, limits parser.Limits) error {
	if profile.Language != Language || profile.Options.EnablePreview {
		return fmt.Errorf("%w: Python without preview features is required", parser.ErrUnsupportedConfig)
	}
	// Any Python 3 level: the grammar parses the language through 3.13, and
	// syntax it does not know becomes partial coverage, not a failed run.
	minor, ok := strings.CutPrefix(profile.Version, "3.")
	if n, err := strconv.Atoi(minor); !ok || err != nil || n < 0 || n > 99 || strconv.Itoa(n) != minor {
		return fmt.Errorf("%w: Python language level must be 3.x", parser.ErrUnsupportedConfig)
	}
	for key, value := range profile.Options.Settings {
		switch key {
		case "python.environment":
			// Module-resolution settings the project discovery provider
			// writes for the resolver; the parser only requires valid JSON.
			if !json.Valid([]byte(value)) {
				return fmt.Errorf("%w: Python setting %q is not JSON", parser.ErrUnsupportedConfig, key)
			}
		default:
			return fmt.Errorf("%w: unknown Python setting %q", parser.ErrUnsupportedConfig, key)
		}
	}
	return validateLimits(limits)
}

var ErrClosed = errors.New("python parser: closed")

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
		return file, errors.New("Python Tree-sitter parse returned no tree")
	}
	defer tree.Close()
	b := newBuilder(ctx, input)
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
