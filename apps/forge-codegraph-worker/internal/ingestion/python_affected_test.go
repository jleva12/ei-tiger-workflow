package ingestion

import (
	"context"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/languages/python/environment"
	"ei-aitiger-codegraph/worker/internal/repository/github"
)

func TestPythonEnvironmentChangesInvalidateContext(t *testing.T) {
	settings := environment.Settings{Version: "3.12", Platform: "Linux", Roots: []string{".", "src"}}
	digest := func(s environment.Settings) string {
		t.Helper()
		options, err := environment.Encode(s)
		if err != nil {
			t.Fatal(err)
		}
		b := bc.BuildContext{Inventory: bc.Inventory{SourceSets: []bc.SourceSet{{ID: "python", Language: "python", LanguageVersion: s.Version, LanguageOptions: options}}}}
		return contextDigests(b)["python"]
	}
	before := digest(settings)
	for _, change := range []func(*environment.Settings){
		func(s *environment.Settings) { s.Version = "3.13" },
		func(s *environment.Settings) { s.VersionSource = ".python-version" },
		func(s *environment.Settings) { s.VersionRequest = "3.12.9" },
		func(s *environment.Settings) { s.RequiresPython = ">=3.12.9,<3.13" },
		func(s *environment.Settings) { s.Platform = "Windows" },
		func(s *environment.Settings) { s.Roots = []string{"src", "."} },
		func(s *environment.Settings) { s.ConfigDigest = "lockfile-change" },
		func(s *environment.Settings) { s.AnalyzerDigest = "analyzer-change" },
	} {
		updated := settings
		change(&updated)
		if digest(updated) == before {
			t.Fatal("Python environment change did not invalidate")
		}
	}
}

func TestPythonAddedModuleRechecksExistingUnresolvedImports(t *testing.T) {
	app := input("repo", "python", "python", "app.py")
	added := input("repo", "python", "python", "added.py")
	build := bc.BuildContext{Inventory: bc.Inventory{SourceSets: []bc.SourceSet{{ID: "python", ModuleID: "python", Language: "python", LanguageVersion: "3.12"}}}}
	cs, err := computeChangeSet(context.Background(), "repo", 1, []semantic.SourceInput{app, added}, build,
		[]github.FileChange{{Path: "added.py", Status: github.StatusAdded}}, &fakeReader{}, changeScope{})
	if err != nil {
		t.Fatal(err)
	}
	if !cs.files[app.Lineage] || !cs.files[added.Lineage] {
		t.Fatal("adding a module must revisit prior negative imports")
	}
}
