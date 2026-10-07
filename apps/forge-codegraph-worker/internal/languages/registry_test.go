package languages

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/buildcontext/composite"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
	"ei-aitiger-codegraph/worker/internal/buildcontext/syntax"
)

type fakeResolver struct{ language string }

func (r fakeResolver) Resolve(context.Context, semantic.ResolveRequest, semantic.Workspace) (semantic.ResolutionResult, error) {
	return semantic.ResolutionResult{}, nil
}
func (r fakeResolver) Version() string      { return r.language + "-resolver-1" }
func (r fakeResolver) PolicyDigest() string { return "sha256:" + strings.Repeat("0", 64) }

type noBuild struct{}

func (noBuild) Build(context.Context, bc.Request) (bc.BuildContext, error) {
	return bc.BuildContext{}, bc.ErrNoBuild
}

func registration(language, ext string) parser.Registration {
	return parser.Registration{
		Descriptor:   parser.Descriptor{Language: language, Extensions: []string{ext}, Version: "1", GrammarVersion: "1", FeatureSet: "test"},
		New:          func() (parser.Session, error) { return nil, errors.New("not used") },
		Validate:     func(parser.Profile, parser.Limits) error { return nil },
		WorkerMemory: func(parser.Limits) (uint64, error) { return 1 << 20, nil },
	}
}

// adapter is a language whose build discovery is optional and whose
// configuration must be an empty object.
func adapter(language, ext string, discover func(string, manifest.Config) (bc.Provider, error)) Adapter {
	return Adapter{Parser: registration(language, ext), Configure: func(raw json.RawMessage, mode string) (Configured, error) {
		if len(raw) > 0 && string(raw) != "{}" {
			return Configured{}, errors.New("unexpected settings")
		}
		c := Configured{SyntaxProfile: syntax.Profile{Language: language, Version: "1", Roots: []string{"."}}, Resolver: fakeResolver{language}}
		if discover != nil {
			c.Discover = discover
		}
		return c, nil
	}}
}

func TestResolveComposesEveryLanguage(t *testing.T) {
	r, err := NewRegistry(
		adapter("alpha", ".alpha", func(string, manifest.Config) (bc.Provider, error) { return noBuild{}, nil }),
		adapter("beta", ".beta", nil),
	)
	if err != nil {
		t.Fatal(err)
	}
	parsers, provider, err := r.Resolve(BuildConfig{Mode: "maven-resolved"}, Settings{}, parser.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	composed, ok := provider.(*composite.Provider)
	if !ok || strings.Join(composed.Languages(), ",") != "alpha,beta" {
		t.Fatalf("build modes compose every enabled language: %T %v", provider, provider)
	}
	if _, ok := parsers.Lookup("beta"); !ok {
		t.Fatal("parser registry holds every enabled language")
	}
	resolvers, err := r.Resolvers("maven-resolved", Settings{})
	if err != nil || len(resolvers) != 2 || resolvers["alpha"].Version() != "alpha-resolver-1" {
		t.Fatalf("one resolver per language: %v %v", resolvers, err)
	}
	// Selecting one language composes that language alone.
	_, provider, err = r.Resolve(BuildConfig{Mode: "auto"}, Settings{"beta": json.RawMessage("{}")}, parser.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	if composed, ok = provider.(*composite.Provider); !ok || strings.Join(composed.Languages(), ",") != "beta" {
		t.Fatalf("selection: %v", provider)
	}
	// Syntax mode keeps the explicit per-language scan.
	if _, provider, err = r.Resolve(BuildConfig{Mode: "syntax"}, Settings{}, parser.DefaultLimits()); err != nil {
		t.Fatal(err)
	}
	if _, ok = provider.(*syntax.Provider); !ok {
		t.Fatalf("syntax mode: %T", provider)
	}
	// A build system failure is reported with its language, not masked.
	boom := errors.New("gradle exploded")
	r, err = NewRegistry(adapter("alpha", ".alpha", func(string, manifest.Config) (bc.Provider, error) { return nil, boom }))
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err = r.Resolve(BuildConfig{Mode: "gradle"}, Settings{}, parser.DefaultLimits()); !errors.Is(err, boom) || !strings.Contains(err.Error(), "language alpha") {
		t.Fatalf("discover failure: %v", err)
	}
	if _, _, err = r.Resolve(BuildConfig{Mode: "gradle"}, Settings{"gamma": json.RawMessage("{}")}, parser.DefaultLimits()); err == nil {
		t.Fatal("unregistered language accepted")
	}
}
