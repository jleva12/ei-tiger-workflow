package packages

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeNPM installs each dependency of the project's package.json files as
// a package with a declaration, a script and a runtime file, fails for
// names starting with missing (not in the registry) and offline (the
// registry cannot be reached), and logs its arguments.
const fakeNPM = `#!/bin/sh
log="$(dirname "$0")/calls.log"
echo "$@" >> "$log"
names=$(find . -name package.json -not -path '*/node_modules/*' -exec grep -oE '"(dependencies|devDependencies)":\{[^}]*\}' {} \; | sed -E 's/^"[a-zA-Z]+":\{//; s/\}$//' | tr ',' '\n' | sed -n -E 's/^"([^"]+)":.*/\1/p')
for n in $names; do
  case "$n" in
    offline*) echo "npm error network request to https://registry.npmjs.org/$n failed, reason: getaddrinfo ENOTFOUND registry.npmjs.org" >&2; exit 1 ;;
    missing*) echo "npm error code E404" >&2; echo "npm error 404  '$n@^1.0.0' is not in this registry." >&2; exit 1 ;;
  esac
done
for n in $names; do
  mkdir -p "node_modules/$n" node_modules/.bin
  echo "{\"name\":\"$n\",\"types\":\"index.d.ts\"}" > "node_modules/$n/package.json"
  echo "export declare function run(): void;" > "node_modules/$n/index.d.ts"
  echo "exports.run = () => {};" > "node_modules/$n/index.js"
  ln -sf "../$n/index.js" "node_modules/.bin/$n"
done
`

func fake(t *testing.T) (Config, func() []string) {
	t.Helper()
	dir := t.TempDir()
	npm := filepath.Join(dir, "npm")
	if err := os.WriteFile(npm, []byte(fakeNPM), 0o700); err != nil {
		t.Fatal(err)
	}
	calls := func() []string {
		data, _ := os.ReadFile(filepath.Join(dir, "calls.log"))
		text := strings.TrimSpace(string(data))
		if text == "" {
			return nil
		}
		return strings.Split(text, "\n")
	}
	return Config{NPM: npm, NPMVersion: "11.0.0", CacheDir: filepath.Join(dir, "cache"), MaxBytes: 1 << 20, Timeout: time.Minute}, calls
}

func manifest(t *testing.T, deps map[string]string) []byte {
	t.Helper()
	data, err := json.Marshal(map[string]any{"name": "app", "packageManager": "pnpm@9.0.0", "scripts": map[string]string{"postinstall": "curl evil"}, "dependencies": deps})
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestInstallsWithScriptsOffAndKeepsDeclarations(t *testing.T) {
	c, calls := fake(t)
	lock := `{"lockfileVersion":3,"packages":{"":{"dependencies":{"alpha":"^1.0.0","@acme/private":"^2.0.0"}},` +
		`"node_modules/alpha":{"version":"1.0.0","resolved":"https://registry.npmjs.org/alpha/-/alpha-1.0.0.tgz"},` +
		`"node_modules/@acme/private":{"version":"2.0.0","resolved":"https://npm.acme.example/@acme/private/-/private-2.0.0.tgz"},` +
		`"node_modules/shared":{"resolved":"packages/shared","link":true}}}`
	p := Project{Dir: "web", Files: map[string][]byte{
		"package.json":                 manifest(t, map[string]string{"alpha": "^1.0.0", "@acme/private": "^2.0.0", "local": "file:../local", "tool": "github:user/tool", "shared": "workspace:*"}),
		"packages/shared/package.json": []byte(`{"name":"shared"}`),
		"package-lock.json":            []byte(lock),
	}}
	got, err := Install(context.Background(), c, p)
	if err != nil {
		t.Fatal(err)
	}
	failed := map[string]string{}
	for _, f := range got.Failed {
		failed[f.Package] = f.Reason
	}
	if _, reported := failed["local"]; reported || !strings.Contains(failed["@acme/private"], "npm.acme.example") || !strings.Contains(failed["tool"], "github:") || !got.Unlocked {
		t.Fatalf("result: %+v", got)
	}
	for _, name := range []string{"node_modules/alpha/package.json", "node_modules/alpha/index.d.ts", "node_modules/shared/index.d.ts", marker} {
		if _, err := os.Stat(filepath.Join(got.Dir, name)); err != nil {
			t.Fatalf("%s was not kept: %v", name, err)
		}
	}
	for _, name := range []string{"node_modules/alpha/index.js", "node_modules/.bin"} {
		if _, err := os.Lstat(filepath.Join(got.Dir, name)); err == nil {
			t.Fatalf("%s was kept", name)
		}
	}
	written, _ := os.ReadFile(filepath.Join(got.Dir, "package.json"))
	if strings.Contains(string(written), "postinstall") || strings.Contains(string(written), "packageManager") || strings.Contains(string(written), "@acme/private") {
		t.Fatalf("manifest given to npm: %s", written)
	}
	log := calls()
	if len(log) != 1 || !strings.HasPrefix(log[0], "install ") {
		t.Fatalf("calls: %q", log)
	}
	for _, arg := range []string{"--ignore-scripts", "--registry=" + DefaultRegistry, "--cache=" + filepath.Join(c.CacheDir, "npm-cache"), "--legacy-peer-deps"} {
		if !strings.Contains(log[0], arg) {
			t.Fatalf("%s missing from %s", arg, log[0])
		}
	}
	again, err := Install(context.Background(), c, p)
	if err != nil || again.Dir != got.Dir || len(calls()) != 1 {
		t.Fatalf("second install: %+v %v, %d calls", again, err, len(calls()))
	}
}

func TestPackagesTheRegistryDoesNotHaveAreLeftOut(t *testing.T) {
	c, calls := fake(t)
	got, err := Install(context.Background(), c, Project{Dir: ".", Files: map[string][]byte{"package.json": manifest(t, map[string]string{"alpha": "^1.0.0", "missing-sdk": "^1.0.0"})}})
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Failed) != 1 || got.Failed[0].Package != "missing-sdk" || !strings.Contains(got.Failed[0].Reason, "is not in this registry") {
		t.Fatalf("result: %+v", got)
	}
	if _, err := os.Stat(filepath.Join(got.Dir, "node_modules/alpha/index.d.ts")); err != nil {
		t.Fatal("the rest was not installed")
	}
	if n := len(calls()); n != 2 {
		t.Fatalf("%d calls: %q", n, calls())
	}
}

func TestAnUnreachableRegistryIsTriedHoursLater(t *testing.T) {
	c, calls := fake(t)
	p := Project{Dir: ".", Files: map[string][]byte{"package.json": manifest(t, map[string]string{"offline-pkg": "^1.0.0"})}}
	if _, err := Install(context.Background(), c, p); err == nil || !strings.Contains(err.Error(), "could not be reached") {
		t.Fatalf("offline: %v", err)
	}
	if _, err := Install(context.Background(), c, p); err == nil || !strings.Contains(err.Error(), "tried again") || len(calls()) != 1 {
		t.Fatalf("tried again at once: %v, %d calls", err, len(calls()))
	}
}

func TestFailingPackage(t *testing.T) {
	for output, want := range map[string]string{
		"npm error 404  '@acme/private@^1.0.0' is not in this registry.":                              "@acme/private",
		"npm error notarget No matching version found for left-pad@^9.9.9.":                           "left-pad",
		"npm error 404 Not Found - GET https://registry.npmjs.org/@acme%2fprivate - Not found":        "@acme/private",
		"npm error 403 403 Forbidden - GET https://npm.example.com/@mobiscroll%2freact/-/react-6.tgz": "@mobiscroll/react",
		"npm error something else": "",
	} {
		if got := failingPackage(output); got != want {
			t.Fatalf("%q: %q, want %q", output, got, want)
		}
	}
	if !registrySpec("^1.2.0") || !registrySpec("npm:string-width@^4") || !registrySpec("latest") || registrySpec("file:../x") || registrySpec("user/repo") || registrySpec("git+https://x") || registrySpec("link:../x") {
		t.Fatal("registry specs")
	}
}

// read decodes a manifest npm was given.
func readManifest(t *testing.T, dir, name string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, filepath.FromSlash(name)))
	if err != nil {
		t.Fatal(err)
	}
	var manifest map[string]any
	if err := json.Unmarshal(data, &manifest); err != nil {
		t.Fatal(err)
	}
	return manifest
}

func deps(manifest map[string]any) map[string]any {
	d, _ := manifest["dependencies"].(map[string]any)
	return d
}

// A pnpm workspace installs with npm: its packages, which only
// pnpm-workspace.yaml lists, become npm workspaces; catalog: ranges take
// the catalog's version; a workspace: link to a package the checkout holds
// is linked and one to a package it does not is left out, never fetched
// from the registry under the same name.
func TestPNPMWorkspaceInstallsWithNPM(t *testing.T) {
	c, _ := fake(t)
	p := Project{Dir: ".", Files: map[string][]byte{
		"package.json":                 []byte(`{"name":"mono","packageManager":"pnpm@9.12.0","devDependencies":{"typescript":"catalog:"}}`),
		"pnpm-workspace.yaml":          []byte("packages:\n  - 'apps/*'\n  - 'packages/*'\ncatalog:\n  react: ^18.3.1\n  typescript: 5.6.3\ncatalogs:\n  legacy:\n    'react-dom': ^17.0.2\n"),
		"apps/web/package.json":        []byte(`{"name":"web","dependencies":{"react":"catalog:","react-dom":"catalog:legacy","@mono/ui":"workspace:*","@mono/gone":"workspace:^","lodash":"catalog:missing"}}`),
		"packages/ui/package.json":     []byte(`{"name":"@mono/ui","dependencies":{"clsx":"^2.0.0"}}`),
		"packages/noname/package.json": []byte(`{"dependencies":{"zod":"^3.0.0"}}`),
	}}
	got, err := Install(context.Background(), c, p)
	if err != nil {
		t.Fatal(err)
	}
	root := readManifest(t, got.Dir, "package.json")
	if workspaces, _ := json.Marshal(root["workspaces"]); string(workspaces) != `["apps/web","packages/noname","packages/ui"]` {
		t.Fatalf("workspaces: %s", workspaces)
	}
	if root["packageManager"] != nil || root["devDependencies"].(map[string]any)["typescript"] != "5.6.3" {
		t.Fatalf("root: %v", root)
	}
	web := deps(readManifest(t, got.Dir, "apps/web/package.json"))
	if web["react"] != "^18.3.1" || web["react-dom"] != "^17.0.2" || web["@mono/ui"] != "*" || web["lodash"] != "*" {
		t.Fatalf("web: %v", web)
	}
	if _, fetched := web["@mono/gone"]; fetched {
		t.Fatal("a link to a package the checkout does not hold was left for the registry")
	}
	if name, _ := readManifest(t, got.Dir, "packages/noname/package.json")["name"].(string); !strings.HasPrefix(name, "codegraph-workspace-") {
		t.Fatalf("a workspace package without a name was not named: %q", name)
	}
	for _, installed := range []string{"clsx", "zod", "react"} {
		if _, err := os.Stat(filepath.Join(got.Dir, "node_modules", installed, "index.d.ts")); err != nil {
			t.Fatalf("%s of a workspace package was not installed", installed)
		}
	}
}

// Bun keeps its catalogs in the root manifest's workspaces object, a form
// npm does not accept: npm is given the packages as a list.
func TestBunWorkspaceCatalogs(t *testing.T) {
	c, _ := fake(t)
	got, err := Install(context.Background(), c, Project{Dir: ".", Files: map[string][]byte{
		"package.json":              []byte(`{"name":"mono","workspaces":{"packages":["packages/*"],"catalog":{"zod":"^3.23.0"}}}`),
		"packages/api/package.json": []byte(`{"name":"api","dependencies":{"zod":"catalog:"}}`),
	}})
	if err != nil {
		t.Fatal(err)
	}
	if workspaces, _ := json.Marshal(readManifest(t, got.Dir, "package.json")["workspaces"]); string(workspaces) != `["packages/api"]` {
		t.Fatalf("workspaces: %s", workspaces)
	}
	if api := deps(readManifest(t, got.Dir, "packages/api/package.json")); api["zod"] != "^3.23.0" {
		t.Fatalf("api: %v", api)
	}
}
