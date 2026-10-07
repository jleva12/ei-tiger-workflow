package typescript

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/worker/internal/buildcontext/composite"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
	"ei-aitiger-codegraph/worker/internal/languages"
)

func contains(list []string, item string) bool {
	for _, x := range list {
		if x == item {
			return true
		}
	}
	return false
}

func TestConfigureAndCompose(t *testing.T) {
	for _, raw := range []string{"", "{}", `{"version":"4"}`} {
		c, err := configure(json.RawMessage(raw), "maven-resolved")
		if err != nil || c.Resolver == nil || c.SyntaxProfile.Language != "typescript" || c.Discover == nil || !contains(c.SyntaxProfile.Excludes, "**/node_modules/**") || !contains(c.SyntaxProfile.Excludes, "**/target/**") || !contains(c.SyntaxProfile.Excludes, "**/dist/**") || len(c.SyntaxProfile.Includes) != 0 {
			t.Fatalf("%q: %+v %v", raw, c, err)
		}
		if provider, err := c.Discover("maven-resolved", manifest.Config{}); err != nil || provider == nil {
			t.Fatalf("%q: discover %v %v", raw, provider, err)
		}
	}
	for _, raw := range []string{`[]`, `{"version":"3"}`, `{"unexpected":1}`, `{"version":"5"} {}`, `{"excludes":["/abs/**"]}`, `{"includes":["../x/**"]}`} {
		if _, err := configure(json.RawMessage(raw), "auto"); err == nil {
			t.Fatalf("invalid config accepted: %s", raw)
		}
	}
	// Operator selection: extra excludes, includes, and the defaults switched off.
	c, err := configure(json.RawMessage(`{"excludes":["dev/browser/**"],"includes":["src/**","dev/**"]}`), "auto")
	if err != nil || !contains(c.SyntaxProfile.Excludes, "dev/browser/**") || !contains(c.SyntaxProfile.Excludes, "**/dist/**") || len(c.SyntaxProfile.Includes) != 2 {
		t.Fatalf("selection: %+v %v", c.SyntaxProfile, err)
	}
	c, err = configure(json.RawMessage(`{"default_excludes":false}`), "auto")
	if err != nil || len(c.SyntaxProfile.Excludes) != 1 || c.SyntaxProfile.Excludes[0] != "**/node_modules/**" {
		t.Fatalf("defaults off: %+v %v", c.SyntaxProfile.Excludes, err)
	}
	// Without a build provider the registry composes the syntax fallback.
	r, err := languages.NewRegistry(Adapter())
	if err != nil {
		t.Fatal(err)
	}
	parsers, provider, err := r.Resolve(languages.BuildConfig{Mode: "maven-resolved"}, languages.Settings{}, parser.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := provider.(*composite.Provider); !ok {
		t.Fatalf("provider: %T", provider)
	}
	if language, ok := parsers.LanguageForPath("web/src/App.tsx"); !ok || language != "typescript" {
		t.Fatalf("tsx routing: %s %v", language, ok)
	}
	if language, ok := parsers.LanguageForPath("web/src/index.js"); !ok || language != "typescript" {
		t.Fatalf("js routing: %s %v", language, ok)
	}
	resolvers, err := r.Resolvers("maven-resolved", languages.Settings{})
	if err != nil || resolvers["typescript"] == nil {
		t.Fatalf("resolver: %v %v", resolvers, err)
	}
}

// The compiler tier and package installation are checked at startup: off
// when disabled, errors when asked for without what they need.
func TestCompilerAndInstallConfiguration(t *testing.T) {
	dir := t.TempDir()
	node := filepath.Join(dir, "node")
	npm := filepath.Join(dir, "npm")
	bridge := filepath.Join(dir, "dist", "bridge.cjs")
	for name, text := range map[string]string{node: "#!/bin/sh\n", npm: "#!/bin/sh\necho 11.0.0\n", bridge: "// bridge\n"} {
		if err := os.MkdirAll(filepath.Dir(name), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(name, []byte(text), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	compiler := `"compiler":{"node_path":"` + node + `","analyzer_path":"` + bridge + `"}`
	c, err := configure(json.RawMessage(`{`+compiler+`,"install":{"npm_path":"`+npm+`","cache_dir":"`+dir+`/cache","exclude":["@acme/private"]}}`), "auto")
	if err != nil || !strings.Contains(c.Resolver.Version(), "compiler") {
		t.Fatalf("compiler tier: %v %v", c.Resolver, err)
	}
	c, err = configure(json.RawMessage(`{"compiler":{"enabled":false}}`), "auto")
	if err != nil || strings.Contains(c.Resolver.Version(), "compiler") {
		t.Fatalf("disabled: %v %v", c.Resolver, err)
	}
	for _, raw := range []string{
		`{"compiler":{"enabled":true,"node_path":"/missing/node"}}`,
		`{"compiler":{"enabled":true,"node_path":"` + node + `","analyzer_path":"/missing/bridge.cjs"}}`,
		`{"compiler":{"enabled":false},"install":{"enabled":true}}`,
		`{` + compiler + `,"install":{"npm_path":"` + npm + `","registry":"ftp://mirror"}}`,
		`{` + compiler + `,"install":{"npm_path":"` + npm + `","exclude":["Not A Name"]}}`,
		`{` + compiler + `,"install":{"npm_path":"` + npm + `","max_mib":1}}`,
		`{"compiler":{"node_path":"` + node + `","analyzer_path":"` + bridge + `","max_heap_mib":1}}`,
	} {
		if _, err := configure(json.RawMessage(raw), "auto"); err == nil {
			t.Fatalf("invalid configuration accepted: %s", raw)
		}
	}
	if c, err := configure(json.RawMessage(`{"compiler":{"enabled":true,"node_path":"`+node+`","analyzer_path":"`+bridge+`"}}`), "syntax"); err == nil {
		t.Fatalf("compiler tier outside auto discovery accepted: %v", c.Resolver)
	}
}
