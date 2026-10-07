package composite

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/buildcontext/syntax"
)

// fake is a build provider that answers with a fixed inventory or error.
type fake struct {
	module bc.ModuleID
	sets   []bc.SourceSet
	err    error
}

func (f fake) Build(ctx context.Context, r bc.Request) (bc.BuildContext, error) {
	if f.err != nil {
		return bc.BuildContext{}, f.err
	}
	in := bc.Inventory{Modules: []bc.Module{{ID: f.module, Name: "module", Directory: "."}}}
	for _, set := range f.sets {
		for _, id := range set.SourceRootIDs {
			in.Inputs = append(in.Inputs, bc.Input{ID: id, Kind: bc.InputSourceRoot, Location: &bc.Location{Root: "checkout", Path: string(id)}})
		}
		in.SourceSets = append(in.SourceSets, set)
	}
	var checks []bc.InputCheck
	for _, x := range in.Inputs {
		checks = append(checks, bc.InputCheck{InputID: x.ID, Status: bc.Available})
	}
	digest, err := in.Digest()
	if err != nil {
		return bc.BuildContext{}, err
	}
	return bc.Seal(bc.BuildContext{RepositoryID: r.Checkout.RepositoryID, SnapshotID: r.Checkout.SnapshotID, Producer: bc.Producer{Name: "fake", Version: "1", InputSHA256: digest}, Inventory: in, Checks: checks})
}

func declared(module bc.ModuleID, id bc.SourceSetID, language, root string) fake {
	return fake{module: module, sets: []bc.SourceSet{{ID: id, ModuleID: module, Name: "main", Kind: bc.SourceSetMain, Language: language, LanguageVersion: "5.4", SourceRootIDs: []bc.InputID{bc.InputID(root)}}}}
}

func request(t *testing.T) bc.Request {
	t.Helper()
	dir := t.TempDir()
	for _, d := range []string{"api/src", "web/src"} {
		if err := os.MkdirAll(filepath.Join(dir, d), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return bc.Request{Checkout: bc.Checkout{Path: dir, RepositoryID: "repo", SnapshotID: "snapshot"}, Limits: bc.DefaultLimits()}
}

// profile is a repository-wide fallback; Java needs a real release.
func profile(language string) syntax.Profile {
	version := "1"
	if language == "java" {
		version = "21"
	}
	return syntax.Profile{Language: language, Version: version, Roots: []string{"."}}
}

func TestDeclaredSetsWinAndFallbackCoversTheRest(t *testing.T) {
	p, err := New(
		Member{Language: "typescript", Provider: declared("web", "web-main", "typescript", "web/src"), Fallback: profile("typescript")},
		Member{Language: "java", Provider: fake{err: bc.ErrNoBuild}, Fallback: profile("java")},
		Member{Language: "python", Fallback: profile("python")},
	)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(p.Languages(), ",") != "java,python,typescript" {
		t.Fatalf("languages: %v", p.Languages())
	}
	c, err := p.Build(context.Background(), request(t))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Validate(); err != nil {
		t.Fatalf("composed context is not sealed: %v", err)
	}
	byLanguage := map[string][]bc.SourceSet{}
	for _, set := range c.Inventory.SourceSets {
		language, _ := set.SyntaxLanguage()
		byLanguage[language] = append(byLanguage[language], set)
	}
	if sets := byLanguage["typescript"]; len(sets) != 1 || sets[0].ID != "web-main" {
		t.Fatalf("a declared language keeps its declared set only: %+v", sets)
	}
	for _, language := range []string{"java", "python"} {
		sets := byLanguage[language]
		if len(sets) != 1 || sets[0].ID != bc.SourceSetID("syntax-"+language) || sets[0].ModuleID != bc.ModuleID("syntax-"+language) || len(sets[0].SourceRootIDs) != 1 {
			t.Fatalf("%s fallback set: %+v", language, sets)
		}
	}
	if len(byLanguage["java"][0].SourceRootIDs) != 1 || byLanguage["java"][0].TargetRelease != 21 || len(c.Inventory.JDKs) != 1 {
		t.Fatalf("java fallback carries its syntax-only JDK: %+v %+v", byLanguage["java"], c.Inventory.JDKs)
	}
	if c.Status != bc.Incomplete {
		t.Fatalf("fallbacks declare a gap, so the context is incomplete: %s", c.Status)
	}
	gaps := map[bc.GapID]bool{}
	for _, g := range c.Inventory.MissingInputs {
		gaps[g.ID] = true
	}
	if !gaps["syntax-java-build-inventory"] || !gaps["syntax-python-build-inventory"] || len(gaps) != 2 {
		t.Fatalf("namespaced gaps: %v", gaps)
	}
	if c.Producer.Name != "composite-languages" || c.Producer.Version != Version {
		t.Fatalf("producer: %+v", c.Producer)
	}
	// The composition is deterministic and records which member described a language.
	again, err := p.Build(context.Background(), request(t))
	if err != nil || again.Producer.InputSHA256 != c.Producer.InputSHA256 {
		t.Fatalf("producer digest unstable: %v", err)
	}
	// A provider that finds no build and no provider at all both end in the
	// same fallback, so they compose identically; a declared build does not.
	same, _ := New(
		Member{Language: "typescript", Provider: declared("web", "web-main", "typescript", "web/src"), Fallback: profile("typescript")},
		Member{Language: "java", Fallback: profile("java")},
		Member{Language: "python", Fallback: profile("python")},
	)
	d, err := same.Build(context.Background(), request(t))
	if err != nil || d.Producer.InputSHA256 != c.Producer.InputSHA256 || d.ID != c.ID {
		t.Fatalf("no provider and no build must compose identically: %v", err)
	}
	other, _ := New(
		Member{Language: "typescript", Fallback: profile("typescript")},
		Member{Language: "java", Fallback: profile("java")},
		Member{Language: "python", Fallback: profile("python")},
	)
	if d, err = other.Build(context.Background(), request(t)); err != nil || d.Producer.InputSHA256 == c.Producer.InputSHA256 {
		t.Fatalf("a declared build and a fallback must fingerprint differently: %v", err)
	}
}

// A build that fails leaves its language to the fallback profile, with a
// gap saying why; the other languages keep their builds. Only cancellation
// fails the whole context.
func TestBuildFailuresFallBackWithAGap(t *testing.T) {
	boom := errors.New("maven exploded:\n  parent POM not found")
	p, err := New(
		Member{Language: "java", Provider: fake{err: boom}, Fallback: profile("java")},
		Member{Language: "python", Fallback: profile("python")},
	)
	if err != nil {
		t.Fatal(err)
	}
	c, err := p.Build(context.Background(), request(t))
	if err != nil {
		t.Fatal(err)
	}
	var gap *bc.MissingInput
	for i, g := range c.Inventory.MissingInputs {
		if g.Requested == bc.GapBuildFailed {
			gap = &c.Inventory.MissingInputs[i]
		}
	}
	if gap == nil || !strings.Contains(gap.Reason, "The java build failed: maven exploded: parent POM not found") || c.Status != bc.Incomplete {
		t.Fatalf("gap %+v status %s", gap, c.Status)
	}
	languages := map[string]bool{}
	for _, set := range c.Inventory.SourceSets {
		language, _ := set.SyntaxLanguage()
		languages[language] = true
	}
	if !languages["java"] || !languages["python"] {
		t.Fatalf("source sets %+v", c.Inventory.SourceSets)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err = p.Build(ctx, request(t)); err == nil {
		t.Fatal("a canceled build succeeded")
	}
}

func TestIdentifierCollisionsAndInvalidMembers(t *testing.T) {
	p, err := New(
		Member{Language: "kotlin", Provider: declared("shared", "shared-main", "kotlin", "api/src"), Fallback: profile("kotlin")},
		Member{Language: "scala", Provider: declared("shared", "shared-main", "scala", "api/src"), Fallback: profile("scala")},
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = p.Build(context.Background(), request(t)); !errors.Is(err, bc.ErrInvalidInput) || !strings.Contains(err.Error(), "declared by both kotlin and scala") {
		t.Fatalf("collision must be reported: %v", err)
	}
	for _, members := range [][]Member{
		nil,
		{{Language: "", Fallback: profile("java")}},
		{{Language: "java", Fallback: profile("kotlin")}},
		{{Language: "java", Fallback: syntax.Profile{Language: "java", Roots: []string{"."}}}},
		{{Language: "java", Fallback: syntax.Profile{Language: "java", Version: "21"}}},
		{{Language: "java", Fallback: syntax.Profile{Language: "java", Version: "1", Roots: []string{"."}}}},
		{{Language: "java", Fallback: profile("java")}, {Language: "java", Fallback: profile("java")}},
	} {
		if _, err := New(members...); err == nil {
			t.Fatalf("accepted invalid members: %+v", members)
		}
	}
}

// scanned is a build provider that, like the Python and TypeScript project
// providers, describes the checkout with a syntax scan of its own.
func scanned(t *testing.T, language string) bc.Provider {
	t.Helper()
	p, err := syntax.NewProfiles(profile(language))
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func TestSyntaxScanningBuildsShareACheckout(t *testing.T) {
	// A repository with both a Python and a TypeScript project: each build
	// provider scans by syntax and names its module "syntax".
	p, err := New(
		Member{Language: "python", Provider: scanned(t, "python"), Fallback: profile("python")},
		Member{Language: "typescript", Provider: scanned(t, "typescript"), Fallback: profile("typescript")},
	)
	if err != nil {
		t.Fatal(err)
	}
	c, err := p.Build(context.Background(), request(t))
	if err != nil {
		t.Fatalf("two syntax-scanning builds must compose: %v", err)
	}
	modules := map[bc.ModuleID]bool{}
	for _, m := range c.Inventory.Modules {
		modules[m.ID] = true
	}
	if len(modules) != 2 || !modules["syntax-python"] || !modules["syntax-typescript"] {
		t.Fatalf("each language needs its own module: %v", modules)
	}
	for _, set := range c.Inventory.SourceSets {
		if want := bc.ModuleID("syntax-" + set.Language); set.ModuleID != want {
			t.Fatalf("source set %s in module %s, want %s", set.ID, set.ModuleID, want)
		}
	}
	gaps := map[bc.GapID]bool{}
	for _, gap := range c.Inventory.MissingInputs {
		gaps[gap.ID] = true
	}
	if !gaps["syntax-python-build-inventory"] || !gaps["syntax-typescript-build-inventory"] {
		t.Fatalf("each language needs its own gap: %v", gaps)
	}

	// Alone, a syntax-scanning build keeps its module's name, so the
	// identities of files already in the graph don't change.
	alone, err := New(Member{Language: "python", Provider: scanned(t, "python"), Fallback: profile("python")})
	if err != nil {
		t.Fatal(err)
	}
	c, err = alone.Build(context.Background(), request(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Inventory.Modules) != 1 || c.Inventory.Modules[0].ID != "syntax" {
		t.Fatalf("a lone syntax build was renamed: %+v", c.Inventory.Modules)
	}
}
