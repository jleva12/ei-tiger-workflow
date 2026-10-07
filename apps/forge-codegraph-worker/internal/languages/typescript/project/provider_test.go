package project

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	tsresolve "ei-aitiger-codegraph/worker/internal/resolve/typescript"
)

func write(t *testing.T, root, name, content string) {
	t.Helper()
	full := filepath.Join(root, filepath.FromSlash(name))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func checkout(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	write(t, root, "tsconfig.base.json", `{
  // shared options
  "compilerOptions": {
    "paths": { "@shared/*": ["packages/shared/src/*"] }, /* trailing comma below */
  },
}`)
	write(t, root, "web/tsconfig.json", `{ "extends": "../tsconfig.base.json", "compilerOptions": { "paths": { "@/*": ["./src/*"], "config": ["src/config.ts"] }, "outDir": "dist" } }`)
	write(t, root, "mobile/tsconfig.json", `{ "extends": "../tsconfig.base.json" }`)
	write(t, root, "web/admin/tsconfig.json", `{ "extends": "../tsconfig.json" }`)
	write(t, root, "web/widgets/tsconfig.json", `{ "extends": "../tsconfig.json", "compilerOptions": { "outDir": "build" } }`)
	write(t, root, "api/jsconfig.json", `{ "compilerOptions": { "baseUrl": "src" } }`)
	write(t, root, "broken/tsconfig.json", `{ not json`)
	write(t, root, "node_modules/pkg/tsconfig.json", `{ "compilerOptions": { "baseUrl": "." } }`)
	write(t, root, "package.json", `{ "name": "root", "workspaces": ["packages/*", "apps/**"] }`)
	write(t, root, "pnpm-workspace.yaml", "packages:\n  - 'tools/*'\n  - \"!**/test/**\"\n")
	write(t, root, "packages/shared/package.json", `{ "name": "@acme/shared", "main": "./dist/index.js", "source": "src/index.ts", "exports": { ".": { "import": "./src/index.ts", "types": "./src/index.d.ts" }, "./sub": "./src/sub.ts" } }`)
	write(t, root, "packages/unnamed/package.json", `{ "private": true }`)
	write(t, root, "apps/site/package.json", `{ "name": "site", "module": "src/main.tsx" }`)
	write(t, root, "apps/nested/deeper/package.json", `{ "name": "deeper" }`)
	write(t, root, "tools/cli/package.json", `{ "name": "cli", "exports": "./index.js" }`)
	return root
}

func TestScanReadsProjectsAndPackages(t *testing.T) {
	root, err := os.OpenRoot(checkout(t))
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	settings, found, err := Scan(context.Background(), root, bc.DefaultLimits())
	if err != nil || !found {
		t.Fatal(found, err)
	}
	// tsconfig.base.json is only a base, not a project of its own, and a
	// child's paths replace the base's, as in tsc; the broken file and the
	// one under node_modules are ignored.
	wantProjects := []tsresolve.Project{
		{Dir: "api", BaseURL: "api/src"},
		{Dir: "mobile", Paths: map[string][]string{"@shared/*": {"packages/shared/src/*"}}},
		{Dir: "web", Paths: map[string][]string{"@/*": {"web/src/*"}, "config": {"web/src/config.ts"}}, OutDir: "web/dist"},
		// web/admin inherits everything from web and is dropped; web/widgets
		// keeps only its own output directory.
		{Dir: "web/widgets", OutDir: "web/widgets/build"},
	}
	if !reflect.DeepEqual(settings.Projects, wantProjects) {
		t.Fatalf("projects: %+v", settings.Projects)
	}
	wantPackages := []tsresolve.Package{
		{Name: "@acme/shared", Dir: "packages/shared", Entries: []string{"packages/shared/src/index.ts", "packages/shared/src/index.d.ts", "packages/shared/dist/index.js"}},
		{Name: "cli", Dir: "tools/cli", Entries: []string{"tools/cli/index.js"}},
		{Name: "deeper", Dir: "apps/nested/deeper"},
		{Name: "site", Dir: "apps/site", Entries: []string{"apps/site/src/main.tsx"}},
	}
	if !reflect.DeepEqual(settings.Packages, wantPackages) {
		t.Fatalf("packages: %+v", settings.Packages)
	}
	if excludes := Excludes(settings, Config{}); !reflect.DeepEqual(excludes, []string{"**/node_modules/**", "web/dist/**", "web/widgets/build/**"}) {
		t.Fatalf("excludes: %v", excludes)
	}
	// The defaults and the operator's own patterns join the projects' output
	// directories; node_modules is always present and nothing repeats.
	withDefaults := Excludes(settings, Config{DefaultExcludes: true, Excludes: []string{"dev/browser/**", "**/dist/**"}})
	want := append([]string{"dev/browser/**", "web/dist/**", "web/widgets/build/**"}, DefaultExcludes...)
	sort.Strings(want)
	if !reflect.DeepEqual(withDefaults, want) {
		t.Fatalf("excludes with defaults: %v", withDefaults)
	}
	if _, err := New(Config{Version: "5", Excludes: []string{"/absolute/**"}}); err == nil {
		t.Fatal("absolute exclude accepted")
	}
}

func TestBuildDescribesTheCheckout(t *testing.T) {
	p, err := New(Config{Version: "5"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := New(Config{Version: "3"}); err == nil {
		t.Fatal("bad version accepted")
	}
	commit := "0123456789abcdef0123456789abcdef01234567"
	c, err := p.Build(context.Background(), bc.Request{Checkout: bc.Checkout{Path: checkout(t), RepositoryID: "repo", SnapshotID: commit}, Limits: bc.DefaultLimits()})
	if err != nil {
		t.Fatal(err)
	}
	if c.Producer.Name != "typescript-project" || len(c.Inventory.SourceSets) != 1 {
		t.Fatalf("context: %+v", c.Producer)
	}
	set := c.Inventory.SourceSets[0]
	if set.Language != "typescript" || set.LanguageVersion != "5" || set.LanguageOptions[tsresolve.SettingProjects] == "" || set.LanguageOptions[tsresolve.SettingPackages] == "" {
		t.Fatalf("source set: %+v", set)
	}
	if !reflect.DeepEqual(set.ExcludePatterns, []string{"**/node_modules/**", "web/dist/**", "web/widgets/build/**"}) {
		t.Fatalf("excludes: %v", set.ExcludePatterns)
	}
	if _, err := tsresolve.DecodeSettings(set.LanguageOptions); err != nil {
		t.Fatal(err)
	}
	// A checkout without any project configuration has no build.
	if _, err := p.Build(context.Background(), bc.Request{Checkout: bc.Checkout{Path: t.TempDir(), RepositoryID: "repo", SnapshotID: commit}, Limits: bc.DefaultLimits()}); !errors.Is(err, bc.ErrNoBuild) {
		t.Fatalf("empty checkout: %v", err)
	}
}

func TestJSONC(t *testing.T) {
	in := "{\n  // line\n  \"a\": \"x // not a comment\", /* block */ \"b\": [1, 2,],\n}\n"
	if got := string(stripJSONC([]byte(in))); got != "{\n  \n  \"a\": \"x // not a comment\",  \"b\": [1, 2]\n}\n" {
		t.Fatalf("stripped: %q", got)
	}
}
