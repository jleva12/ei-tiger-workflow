package parser_test

import (
	"errors"
	"testing"

	"ei-aitiger-codegraph/pkg/parser"
)

func registration(language string, extensions ...string) parser.Registration {
	return parser.Registration{
		Descriptor:   parser.Descriptor{Language: language, Extensions: extensions, Version: "1", GrammarVersion: "1", FeatureSet: "syntax/1"},
		New:          func() (parser.Session, error) { return nil, errors.New("unused") },
		Validate:     func(parser.Profile, parser.Limits) error { return nil },
		WorkerMemory: func(parser.Limits) (uint64, error) { return 1024, nil },
	}
}

func TestRegistryOwnershipRoutingAndIdentity(t *testing.T) {
	a, b := registration("alpha", ".ax", ".a"), registration("beta", ".b")
	r, err := parser.NewRegistry(a, b)
	if err != nil {
		t.Fatal(err)
	}
	a.Descriptor.Extensions[0] = ".mutated"
	entry, _ := r.Lookup("alpha")
	entry.Descriptor.Extensions[0] = ".mutated-again"
	for name, want := range map[string]string{"src/file.a": "alpha", "file.ax": "alpha", "file.b": "beta", "file.A": "", "file.unknown": "", "file": ""} {
		got, ok := r.LanguageForPath(name)
		if got != want || ok != (want != "") {
			t.Fatalf("%s: %q, %v", name, got, ok)
		}
	}
	same, _ := parser.NewRegistry(b, registration("alpha", ".a", ".ax"))
	if same.Digest() != r.Digest() {
		t.Fatal("registration order affects digest")
	}
	for _, change := range []func(*parser.Descriptor){
		func(d *parser.Descriptor) { d.Version = "2" },
		func(d *parser.Descriptor) { d.GrammarVersion = "2" },
		func(d *parser.Descriptor) { d.FeatureSet = "syntax/2" },
		func(d *parser.Descriptor) { d.Extensions = []string{".a"} },
	} {
		changed := registration("alpha", ".a", ".ax")
		change(&changed.Descriptor)
		other, err := parser.NewRegistry(b, changed)
		if err != nil || other.Digest() == r.Digest() {
			t.Fatal("adapter change omitted from fingerprint", err)
		}
	}
}

func TestRegistryRejectsAmbiguousOrIncompleteAdapters(t *testing.T) {
	for _, entries := range [][]parser.Registration{
		nil,
		{registration("a", ".a"), registration("a", ".b")},
		{registration("a", ".a"), registration("b", ".a")},
		{registration("a", ".a", ".a")},
		{registration("a", "a")},
		{registration("a", ".a.b")},
		{registration("a", ".a/b")},
		{registration("A", ".a")},
		{registration("", ".a")},
		{registration("a")},
		{{Descriptor: registration("a", ".a").Descriptor}},
	} {
		if _, err := parser.NewRegistry(entries...); !errors.Is(err, parser.ErrInvalidInput) {
			t.Fatalf("accepted invalid registry: %v", err)
		}
	}
}

func TestRegistryAdmissionUsesLargestSession(t *testing.T) {
	a, b := registration("a", ".a"), registration("b", ".b")
	b.WorkerMemory = func(parser.Limits) (uint64, error) { return 4096, nil }
	r, _ := parser.NewRegistry(a, b)
	if n, err := r.WorkerReservation(parser.DefaultLimits()); err != nil || n != 4096 {
		t.Fatal(n, err)
	}
	for _, estimate := range []func(parser.Limits) (uint64, error){
		func(parser.Limits) (uint64, error) { return 0, nil },
		func(parser.Limits) (uint64, error) { return 0, parser.ErrUnsupportedConfig },
	} {
		b.WorkerMemory = estimate
		r, _ := parser.NewRegistry(a, b)
		if _, err := r.WorkerReservation(parser.DefaultLimits()); err == nil {
			t.Fatal("invalid admission accepted")
		}
	}
}
