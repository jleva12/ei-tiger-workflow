package java

import (
	"context"
	"errors"
	"testing"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
)

func TestBuildMetadataAdmissionPreservesSyntaxAndFingerprint(t *testing.T) {
	p := newTestParser(t)
	input := inputFor("class A { int value; }")
	input.Source.LanguageVersion = "8"
	plain := parseTest(t, p, input)
	input.Options.Settings = map[string]string{
		"java.compiler.mode": "source-target", "java.compiler.target": "8", "java.compiler.proc": "only",
		"java.maven.execution": "gen-metadata", "java.maven.phase": "process-resources",
	}
	configured := parseTest(t, p, input)
	if configured.Coverage.Status != ir.ExtractionComplete || len(configured.Declarations) != len(plain.Declarations) {
		t.Fatal("build metadata changed syntax extraction")
	}
	if configured.Producer.ConfigDigest == plain.Producer.ConfigDigest {
		t.Fatal("build settings omitted from fingerprint")
	}
	input.Options.Settings["java.compiler.target"] = "11"
	if parseTest(t, p, input).Producer.ConfigDigest == configured.Producer.ConfigDigest {
		t.Fatal("compiler target omitted from fingerprint")
	}
}

func TestUnknownAndMalformedBuildSettingsRemainRejected(t *testing.T) {
	cases := []map[string]string{
		{"unknown": "true"}, {"java.compiler.mode": "unknown"},
		{"java.compiler.mode": "source-target", "java.compiler.target": "seven"},
		{"java.compiler.mode": "source-target", "java.compiler.target": "7"},
		{"java.compiler.mode": "source-target", "java.compiler.target": "08"},
		{"java.compiler.target": "8"}, {"java.compiler.proc": "maybe"},
		{"java.maven.execution": ""}, {"java.maven.phase": "compile\nother"},
	}
	p := newTestParser(t)
	for _, settings := range cases {
		input := inputFor("class A {}")
		input.Source.LanguageVersion = "8"
		input.Options.Settings = settings
		if _, err := p.Parse(context.Background(), input); !errors.Is(err, parser.ErrUnsupportedConfig) {
			t.Errorf("settings=%v err=%v", settings, err)
		}
	}
}
