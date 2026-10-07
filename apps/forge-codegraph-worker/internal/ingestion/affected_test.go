package ingestion

import (
	"context"
	"fmt"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/repository/github"
)

type fakeReader struct {
	records  map[string][]graph.Version  // lineage → versions
	incoming map[string][]graph.Neighbor // node id → incoming
	paths    map[string][]string         // path → lineages
}

func (f *fakeReader) RecordsByLineage(_ context.Context, _, lineage string, _ uint64) ([]graph.Version, error) {
	return f.records[lineage], nil
}
func (f *fakeReader) Neighbors(_ context.Context, q graph.NeighborQuery) (graph.NeighborPage, error) {
	return graph.NeighborPage{Neighbors: f.incoming[q.NodeID]}, nil
}
func (f *fakeReader) LineagesByPath(_ context.Context, _, path string, _ uint64) ([]string, error) {
	return f.paths[path], nil
}

func input(repo, module, set, path string) semantic.SourceInput {
	return semantic.SourceInput{Source: ir.Source{Path: path, ModuleID: module, SourceSetID: set, FileID: ir.FileID(path + "@" + set)}, Lineage: graph.Lineage(repo, module, set, path)}
}

func TestComputeChangeSetInitialRunIsFull(t *testing.T) {
	inv := []semantic.SourceInput{input("r", "m", "m:main", "A.java"), input("r", "m", "m:test", "ATest.java")}
	cs, err := computeChangeSet(context.Background(), "r", 0, inv, bc.BuildContext{}, nil, &fakeReader{}, changeScope{})
	if err != nil || !cs.full || len(cs.files) != 2 || len(cs.contexts) != 2 {
		t.Fatalf("full run expected: %+v %v", cs, err)
	}
}

func TestComputeChangeSetExpandsDependentsAndDownstreamContexts(t *testing.T) {
	repo := "r"
	a := input(repo, "core", "core:main", "core/A.java")
	b := input(repo, "core", "core:main", "core/B.java")
	c := input(repo, "app", "app:main", "app/C.java")
	d := input(repo, "other", "other:main", "other/D.java")
	inv := []semantic.SourceInput{a, b, c, d}
	build := bc.BuildContext{Inventory: bc.Inventory{SourceSets: []bc.SourceSet{
		{ID: "core:main", ModuleID: "core"},
		{ID: "app:main", ModuleID: "app", Classpath: []bc.PathEntry{{Kind: bc.EntrySourceSet, RefID: "core:main"}}},
		{ID: "other:main", ModuleID: "other"},
	}}}
	entityA := graph.ID("entity", "a")
	reader := &fakeReader{
		records:  map[string][]graph.Version{a.Lineage: {{Fact: graph.Fact{Node: &graph.Node{ID: entityA, Kind: "class"}}, Lineage: a.Lineage}}},
		incoming: map[string][]graph.Neighbor{entityA: {{Edge: graph.Version{Lineage: d.Lineage, Fact: graph.Fact{Edge: &graph.Edge{ID: graph.ID("edge", "x"), Kind: graph.EdgeCalls}}}}}},
	}
	cs, err := computeChangeSet(context.Background(), repo, 3, inv, build, []github.FileChange{{Path: "core/A.java", Status: github.StatusModified}}, reader, changeScope{})
	if err != nil {
		t.Fatal(err)
	}
	if cs.full {
		t.Fatal("incremental run expected")
	}
	for _, id := range []bc.SourceSetID{"core:main", "app:main", "other:main"} {
		if !cs.contexts[id] {
			t.Fatalf("context %s should be affected: %+v", id, cs.contexts)
		}
	}
	for _, in := range inv {
		if !cs.files[in.Lineage] {
			t.Fatalf("%s should be re-projected (its context is affected)", in.Source.Path)
		}
	}
}

func TestComputeChangeSetRenameAndDelete(t *testing.T) {
	repo := "r"
	newFile := input(repo, "m", "m:main", "src/New.java")
	inv := []semantic.SourceInput{newFile}
	build := bc.BuildContext{Inventory: bc.Inventory{SourceSets: []bc.SourceSet{{ID: "m:main", ModuleID: "m"}, {ID: "m:test", ModuleID: "m"}}}}
	changes := []github.FileChange{{Path: "src/New.java", OldPath: "src/Old.java", Status: github.StatusRenamed}, {Path: "src/Gone.java", Status: github.StatusDeleted}}
	oldLineage := graph.Lineage(repo, "m", "m:main", "src/Old.java")
	goneMain, goneTest := graph.Lineage(repo, "m", "m:main", "src/Gone.java"), graph.Lineage(repo, "m", "m:test", "src/Gone.java")
	reader := &fakeReader{paths: map[string][]string{"src/Old.java": {oldLineage}, "src/Gone.java": {goneMain, goneTest}}}
	cs, err := computeChangeSet(context.Background(), repo, 2, inv, build, changes, reader, changeScope{})
	if err != nil {
		t.Fatal(err)
	}
	if cs.renamed[newFile.Lineage] != oldLineage {
		t.Fatalf("rename alias missing: %+v", cs.renamed)
	}
	wantDeleted := map[string]bool{oldLineage: true, goneMain: true, goneTest: true}
	if len(cs.deleted) != len(wantDeleted) {
		t.Fatalf("deleted lineages: %v", cs.deleted)
	}
	for _, l := range cs.deleted {
		if !wantDeleted[l] {
			t.Fatalf("unexpected deleted lineage %s", l)
		}
	}
	if !cs.files[newFile.Lineage] || !cs.contexts["m:main"] {
		t.Fatalf("renamed file must be affected: %+v", cs)
	}
}

// Regression for the audit's finding 3: build inputs are fingerprinted per
// compilation context, independently of source edits.
func TestContextDigestsTrackCompilerInputs(t *testing.T) {
	base := func() bc.BuildContext {
		return bc.BuildContext{Inventory: bc.Inventory{
			Inputs: []bc.Input{
				{ID: "jdk-home", Kind: bc.InputJDK, SHA256: "aa"},
				{ID: "src-core", Kind: bc.InputSourceRoot},
				{ID: "gen-core", Kind: bc.InputGeneratedRoot, SHA256: "g1"},
				{ID: "lib-1", Kind: bc.InputJAR, SHA256: "11"},
				{ID: "lib-2", Kind: bc.InputJAR, SHA256: "22"},
			},
			JDKs:      []bc.JDK{{ID: "jdk", HomeInputID: "jdk-home", Version: "21"}},
			Artifacts: []bc.Artifact{{ID: "a1", BinaryInputID: "lib-1"}, {ID: "a2", BinaryInputID: "lib-2"}},
			SourceSets: []bc.SourceSet{
				{ID: "core:main", ModuleID: "core", JDKID: "jdk", TargetRelease: 21, SourceRootIDs: []bc.InputID{"src-core"}, GeneratedRootIDs: []bc.InputID{"gen-core"}, Classpath: []bc.PathEntry{{Kind: bc.EntryArtifact, RefID: "a1"}, {Kind: bc.EntryArtifact, RefID: "a2"}}},
				{ID: "app:main", ModuleID: "app", JDKID: "jdk", TargetRelease: 21, Classpath: []bc.PathEntry{{Kind: bc.EntrySourceSet, RefID: "core:main"}, {Kind: bc.EntryArtifact, RefID: "a2"}}},
			},
		}}
	}
	before := contextDigests(base())
	if len(before) != 2 || before["core:main"] == before["app:main"] {
		t.Fatalf("digests: %v", before)
	}
	if again := contextDigests(base()); again["core:main"] != before["core:main"] || again["app:main"] != before["app:main"] {
		t.Fatal("digests must be deterministic")
	}
	// A new version of a dependency changes only the contexts that see it.
	b := base()
	b.Inventory.Inputs[3].SHA256 = "11-upgraded"
	after := contextDigests(b)
	if after["core:main"] == before["core:main"] || after["app:main"] != before["app:main"] {
		t.Fatalf("artifact change must invalidate core only: %v vs %v", before, after)
	}
	// A regenerated root changes its context.
	b = base()
	b.Inventory.Inputs[2].SHA256 = "g2"
	if after = contextDigests(b); after["core:main"] == before["core:main"] {
		t.Fatal("generated root change must invalidate the context")
	}
	// Compiler settings change every context that uses them.
	b = base()
	b.Inventory.SourceSets[0].TargetRelease, b.Inventory.SourceSets[1].TargetRelease = 17, 17
	if after = contextDigests(b); after["core:main"] == before["core:main"] || after["app:main"] == before["app:main"] {
		t.Fatal("release change must invalidate both contexts")
	}
	// Classpath order matters to javac, so it matters here.
	b = base()
	b.Inventory.SourceSets[0].Classpath[0], b.Inventory.SourceSets[0].Classpath[1] = b.Inventory.SourceSets[0].Classpath[1], b.Inventory.SourceSets[0].Classpath[0]
	if after = contextDigests(b); after["core:main"] == before["core:main"] {
		t.Fatal("classpath order change must invalidate the context")
	}
	// A source root is not an input digest: file content hashes cover it.
	b = base()
	b.Inventory.Inputs[1].SHA256 = "changed-source-root"
	if after = contextDigests(b); after["core:main"] != before["core:main"] {
		t.Fatal("source root fingerprints must not enter the digest")
	}
	// A dependency that becomes unavailable changes the context too.
	b = base()
	b.Inventory.Inputs[4] = bc.Input{ID: "lib-2", Kind: bc.InputJAR, UnavailableReason: "not downloaded"}
	if after = contextDigests(b); after["app:main"] == before["app:main"] || after["core:main"] == before["core:main"] {
		t.Fatal("an unavailable dependency must invalidate its consumers")
	}
}

// A POM-only commit changes no source file; the invalidated contexts and
// their downstream consumers must still be recomputed.
func TestComputeChangeSetInvalidatedContexts(t *testing.T) {
	repo := "r"
	a := input(repo, "core", "core:main", "core/A.java")
	c := input(repo, "app", "app:main", "app/C.java")
	d := input(repo, "other", "other:main", "other/D.java")
	inv := []semantic.SourceInput{a, c, d}
	build := bc.BuildContext{Inventory: bc.Inventory{SourceSets: []bc.SourceSet{
		{ID: "core:main", ModuleID: "core"},
		{ID: "app:main", ModuleID: "app", Classpath: []bc.PathEntry{{Kind: bc.EntrySourceSet, RefID: "core:main"}}},
		{ID: "other:main", ModuleID: "other"},
	}}}
	cs, err := computeChangeSet(context.Background(), repo, 5, inv, build, nil, &fakeReader{}, changeScope{invalidated: map[bc.SourceSetID]bool{"core:main": true}})
	if err != nil {
		t.Fatal(err)
	}
	if cs.full || !cs.contexts["core:main"] || !cs.contexts["app:main"] || cs.contexts["other:main"] {
		t.Fatalf("invalidated context and its consumer expected: %+v", cs.contexts)
	}
	if !cs.files[a.Lineage] || !cs.files[c.Lineage] || cs.files[d.Lineage] {
		t.Fatalf("files of affected contexts expected: %+v", cs.files)
	}
	// Nothing invalidated and nothing changed is an empty change set.
	cs, err = computeChangeSet(context.Background(), repo, 5, inv, build, nil, &fakeReader{}, changeScope{})
	if err != nil || cs.full || len(cs.files) != 0 || len(cs.contexts) != 0 {
		t.Fatalf("empty change set expected: %+v %v", cs, err)
	}
}

// A forced full recomputation (refresh, configuration change, unknown
// baseline inputs) still honours git's deletions and renames.
func TestComputeChangeSetForcedFullKeepsDeletions(t *testing.T) {
	repo := "r"
	kept := input(repo, "m", "m:main", "src/Kept.java")
	moved := input(repo, "m", "m:main", "src/New.java")
	inv := []semantic.SourceInput{kept, moved}
	build := bc.BuildContext{Inventory: bc.Inventory{SourceSets: []bc.SourceSet{{ID: "m:main", ModuleID: "m"}}}}
	oldLineage := graph.Lineage(repo, "m", "m:main", "src/Old.java")
	gone := graph.Lineage(repo, "m", "m:main", "src/Gone.java")
	reader := &fakeReader{paths: map[string][]string{"src/Old.java": {oldLineage}, "src/Gone.java": {gone}}}
	changes := []github.FileChange{{Path: "src/New.java", OldPath: "src/Old.java", Status: github.StatusRenamed}, {Path: "src/Gone.java", Status: github.StatusDeleted}}
	cs, err := computeChangeSet(context.Background(), repo, 4, inv, build, changes, reader, changeScope{full: true})
	if err != nil {
		t.Fatal(err)
	}
	if !cs.full || !cs.files[kept.Lineage] || !cs.files[moved.Lineage] || !cs.contexts["m:main"] {
		t.Fatalf("every file must be affected: %+v", cs)
	}
	if len(cs.deleted) != 2 || cs.renamed[moved.Lineage] != oldLineage {
		t.Fatalf("deletions and renames must survive a full scope: deleted %v renamed %v", cs.deleted, cs.renamed)
	}
}

type pagedFiles struct {
	pages [][]string
	calls int
}

func (f *pagedFiles) ListNodes(_ context.Context, q graph.ListQuery) (graph.NodePage, error) {
	if q.Kind != graph.NodeSourceFile || q.Generation != 4 {
		return graph.NodePage{}, fmt.Errorf("query %+v", q)
	}
	page := graph.NodePage{Generation: 4}
	for _, id := range f.pages[f.calls] {
		page.Nodes = append(page.Nodes, graph.Version{Fact: graph.Fact{Node: &graph.Node{ID: id, Kind: graph.NodeSourceFile}}})
	}
	f.calls++
	if f.calls < len(f.pages) {
		page.NextCursor = fmt.Sprint(f.calls)
	}
	return page, nil
}

// A full recomputation retires every baseline file the inventory lacks,
// once, whatever the diff listed: a file deleted when no diff was possible,
// or a lineage a changed build layout left behind.
func TestFullRecomputeRetiresFilesTheCommitNoLongerHas(t *testing.T) {
	inventory := []semantic.SourceInput{{Lineage: "keep-1"}, {Lineage: "keep-2"}}
	cs := changeSet{deleted: []string{"listed"}, renamed: map[string]string{"keep-2": "renamed-from"}}
	store := &pagedFiles{pages: [][]string{{"keep-1", "listed", "gone-a"}, {"renamed-from", "gone-b", "keep-2"}}}
	if err := retireMissingFiles(context.Background(), store, "repo", 4, inventory, &cs); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(cs.deleted, ","); got != "gone-a,gone-b,listed" || store.calls != 2 {
		t.Fatalf("deleted = %s after %d pages", got, store.calls)
	}
}
