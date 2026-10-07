package builtin

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/worker/internal/languages"
)

// A repository with both a Python and a TypeScript project, which the
// bundled adapters describe with their own syntax scans, builds one context
// with a module per language.
func TestPythonAndTypeScriptProjectsShareACheckout(t *testing.T) {
	dir := t.TempDir()
	for name, content := range map[string]string{
		"pyproject.toml":      "[project]\nname = \"api\"\nrequires-python = \">=3.12\"\n",
		"api/app.py":          "def handler():\n    return 1\n",
		"package.json":        `{"name": "web", "private": true}`,
		"tsconfig.json":       `{"compilerOptions": {"strict": true}}`,
		"web/src/index.ts":    "export const answer = 42\n",
		"web/src/helpers.tsx": "export function Hello() { return null }\n",
	} {
		path := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	registry, err := Registry()
	if err != nil {
		t.Fatal(err)
	}
	settings := languages.Settings{"python": json.RawMessage("{}"), "typescript": json.RawMessage("{}")}
	_, provider, err := registry.Resolve(languages.BuildConfig{Mode: "auto"}, settings, parser.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	built, err := provider.Build(context.Background(), bc.Request{
		Checkout: bc.Checkout{Path: dir, RepositoryID: "repo", SnapshotID: "snapshot"},
		Limits:   bc.DefaultLimits(),
	})
	if err != nil {
		t.Fatalf("a Python and TypeScript checkout must build: %v", err)
	}
	languagesOf := map[bc.ModuleID]string{}
	for _, set := range built.Inventory.SourceSets {
		languagesOf[set.ModuleID] = set.Language
	}
	if languagesOf["syntax-python"] != "python" || languagesOf["syntax-typescript"] != "typescript" {
		t.Fatalf("modules by language: %v", languagesOf)
	}
}
