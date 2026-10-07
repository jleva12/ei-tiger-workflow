package discovery

import (
	"context"
	"errors"
	"os"
	"path"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/worker/internal/buildcontext/syntax"
)

type extensionClassifier map[string]string

func (c extensionClassifier) LanguageForPath(name string) (string, bool) {
	language, ok := c[path.Ext(name)]
	return language, ok
}

func fixture(t *testing.T) (bc.Checkout, bc.BuildContext) {
	t.Helper()
	dir := t.TempDir()
	for p, content := range map[string]string{"src/A.java": "class A {}", "src/nested/B.java": "class B {}", "other/C.java": "class C {}", "src/readme.md": "ignored", ".git/secret.java": "ignored"} {
		name := filepath.Join(dir, p)
		if err := os.MkdirAll(filepath.Dir(name), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(name, []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
	}
	checkout := bc.Checkout{Path: dir, RepositoryID: "repo", SnapshotID: "snapshot"}
	provider, _ := syntax.New(21)
	build, err := provider.Build(context.Background(), bc.Request{Checkout: checkout, Limits: bc.DefaultLimits()})
	if err != nil {
		t.Fatal(err)
	}
	return checkout, build
}
func reseal(t *testing.T, c bc.BuildContext) bc.BuildContext {
	t.Helper()
	c.Producer.InputSHA256, _ = c.Inventory.Digest()
	c, err := bc.Seal(c)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestRootsContextsAndIndependentIdentities(t *testing.T) {
	checkout, build := fixture(t)
	build.Inventory.Inputs = append(build.Inventory.Inputs, bc.Input{ID: "nested", Kind: bc.InputSourceRoot, Location: &bc.Location{Root: "checkout", Path: "src/nested"}})
	build.Checks = append(build.Checks, bc.InputCheck{InputID: "nested", Status: bc.Available})
	build.Inventory.SourceSets[0].SourceRootIDs = append(build.Inventory.SourceSets[0].SourceRootIDs, "nested", "checkout-sources")
	second := build.Inventory.SourceSets[0]
	second.ID = "test"
	second.SourceRootIDs = []bc.InputID{"nested"}
	build.Inventory.SourceSets = append(build.Inventory.SourceSets, second)
	build = reseal(t, build)
	var sources []ir.Source
	report, err := WalkWithClassifier(context.Background(), checkout, build, DefaultLimits(), extensionClassifier{".java": "java"}, func(s ir.Source) error { sources = append(sources, s); return nil })
	if err != nil {
		t.Fatal(err)
	}
	if report.Files != 4 || report.IgnoredGitEntries != 1 || report.UnsupportedFiles != 1 || report.OtherLanguageFiles != 0 || report.Omissions != 0 {
		t.Fatalf("report=%+v", report)
	}
	var names []string
	ids := map[ir.FileID]bool{}
	for _, s := range sources {
		names = append(names, s.SourceSetID+":"+s.Path)
		if ids[s.FileID] || s.BuildContextID != string(build.ID) || s.ContentSHA256 == "" {
			t.Fatal("identity lost")
		}
		ids[s.FileID] = true
	}
	sort.Strings(names)
	want := []string{"syntax:other/C.java", "syntax:src/A.java", "syntax:src/nested/B.java", "test:src/nested/B.java"}
	if !reflect.DeepEqual(names, want) {
		t.Fatalf("sources=%v", names)
	}
	root, err := os.OpenRoot(checkout.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	for _, s := range sources {
		data, err := ReadSource(context.Background(), root, s, 1<<20)
		if err != nil || len(data) == 0 {
			t.Fatalf("read: %s %v", data, err)
		}
	}
}

func TestOmissionsLimitsAndSourceChanges(t *testing.T) {
	checkout, build := fixture(t)
	if err := os.Symlink("src/A.java", filepath.Join(checkout.Path, "linked.java")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(t.TempDir(), filepath.Join(checkout.Path, "outside")); err != nil {
		t.Fatal(err)
	}
	build.Inventory.Inputs = append(build.Inventory.Inputs,
		bc.Input{ID: "missing", Kind: bc.InputGeneratedRoot, UnavailableReason: "not generated"},
		bc.Input{ID: "external", Kind: bc.InputGeneratedRoot, Location: &bc.Location{Root: "generated", Path: "."}, SHA256: build.Producer.InputSHA256})
	build.Checks = append(build.Checks, bc.InputCheck{InputID: "missing", Status: bc.Missing}, bc.InputCheck{InputID: "external", Status: bc.Available, ObservedSHA256: build.Producer.InputSHA256})
	build.Inventory.SourceSets[0].GeneratedRootIDs = []bc.InputID{"missing", "external"}
	build = reseal(t, build)
	var sources []ir.Source
	limits := DefaultLimits()
	limits.MaxIssues = 2
	report, err := WalkWithClassifier(context.Background(), checkout, build, limits, extensionClassifier{".java": "java"}, func(s ir.Source) error { sources = append(sources, s); return nil })
	if err != nil {
		t.Fatal(err)
	}
	if report.Files != 3 || report.Omissions != 4 || len(report.Issues) != 2 {
		t.Fatalf("omissions=%+v", report)
	}
	for _, mutate := range []func(*Limits){func(l *Limits) { l.MaxFiles = 1 }, func(l *Limits) { l.MaxEntries = 1 }, func(l *Limits) { l.MaxHashBytes = 1 }, func(l *Limits) { l.MaxRoots = 1 }} {
		l := DefaultLimits()
		mutate(&l)
		_, err := WalkWithClassifier(context.Background(), checkout, build, l, extensionClassifier{".java": "java"}, func(ir.Source) error { return nil })
		if !errors.Is(err, ErrLimit) {
			t.Fatalf("limit=%+v: %v", l, err)
		}
	}
	// A tree nested beyond the depth limit is left out, not the run.
	deep := DefaultLimits()
	deep.MaxDepth = 1
	report, err = WalkWithClassifier(context.Background(), checkout, build, deep, extensionClassifier{".java": "java"}, func(ir.Source) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	tooDeep := false
	for _, issue := range report.Issues {
		tooDeep = tooDeep || issue.Code == "too_deep"
	}
	if !tooDeep {
		t.Fatalf("no too_deep issue: %+v", report.Issues)
	}
	root, err := os.OpenRoot(checkout.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	s := sources[0]
	if err := os.WriteFile(filepath.Join(checkout.Path, s.Path), []byte("class Z {}"), 0600); err != nil {
		t.Fatal(err)
	}
	_, err = ReadSource(context.Background(), root, s, 1<<20)
	if !errors.Is(err, ErrChanged) {
		t.Fatalf("changed source=%v", err)
	}
	s.Path = "linked.java"
	_, err = ReadSource(context.Background(), root, s, 1<<20)
	if !errors.Is(err, ErrUnsupported) {
		t.Fatalf("symlink=%v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err = WalkWithClassifier(ctx, checkout, build, DefaultLimits(), extensionClassifier{".java": "java"}, func(ir.Source) error { return nil })
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestDiscoveryUsesConfiguredLanguagesWithoutADefault(t *testing.T) {
	checkout := bc.Checkout{Path: t.TempDir(), RepositoryID: "repo", SnapshotID: "snapshot"}
	for name, content := range map[string]string{"one.alpha": "alpha input", "two.beta": "beta input", "readme.md": "ignored"} {
		if err := os.WriteFile(filepath.Join(checkout.Path, name), []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
	}
	provider, err := syntax.NewProfiles(
		syntax.Profile{Language: "alpha", Version: "1", Roots: []string{"."}},
		syntax.Profile{Language: "beta", Version: "2", Roots: []string{"."}},
	)
	if err != nil {
		t.Fatal(err)
	}
	build, err := provider.Build(context.Background(), bc.Request{Checkout: checkout, Limits: bc.DefaultLimits()})
	if err != nil {
		t.Fatal(err)
	}
	var sources []ir.Source
	emit := func(source ir.Source) error { sources = append(sources, source); return nil }
	report, err := WalkWithClassifier(context.Background(), checkout, build, DefaultLimits(), extensionClassifier{".alpha": "alpha", ".beta": "beta"}, emit)
	if err != nil {
		t.Fatal(err)
	}
	// Both roots visit every entry, but each context emits only its own language.
	if report.Entries != 6 || report.Files != 2 || report.UnsupportedFiles != 2 || report.OtherLanguageFiles != 2 || report.Omissions != 0 || len(sources) != 2 {
		t.Fatalf("report=%+v sources=%+v", report, sources)
	}
	for _, source := range sources {
		if (source.Path == "one.alpha" && source.Language == "alpha" && source.LanguageVersion == "1") ||
			(source.Path == "two.beta" && source.Language == "beta" && source.LanguageVersion == "2") {
			continue
		}
		t.Fatalf("source profile changed: %+v", source)
	}
	sources = nil
	if _, err := WalkWithClassifier(context.Background(), checkout, build, DefaultLimits(), nil, emit); err == nil || len(sources) != 0 {
		t.Fatal("missing classifier must fail before emitting sources")
	}
}

func TestCompilationFiltersApplyRelativeToEachRoot(t *testing.T) {
	checkout, build := fixture(t)
	for i := range build.Inventory.Inputs {
		if build.Inventory.Inputs[i].ID == "checkout-sources" {
			build.Inventory.Inputs[i].Location.Path = "src"
		}
	}
	build.Inventory.SourceSets[0].IncludePatterns = []string{"**/*.java"}
	build.Inventory.SourceSets[0].ExcludePatterns = []string{"**/B.java"}
	build = reseal(t, build)
	var names []string
	report, err := WalkWithClassifier(context.Background(), checkout, build, DefaultLimits(), extensionClassifier{".java": "java"}, func(source ir.Source) error { names = append(names, source.Path); return nil })
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(names, []string{"src/A.java"}) || report.ExcludedFiles != 1 || report.Omissions != 0 || report.Files != 1 {
		t.Fatalf("names=%v report=%+v", names, report)
	}
}

func TestWholeTreeExcludesPruneTheWalk(t *testing.T) {
	checkout, build := fixture(t)
	for _, name := range []string{"node_modules/dep/C.java", "src/node_modules/dep/D.java", "out/E.java"} {
		full := filepath.Join(checkout.Path, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte("class X {}"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	build.Inventory.SourceSets[0].ExcludePatterns = []string{"**/node_modules/**", "out/**"}
	build = reseal(t, build)
	var names []string
	report, err := WalkWithClassifier(context.Background(), checkout, build, DefaultLimits(), extensionClassifier{".java": "java"}, func(source ir.Source) error { names = append(names, source.Path); return nil })
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range names {
		if strings.Contains(name, "node_modules") || strings.HasPrefix(name, "out/") {
			t.Fatalf("excluded tree emitted %s", name)
		}
	}
	if report.ExcludedDirectories != 3 || report.ExcludedFiles != 0 || len(names) == 0 {
		t.Fatalf("names=%v report=%+v", names, report)
	}
}

// A name the IR cannot carry, like the Icon\r file macOS puts in folders with
// a custom icon, is left out with an issue; the rest of the tree is walked.
func TestUnrepresentableNamesAreLeftOut(t *testing.T) {
	checkout, build := fixture(t)
	dir := filepath.Join(checkout.Path, "src")
	if err := os.WriteFile(filepath.Join(dir, "Icon\r"), []byte{}, 0o600); err != nil {
		t.Skipf("the file system refuses the name: %v", err)
	}
	var sources []ir.Source
	report, err := WalkWithClassifier(context.Background(), checkout, build, DefaultLimits(), extensionClassifier{".java": "java"}, func(s ir.Source) error { sources = append(sources, s); return nil })
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, issue := range report.Issues {
		found = found || issue.Code == "unrepresentable_path"
	}
	if !found || len(sources) == 0 {
		t.Fatalf("issues=%+v sources=%d", report.Issues, len(sources))
	}
}

// Dot paths and tests are left out unless the limits admit them: hidden
// directories and files, test source sets, and the directories and file
// names each language's test runners use. A Java package named test is code.
func TestHiddenPathsAndTestsAreLeftOut(t *testing.T) {
	checkout := bc.Checkout{Path: t.TempDir(), RepositoryID: "repo", SnapshotID: "snapshot"}
	kept := []string{"app/main.py", "app/testing.py", "web/src/a.ts", "web/src/contest.ts", "svc/src/main/java/com/acme/test/Main.java"}
	left := []string{
		"app/tests/test_x.py", "app/test_y.py", "app/y_test.py", "conftest.py", "app/tests.py",
		"web/src/a.test.ts", "web/src/b.spec.tsx", "web/src/__tests__/c.ts", "web/src/__mocks__/d.ts", "web/test/e.ts",
		"svc/src/test/java/ATest.java",
		".agents/skills/x.js", ".eslintrc.js", "web/.storybook/main.ts",
	}
	for _, name := range append(append([]string(nil), kept...), left...) {
		full := filepath.Join(checkout.Path, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	provider, err := syntax.NewProfiles(
		syntax.Profile{Language: "python", Version: "3.12", Roots: []string{"."}},
		syntax.Profile{Language: "typescript", Version: "5", Roots: []string{"."}},
		syntax.Profile{Language: "java", Version: "21", Roots: []string{"."}},
	)
	if err != nil {
		t.Fatal(err)
	}
	build, err := provider.Build(context.Background(), bc.Request{Checkout: checkout, Limits: bc.DefaultLimits()})
	if err != nil {
		t.Fatal(err)
	}
	tests := build.Inventory.SourceSets[0]
	tests.ID, tests.Name, tests.Kind = "tests", "test", bc.SourceSetTest
	build.Inventory.SourceSets = append(build.Inventory.SourceSets, tests)
	build = reseal(t, build)
	classifier := extensionClassifier{".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "typescript", ".java": "java"}
	walk := func(limits Limits) ([]string, Report) {
		t.Helper()
		seen := map[string]bool{}
		report, err := WalkWithClassifier(context.Background(), checkout, build, limits, classifier, func(s ir.Source) error { seen[s.Path] = true; return nil })
		if err != nil {
			t.Fatal(err)
		}
		var names []string
		for name := range seen {
			names = append(names, name)
		}
		sort.Strings(names)
		return names, report
	}
	names, report := walk(DefaultLimits())
	want := append([]string(nil), kept...)
	sort.Strings(want)
	if !reflect.DeepEqual(names, want) {
		t.Fatalf("names = %v, want %v", names, want)
	}
	if report.TestSourceSets != 1 || report.HiddenEntries == 0 || report.TestEntries == 0 || report.Omissions != 0 {
		t.Fatalf("report = %+v", report)
	}
	all := DefaultLimits()
	all.IncludeHidden, all.IncludeTests = true, true
	names, report = walk(all)
	want = append(append([]string(nil), kept...), left...)
	sort.Strings(want)
	if !reflect.DeepEqual(names, want) || report.HiddenEntries != 0 || report.TestEntries != 0 || report.TestSourceSets != 0 {
		t.Fatalf("with hidden paths and tests: names = %v report = %+v", names, report)
	}
}
