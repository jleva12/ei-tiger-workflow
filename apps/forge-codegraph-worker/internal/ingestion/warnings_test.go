package ingestion

import (
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/worker/internal/discovery"
)

// A source set the build could not compile is a warning on the run, named by
// its module and set, with the build's reason; other gaps are not.
func TestBuildWarningsNameSourceSetsWithoutOutput(t *testing.T) {
	build := bc.BuildContext{Inventory: bc.Inventory{
		Modules:    []bc.Module{{ID: "m", Name: "stg-pitbull-api"}},
		SourceSets: []bc.SourceSet{{ID: "m:main", ModuleID: "m", Name: "main"}},
		MissingInputs: []bc.MissingInput{
			{ID: "g1", Requested: bc.GapCompiledOutput, Reason: "Maven could not compile it: src/A.java:[1,9] cannot find symbol", ModuleID: "m", SourceSetID: "m:main"},
			{ID: "g2", Requested: "build-inventory", Reason: "syntax-only scan"},
		},
	}}
	gaps := buildWarnings(build, true)
	want := "stg-pitbull-api main has no compiled output, so other source sets' references into it are unresolved. Maven could not compile it: src/A.java:[1,9] cannot find symbol"
	warnings := gapTexts(gaps, nil)
	if len(warnings) != 1 || warnings[0] != want {
		t.Fatalf("warnings = %q", warnings)
	}
	// The resolver compiled it itself: nothing is left out.
	if left := gapTexts(gaps, []string{"m:main"}); len(left) != 0 {
		t.Fatalf("a set the resolver compiled is still a warning: %q", left)
	}
	if got := warningMessage(append(warnings, "Lombok could not run.")); got != want+"\nLombok could not run." {
		t.Fatalf("message = %q", got)
	}
	if warningMessage(nil) != "" {
		t.Fatal("a run without warnings has a warning message")
	}
	long := warningMessage([]string{strings.Repeat("x", 3000)})
	if len(long) > deployment.MaxErrorMessageBytes {
		t.Fatalf("message is %d bytes", len(long))
	}
}

func TestEmbeddingWarningIsRecountedNotRepeated(t *testing.T) {
	first := embeddingWarning("Lombok could not run.", 3)
	if first != "Lombok could not run.\n3"+embeddingWarningText {
		t.Fatalf("first pass: %q", first)
	}
	// A resumed pass recounts; the other warnings stay.
	if again := embeddingWarning(first, 1); again != "Lombok could not run.\n1"+embeddingWarningText {
		t.Fatalf("resumed pass: %q", again)
	}
	if clean := embeddingWarning(first, 0); clean != "Lombok could not run." {
		t.Fatalf("a pass that embedded everything: %q", clean)
	}
	if embeddingWarning("", 0) != "" {
		t.Fatal("no warnings")
	}
}

func TestOmissionAndParseWarningsNameWhatIsMissing(t *testing.T) {
	report := discovery.Report{Omissions: 5, Issues: []discovery.Issue{
		{Code: "symlink", Path: "node_modules/x"},
		{Code: "too_deep", Path: "vendor/a/b/c"},
		{Code: "too_deep", Path: "vendor/a/b/d"},
		{Code: "unrepresentable_path", Path: `"Icon\r"`},
		{Code: "unavailable_source_root", Path: "src/main/java"},
	}}
	want := `3 paths were left out of the graph: 2 nested deeper than the depth limit (e.g. vendor/a/b/c); 1 with names the graph cannot store (e.g. "Icon\r").`
	if got := omissionWarning(report); got != want {
		t.Fatalf("omissions = %q", got)
	}
	if omissionWarning(discovery.Report{Issues: []discovery.Issue{{Code: "symlink", Path: "x"}}}) != "" {
		t.Fatal("ordinary omissions are not warnings")
	}

	var stats parseStats
	if stats.warning() != "" {
		t.Fatal("nothing skipped")
	}
	stats.record(skippedFile{Path: "static/assets/index-B2XX.js", Reason: "parser: generated source: minified", Generated: true})
	if stats.warning() != "" {
		t.Fatal("a committed bundle left out on purpose is not a warning")
	}
	stats.skip("gen/Huge.java", "9000000 bytes exceed the 4194304-byte source limit")
	stats.skip("src/Slow.java", "parsing took longer than 2m0s")
	got := stats.warning()
	if !strings.HasPrefix(got, "2 source files could not be parsed") || !strings.Contains(got, "gen/Huge.java (9000000 bytes exceed") || !strings.Contains(got, "src/Slow.java (parsing took") {
		t.Fatalf("parse warning = %q", got)
	}
}

func TestSourceSetsWithoutClasspathShareOneWarning(t *testing.T) {
	build := bc.BuildContext{Inventory: bc.Inventory{
		Modules: []bc.Module{{ID: "a", Name: "api"}, {ID: "w", Name: "web"}},
		SourceSets: []bc.SourceSet{
			{ID: "a:main", ModuleID: "a", Name: "main"}, {ID: "a:test", ModuleID: "a", Name: "test"},
			{ID: "w:main", ModuleID: "w", Name: "main"},
		},
		MissingInputs: []bc.MissingInput{
			{ID: "g1", Requested: "classpath:compile", Reason: "Maven could not build it: Could not resolve dependencies for project g:api", ModuleID: "a", SourceSetID: "a:main"},
			{ID: "g2", Requested: "classpath:test", Reason: "Maven could not build it: Could not resolve dependencies for project g:api", ModuleID: "a", SourceSetID: "a:test"},
			{ID: "g3", Requested: "classpath:compile", Reason: "other", ModuleID: "w", SourceSetID: "w:main"},
		},
	}}
	texts := gapTexts(buildWarnings(build, true), nil)
	want := "api main, api test, web main have no dependency classpath, so references into libraries are unresolved there. Maven could not build it: Could not resolve dependencies for project g:api"
	if len(texts) != 1 || texts[0] != want {
		t.Fatalf("warnings = %q", texts)
	}
}

func TestDegradedContextsAreRecordedToResolveAgain(t *testing.T) {
	real := "sha256:" + digestOf("inputs")
	marked := degradedDigest(real)
	if marked == real || !deployment.ValidDigest(marked) || degradedDigest(real) != marked {
		t.Fatalf("degraded digest %q of %q", marked, real)
	}
}

// A test source set's gaps are warnings only when tests are analysed.
func TestBuildWarningsSkipTestSourceSetsLeftOut(t *testing.T) {
	build := bc.BuildContext{Inventory: bc.Inventory{
		Modules:    []bc.Module{{ID: "m", Name: "api"}},
		SourceSets: []bc.SourceSet{{ID: "m:test", ModuleID: "m", Name: "test", Kind: bc.SourceSetTest}},
		MissingInputs: []bc.MissingInput{
			{ID: "g1", Requested: bc.GapCompiledOutput, Reason: "Maven could not compile it.", ModuleID: "m", SourceSetID: "m:test"},
			{ID: "g2", Requested: "classpath:test", Reason: "The repository is unreachable.", ModuleID: "m", SourceSetID: "m:test"},
		},
	}}
	if got := gapTexts(buildWarnings(build, false), nil); len(got) != 0 {
		t.Fatalf("warnings about tests left out: %q", got)
	}
	if got := gapTexts(buildWarnings(build, true), nil); len(got) != 2 {
		t.Fatalf("warnings = %q", got)
	}
}
