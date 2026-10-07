package project

import (
	"context"
	"os"
	"reflect"
	"sort"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/parser"
	tsparser "ei-aitiger-codegraph/worker/internal/parser/typescript"
	tsresolve "ei-aitiger-codegraph/worker/internal/resolve/typescript"
)

// Each npm project is installed on its own: a workspace root with its
// workspace packages and lockfile, and projects beside it that no
// workspace includes. Hidden tool folders, fixtures, templates, build
// output and projects without dependencies are not installed.
func TestNPMProjects(t *testing.T) {
	root := t.TempDir()
	dep := `{"dependencies":{"react":"^19.0.0"}}`
	write(t, root, "package.json", `{"name":"mono","workspaces":["packages/*"],"devDependencies":{"typescript":"^5.0.0"}}`)
	write(t, root, "package-lock.json", `{"lockfileVersion":3,"packages":{}}`)
	write(t, root, "packages/ui/package.json", `{"name":"@mono/ui","dependencies":{"clsx":"^2.0.0"}}`)
	write(t, root, "packages/core/package.json", `{"name":"@mono/core"}`)
	write(t, root, "services/api/package.json", dep)
	write(t, root, "services/api/package-lock.json", `{"lockfileVersion":3,"packages":{}}`)
	write(t, root, "services/empty/package.json", `{"name":"empty"}`)
	write(t, root, ".agents/skills/tool/package.json", dep)
	write(t, root, "test/fixtures/app/package.json", dep)
	write(t, root, "generator/templates/app/package.json", dep)
	write(t, root, "packages/ui/dist/package.json", dep)
	opened, err := os.OpenRoot(root)
	if err != nil {
		t.Fatal(err)
	}
	defer opened.Close()
	_, _, sc, err := scan(context.Background(), opened, bc.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	projects := sc.npmProjects(Excludes(tsresolve.Settings{}, Config{DefaultExcludes: true}))
	got := map[string][]string{}
	for _, p := range projects {
		for name := range p.Files {
			got[p.Dir] = append(got[p.Dir], name)
		}
		sort.Strings(got[p.Dir])
	}
	want := map[string][]string{
		".":            {"package-lock.json", "package.json", "packages/core/package.json", "packages/ui/package.json"},
		"services/api": {"package-lock.json", "package.json"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("projects:\n got %v\nwant %v", got, want)
	}
}

// The compiler fingerprint changes with any project file the compiler
// reads, a tsconfig variant included.
func TestCompilerFingerprint(t *testing.T) {
	root := t.TempDir()
	write(t, root, "tsconfig.json", `{"files":[],"references":[{"path":"./tsconfig.app.json"}]}`)
	write(t, root, "tsconfig.app.json", `{"compilerOptions":{"jsx":"react-jsx"}}`)
	write(t, root, "src/main.ts", "export const x = 1;\n")
	p, err := New(Config{Version: "5", DefaultExcludes: true, Compiler: "bridge-1"})
	if err != nil {
		t.Fatal(err)
	}
	fingerprint := func() string {
		t.Helper()
		c, err := p.Build(context.Background(), bc.Request{Checkout: bc.Checkout{Path: root, RepositoryID: "repo", SnapshotID: "0123456789abcdef0123456789abcdef01234567"}, Limits: bc.DefaultLimits()})
		if err != nil {
			t.Fatal(err)
		}
		s, err := tsresolve.DecodeSettings(c.Inventory.SourceSets[0].LanguageOptions)
		if err != nil {
			t.Fatal(err)
		}
		return s.Compiler
	}
	first := fingerprint()
	write(t, root, "tsconfig.app.json", `{"compilerOptions":{"jsx":"preserve"}}`)
	if second := fingerprint(); first == "" || second == first {
		t.Fatalf("fingerprints %q %q", first, second)
	}
}

// Everything discovery writes on a source set is accepted by the parser,
// which validates the profile's options too.
func TestDiscoveredSettingsParse(t *testing.T) {
	options, err := tsresolve.EncodeSettings(tsresolve.Settings{
		Projects: []tsresolve.Project{{Dir: ".", BaseURL: "."}},
		Packages: []tsresolve.Package{{Name: "@mono/ui", Dir: "packages/ui"}},
		Installs: []tsresolve.Install{{Dir: ".", Path: "/cache/npm-x", SHA256: strings.Repeat("a", 64)}},
		Compiler: strings.Repeat("b", 64),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := tsparser.Registration().Validate(parser.Profile{Language: Language, Version: "5", Options: parser.Options{Settings: options}}, parser.DefaultLimits()); err != nil {
		t.Fatalf("the parser rejects discovered settings: %v", err)
	}
	if err := tsresolve.ValidateSettings(options); err != nil {
		t.Fatalf("the resolver rejects discovered settings: %v", err)
	}
}

// A pnpm workspace is one project: its packages, listed only in
// pnpm-workspace.yaml, and that file, whose catalogs version them.
func TestPNPMWorkspaceProject(t *testing.T) {
	root := t.TempDir()
	write(t, root, "package.json", `{"name":"mono","private":true}`)
	write(t, root, "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'packages/*'\ncatalog:\n  react: ^18.3.1\n")
	write(t, root, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
	write(t, root, "apps/web/package.json", `{"name":"web","dependencies":{"react":"catalog:"}}`)
	write(t, root, "packages/ui/package.json", `{"name":"@mono/ui","dependencies":{"clsx":"^2.0.0"}}`)
	opened, err := os.OpenRoot(root)
	if err != nil {
		t.Fatal(err)
	}
	defer opened.Close()
	_, _, sc, err := scan(context.Background(), opened, bc.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	projects := sc.npmProjects(nil)
	if len(projects) != 1 || projects[0].Dir != "." {
		t.Fatalf("projects: %+v", projects)
	}
	var names []string
	for name := range projects[0].Files {
		names = append(names, name)
	}
	sort.Strings(names)
	if got := strings.Join(names, ","); got != "apps/web/package.json,package.json,packages/ui/package.json,pnpm-workspace.yaml" {
		t.Fatalf("files: %s", got)
	}
}
