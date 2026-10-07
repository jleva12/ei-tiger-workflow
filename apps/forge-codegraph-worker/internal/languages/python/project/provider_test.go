package project

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/languages/python/environment"
)

func TestEnvironmentDiscoveryAndFingerprint(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	write := func(name, text string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(root, name), []byte(text), 0600); err != nil {
			t.Fatal(err)
		}
	}
	write("pyproject.toml", "[tool.pyright]\npythonVersion = '3.11'\nextraPaths = ['lib']\n")
	write("setup.py", "raise RuntimeError('this file must never execute')\n")
	req := bc.Request{Checkout: bc.Checkout{Path: root, RepositoryID: "repo", SnapshotID: "snapshot"}, Limits: bc.DefaultLimits()}
	p := Provider{Config: Config{Version: "3.12", Platform: "Linux"}}
	first, err := p.Build(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	set := first.Inventory.SourceSets[0]
	s, err := environment.Decode(set.LanguageOptions, set.LanguageVersion)
	if err != nil {
		t.Fatal(err)
	}
	if s.Version != "3.11" || len(s.Roots) != 3 || s.Roots[2] != "lib" {
		t.Fatalf("project settings: %+v", s)
	}
	if first.Status != bc.Incomplete {
		t.Fatal("static inventory claimed runtime completeness")
	}
	write("uv.lock", "version = 1\n")
	second, err := p.Build(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if first.ID == second.ID {
		t.Fatal("lockfile did not invalidate environment")
	}
	write("pyrightconfig.json", `{"pythonVersion":"3.13","extraPaths":["app"]}`)
	third, err := p.Build(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if third.Inventory.SourceSets[0].LanguageVersion != "3.13" {
		t.Fatal("pyright config did not override pyproject")
	}
	p.Config.VersionExplicit = true
	fourth, err := p.Build(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if fourth.Inventory.SourceSets[0].LanguageVersion != "3.12" {
		t.Fatal("operator version did not take precedence")
	}
	// Configuration beyond the input budget is not read, and the build
	// says so; the lock files are still fingerprinted.
	req.Limits.MaxInputBytes = 1
	small, err := p.Build(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	skipped := false
	for _, gap := range small.Inventory.MissingInputs {
		skipped = skipped || (gap.Requested == bc.GapConfiguration && strings.Contains(gap.Reason, "was not read"))
	}
	if !skipped || small.Inventory.SourceSets[0].LanguageVersion != "3.12" {
		t.Fatalf("input budget: %+v", small.Inventory.MissingInputs)
	}
}

// A project setting the analysis cannot follow is left out with a gap that
// says so, never followed out of the checkout and never fatal: escaping
// paths, several execution environments, a platform list, a configuration
// with comments that is still broken, a pyproject.toml that does not parse.
func TestLeavesOutWhatItCannotFollow(t *testing.T) {
	for _, tc := range []struct{ name, body, note string }{
		{"pyrightconfig.json", `{"extraPaths":["../outside", "/abs", "lib"]}`, "extraPaths entry ../outside"},
		{"pyrightconfig.json", `{"executionEnvironments":[{"root":"../a"},{"root":"b","extraPaths":["/abs"]}]}`, "has no root inside the checkout"},
		{"pyrightconfig.json", `{"executionEnvironments":[{"root":"b","extraPaths":["/abs"]}]}`, "extraPaths entry /abs of the b environment"},
		{"pyrightconfig.json", `{"pythonPlatform":"All"}`, `pythonPlatform "All"`},
		{"pyrightconfig.json", `{"pythonVersion": "3.11",, }`, "pyrightconfig.json could not be read"},
		{"pyproject.toml", "[tool.pyright\npythonVersion = 3.11", "pyproject.toml could not be read"},
	} {
		root := t.TempDir()
		if err := os.WriteFile(filepath.Join(root, tc.name), []byte(tc.body), 0600); err != nil {
			t.Fatal(err)
		}
		got, err := (Provider{Config: Config{Version: "3.12", Platform: "Linux"}}).Build(context.Background(), bc.Request{Checkout: bc.Checkout{Path: root, RepositoryID: "repo", SnapshotID: "snapshot"}, Limits: bc.DefaultLimits()})
		if err != nil {
			t.Fatalf("%s: %v", tc.body, err)
		}
		var reasons []string
		for _, gap := range got.Inventory.MissingInputs {
			if gap.Requested == bc.GapConfiguration {
				reasons = append(reasons, gap.Reason)
			}
		}
		if !strings.Contains(strings.Join(reasons, "\n"), tc.note) {
			t.Fatalf("%s: gaps %q, want %q", tc.body, reasons, tc.note)
		}
		s, err := environment.Decode(got.Inventory.SourceSets[0].LanguageOptions, got.Inventory.SourceSets[0].LanguageVersion)
		if err != nil {
			t.Fatal(err)
		}
		for _, r := range s.Roots {
			if r == "../outside" || r == "/abs" {
				t.Fatalf("followed %s out of the checkout", r)
			}
		}
	}
	// Comments and trailing commas are read the way Pyright reads them.
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "pyrightconfig.json"), []byte("{\n  // the analysis target\n  \"pythonVersion\": \"3.11\",\n}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	got, err := (Provider{Config: Config{Version: "3.12", Platform: "Linux"}}).Build(context.Background(), bc.Request{Checkout: bc.Checkout{Path: root, RepositoryID: "repo", SnapshotID: "snapshot"}, Limits: bc.DefaultLimits()})
	if err != nil || got.Inventory.SourceSets[0].LanguageVersion != "3.11" {
		t.Fatalf("JSON with comments: %v", err)
	}
}

// The projects nested in a monorepo are found by their manifests, with the
// directories their packages are in. Sample projects under tests keep to
// themselves, and installed or hidden trees are not searched. The
// repository's own executionEnvironments replace the search.
func TestNestedProjectsAreFound(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	write := func(name, text string) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(filepath.Join(root, name)), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, name), []byte(text), 0600); err != nil {
			t.Fatal(err)
		}
	}
	write("pyproject.toml", "[project]\nname = 'mono'\n")
	write("apps/api/pyproject.toml", "[tool.poetry]\npackages = [{ include = 'api', from = 'lib' }]\n")
	write("apps/api/lib/api/__init__.py", "")
	write("apps/worker/setup.cfg", "[metadata]\nname = worker\n\n[options]\npackage_dir =\n    = source\n")
	write("apps/worker/source/worker/__init__.py", "")
	write("packages/common/pyproject.toml", "[project]\nname = 'common'\n")
	write("packages/common/src/common/__init__.py", "")
	write("packages/flat/setup.py", "raise RuntimeError('this file must never execute')\n")
	write("packages/tools/pyproject.toml", "[tool.setuptools.package-dir]\n\"\" = \"python\"\n")
	write("packages/tools/python/tools/__init__.py", "")
	write("packages/wheel/pyproject.toml", "[tool.hatch.build.targets.wheel]\npackages = [\"code/wheel\"]\n")
	write("packages/wheel/code/wheel/__init__.py", "")
	write("tests/fixtures/requests/pyproject.toml", "[project]\nname = 'requests'\n")
	write("web/node_modules/pkg/pyproject.toml", "")
	write(".venv/lib/pkg/pyproject.toml", "")
	req := bc.Request{Checkout: bc.Checkout{Path: root, RepositoryID: "repo", SnapshotID: "snapshot"}, Limits: bc.DefaultLimits()}
	p := Provider{Config: Config{Version: "3.12", Platform: "Linux"}}
	projects := func(c bc.BuildContext) []environment.Project {
		t.Helper()
		set := c.Inventory.SourceSets[0]
		s, err := environment.Decode(set.LanguageOptions, set.LanguageVersion)
		if err != nil {
			t.Fatal(err)
		}
		return s.Projects
	}
	first, err := p.Build(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	got := fmt.Sprint(projects(first))
	want := "[{apps/api [apps/api/lib] true} {apps/worker [apps/worker/source] true} {packages/common [packages/common/src] true} {packages/flat [] true} {packages/tools [packages/tools/python] true} {packages/wheel [packages/wheel/code] true} {tests/fixtures/requests [] false}]"
	if got != want {
		t.Fatalf("projects:\n got %s\nwant %s", got, want)
	}
	write("apps/api/pyproject.toml", "[tool.poetry]\npackages = [{ include = 'api' }]\n")
	second, err := p.Build(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if first.ID == second.ID || fmt.Sprint(projects(second)[0]) != "{apps/api [] true}" {
		t.Fatalf("a project's layout change is not seen: %v", projects(second)[0])
	}
	write("pyrightconfig.json", `{"executionEnvironments":[{"root":"services/a","extraPaths":["libs"]},{"root":".","extraPaths":["shared"]}]}`)
	third, err := p.Build(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	set := third.Inventory.SourceSets[0]
	s, err := environment.Decode(set.LanguageOptions, set.LanguageVersion)
	if err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprint(s.Projects); got != "[{services/a [libs] false}]" || fmt.Sprint(s.Roots) != "[. src shared]" {
		t.Fatalf("declared environments: %s, roots %v", got, s.Roots)
	}
}

func TestSetupCfgPackageDir(t *testing.T) {
	for text, want := range map[string]string{
		"[options]\npackage_dir = =src\n":                        "src",
		"[options]\npackage_dir =\n    pkg = other\n    = lib\n": "lib",
		"[options]\npackages = find:\n":                          "",
		"[metadata]\npackage_dir = =src\n":                       "",
	} {
		if got := setupCfgPackageDir(text); got != want {
			t.Fatalf("%q: %q, want %q", text, got, want)
		}
	}
}

func TestRequirementsAreOnlyPackagesOfTheIndex(t *testing.T) {
	for line, want := range map[string]string{
		"requests":                  "requests",
		"fastapi[standard] >=0.115": "fastapi[standard] >=0.115",
		"Django>=4,<5 ; python_version >= '3.10'": "Django >=4,<5 ; python_version >= '3.10'",
		"celery-types (>=0.26.0,<0.27.0)":         "celery-types >=0.26.0,<0.27.0",
		"numpy==1.26.4 --hash=sha256:abc \\":      "numpy ==1.26.4",
		"uvicorn[standard]>=0.34  # the server":   "uvicorn[standard] >=0.34",
		"pkg @ https://example.com/pkg.whl":       "",
		"git+https://github.com/org/private.git":  "",
		"-e .":                                    "",
		"./packages/common":                       "",
		"--index-url https://example.com/simple":  "",
		"name ; os_name == `rm -rf`":              "",
		"weird !! version":                        "",
	} {
		got, _, ok := requirement(line)
		if (want == "") == ok || got != want {
			t.Fatalf("%q: %q %v, want %q", line, got, ok, want)
		}
	}
	if normalize("Foo.Bar__baz") != "foo-bar-baz" {
		t.Fatal(normalize("Foo.Bar__baz"))
	}
}

// Every manifest format a project may declare packages in is read; the
// checkout's own projects and path dependencies are not packages to
// install; lockfiles pin versions from the index only.
func TestDeclaredPackagesAcrossManifests(t *testing.T) {
	root := t.TempDir()
	write := func(name, text string) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(filepath.Join(root, name)), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, name), []byte(text), 0600); err != nil {
			t.Fatal(err)
		}
	}
	write("pyproject.toml", `[project]
name = "mono"
dependencies = ["common-lib", "httpx>=0.27"]
[project.optional-dependencies]
dev = ["pytest"]
[dependency-groups]
lint = ["ruff", {include-group = "dev"}]
[tool.uv]
dev-dependencies = ["mypy"]
`)
	write("uv.lock", `version = 1
[[package]]
name = "httpx"
version = "0.28.1"
source = { registry = "https://pypi.org/simple" }
[[package]]
name = "common-lib"
version = "0.1.0"
source = { editable = "packages/common" }
`)
	write("packages/common/pyproject.toml", "[project]\nname = \"common_lib\"\ndependencies = [\"SQLAlchemy[asyncio]>=2.0\"]\n")
	write("apps/api/pyproject.toml", `[tool.poetry]
name = "api"
[tool.poetry.dependencies]
python = "^3.12"
fastapi = "^0.115"
pydantic = { version = "2.9.2", extras = ["email"] }
common-lib = { path = "../../packages/common", develop = true }
private = { git = "https://github.com/org/private.git" }
[tool.poetry.group.test.dependencies]
pytest-asyncio = "*"
`)
	write("apps/api/poetry.lock", "[[package]]\nname = \"fastapi\"\nversion = \"0.115.6\"\n\n[[package]]\nname = \"common-lib\"\nversion = \"0.1.0\"\n[package.source]\ntype = \"directory\"\nurl = \"../../packages/common\"\n")
	write("apps/worker/setup.cfg", "[metadata]\nname = worker\n\n[options]\ninstall_requires =\n    celery[redis]>=5.4\n    redis\n[options.extras_require]\nsentry = sentry-sdk\n")
	write("apps/worker/setup.py", "from setuptools import setup\nsetup(install_requires=['boto3>=1.35', \"structlog\"])\n")
	write("apps/worker/requirements.txt", "-r requirements/base.txt\n--index-url https://example.com/simple\nrich==13.9.4 \\\n    --hash=sha256:abc\n-e ../../packages/common\n")
	write("apps/worker/requirements/base.txt", "click\ngit+https://github.com/org/tool.git\n")
	write("apps/web/Pipfile", "[packages]\nflask = \"*\"\ngunicorn = \">=22\"\n[dev-packages]\nblack = {version = \"==24.10.0\"}\n")
	opened, err := os.OpenRoot(root)
	if err != nil {
		t.Fatal(err)
	}
	defer opened.Close()
	d, err := declaredPackages(opened, []string{"packages/common", "apps/api", "apps/worker", "apps/web"}, bc.DefaultLimits(), io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	got := strings.Join(d.Requirements, "|")
	for _, want := range []string{"httpx >=0.27", "pytest", "ruff", "mypy", "SQLAlchemy[asyncio] >=2.0", "fastapi", "pydantic[email] ==2.9.2", "pytest-asyncio", "celery[redis] >=5.4", "redis", "sentry-sdk", "boto3 >=1.35", "structlog", "rich ==13.9.4", "click", "flask", "gunicorn >=22", "black ==24.10.0"} {
		if !strings.Contains("|"+got+"|", "|"+want+"|") {
			t.Fatalf("%q missing from %s", want, got)
		}
	}
	for _, unwanted := range []string{"common-lib", "private", "python", "include-group", "tool.git"} {
		if strings.Contains(got, unwanted) {
			t.Fatalf("%q is not a package to install: %s", unwanted, got)
		}
	}
	if strings.Join(d.Pins, ",") != "fastapi==0.115.6,httpx==0.28.1" {
		t.Fatalf("pins: %v", d.Pins)
	}
	if len(d.Notes) != 1 || !strings.Contains(d.Notes[0], "git+https://github.com/org/tool.git") {
		t.Fatalf("notes: %q", d.Notes)
	}
}
