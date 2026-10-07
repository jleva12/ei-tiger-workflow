package buildcontext_test

import (
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func fixture() bc.Inventory {
	return bc.Inventory{
		Inputs: []bc.Input{
			{ID: "src", Kind: bc.InputSourceRoot, Location: &bc.Location{Root: "checkout", Path: "src/main/java"}},
			{ID: "test", Kind: bc.InputSourceRoot, Location: &bc.Location{Root: "checkout", Path: "src/test/java"}},
			{ID: "jdk", Kind: bc.InputJDK, Location: &bc.Location{Root: "toolchains", Path: "jdk21"}, SHA256: strings.Repeat("a", 64)},
			{ID: "v1", Kind: bc.InputJAR, Location: &bc.Location{Root: "dependencies", Path: "library-1.jar"}, SHA256: strings.Repeat("b", 64)},
			{ID: "v2", Kind: bc.InputJAR, Location: &bc.Location{Root: "dependencies", Path: "library-2.jar"}, SHA256: strings.Repeat("c", 64)},
		},
		JDKs:    []bc.JDK{{ID: "jdk21", HomeInputID: "jdk", Vendor: "Example", Version: "21.0.6", Major: 21}},
		Modules: []bc.Module{{ID: "app", Name: "app", Directory: "."}},
		SourceSets: []bc.SourceSet{
			{ID: "main", ModuleID: "app", Name: "main", Kind: bc.SourceSetMain, JDKID: "jdk21", TargetRelease: 17, SourceRootIDs: []bc.InputID{"src"}, Classpath: []bc.PathEntry{{Kind: bc.EntryArtifact, RefID: "lib1"}, {Kind: bc.EntryArtifact, RefID: "lib2"}}},
			{ID: "test", ModuleID: "app", Name: "test", Kind: bc.SourceSetTest, JDKID: "jdk21", TargetRelease: 17, SourceRootIDs: []bc.InputID{"test"}, Classpath: []bc.PathEntry{{Kind: bc.EntrySourceSet, RefID: "main"}, {Kind: bc.EntryArtifact, RefID: "lib2"}}},
		},
		Artifacts: []bc.Artifact{
			{ID: "lib1", Coordinates: bc.Coordinates{Group: "example", Name: "library", Version: "1.0", Extension: "jar"}, BinaryInputID: "v1"},
			{ID: "lib2", Coordinates: bc.Coordinates{Group: "example", Name: "library", Version: "2.0", Extension: "jar"}, BinaryInputID: "v2"},
		},
	}
}
func seal(t *testing.T, in bc.Inventory) bc.BuildContext {
	t.Helper()
	digest, err := in.Digest()
	if err != nil {
		t.Fatal(err)
	}
	var checks []bc.InputCheck
	for _, x := range in.Inputs {
		checks = append(checks, bc.InputCheck{InputID: x.ID, Status: bc.Available, ObservedSHA256: x.SHA256})
	}
	c, err := bc.Seal(bc.BuildContext{RepositoryID: "repo", SnapshotID: "commit", Producer: bc.Producer{Name: "test", Version: "1", InputSHA256: digest}, Inventory: in, Checks: checks})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

// M18/M20: catalog order is irrelevant; compiler path order and artifact
// versions are part of identity, and test visibility does not leak into main.
func TestIdentityOrderingAndSourceSetIsolation(t *testing.T) {
	in := fixture()
	a := seal(t, in)
	in.Inputs[0], in.Inputs[4] = in.Inputs[4], in.Inputs[0]
	in.Artifacts[0], in.Artifacts[1] = in.Artifacts[1], in.Artifacts[0]
	in.SourceSets[0], in.SourceSets[1] = in.SourceSets[1], in.SourceSets[0]
	b := seal(t, in)
	if !reflect.DeepEqual(a, b) {
		t.Fatal("catalog ordering changed identity/output")
	}
	in = fixture()
	in.SourceSets[0].Classpath[0], in.SourceSets[0].Classpath[1] = in.SourceSets[0].Classpath[1], in.SourceSets[0].Classpath[0]
	if seal(t, in).ID == a.ID {
		t.Fatal("classpath order missing from identity")
	}
	in = fixture()
	in.SourceSets[0].ModulePath = []bc.PathEntry{{Kind: bc.EntryArtifact, RefID: "lib1"}, {Kind: bc.EntryArtifact, RefID: "lib2"}}
	before := seal(t, in)
	in.SourceSets[0].ModulePath[0], in.SourceSets[0].ModulePath[1] = in.SourceSets[0].ModulePath[1], in.SourceSets[0].ModulePath[0]
	if seal(t, in).ID == before.ID {
		t.Fatal("module-path order missing from identity")
	}
	if len(a.Inventory.Artifacts) != 2 || a.Inventory.Artifacts[0].Coordinates.Version != "1.0" || a.Inventory.Artifacts[1].Coordinates.Version != "2.0" {
		t.Fatal("artifact versions collapsed")
	}
	if len(a.Inventory.SourceSets[0].Classpath) != 2 || a.Inventory.SourceSets[0].Classpath[0].Kind != bc.EntryArtifact || a.Inventory.SourceSets[1].Classpath[0].RefID != "main" {
		t.Fatal("source-set environments changed")
	}
	for _, mutate := range []func(*bc.BuildContext){func(c *bc.BuildContext) { c.SnapshotID = "other" }, func(c *bc.BuildContext) { c.RepositoryID = "other" }, func(c *bc.BuildContext) { c.Producer.Version = "2" }, func(c *bc.BuildContext) { c.Inventory.SourceSets[0].TargetRelease = 11 }} {
		c := a
		mutate(&c)
		next, err := bc.Seal(c)
		if err != nil {
			t.Fatal(err)
		}
		if next.ID == a.ID {
			t.Fatal("context identity omitted semantic input")
		}
	}
}

func TestOwnedRoundTripAndCorruption(t *testing.T) {
	in := fixture()
	c := seal(t, in)
	data, err := json.Marshal(c)
	if err != nil {
		t.Fatal(err)
	}
	in.Inputs[0].Location.Path = "changed"
	in.SourceSets[0].Classpath[0].RefID = "changed"
	owned, _ := json.Marshal(c)
	if string(data) != string(owned) {
		t.Fatal("result aliases input")
	}
	var round bc.BuildContext
	if err := json.Unmarshal(data, &round); err != nil {
		t.Fatal(err)
	}
	if err := round.Validate(); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name   string
		mutate func(*bc.BuildContext)
	}{
		{"version", func(c *bc.BuildContext) { c.SchemaVersion = "2" }},
		{"id", func(c *bc.BuildContext) { c.ID = "wrong" }},
		{"status", func(c *bc.BuildContext) { c.Status = bc.Incomplete }},
		{"check_missing", func(c *bc.BuildContext) { c.Checks = c.Checks[1:] }},
		{"check_duplicate", func(c *bc.BuildContext) { c.Checks = append(c.Checks, c.Checks[0]) }},
		{"false_available", func(c *bc.BuildContext) {
			for i := range c.Checks {
				if c.Checks[i].ObservedSHA256 != "" {
					c.Checks[i].ObservedSHA256 = strings.Repeat("f", 64)
					return
				}
			}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var copy bc.BuildContext
			json.Unmarshal(data, &copy)
			tc.mutate(&copy)
			if err := copy.Validate(); err == nil {
				t.Fatal("corrupt context accepted")
			}
		})
	}
}

// M18/M21: an unresolved artifact remains represented; complete cannot conceal
// missing bytes or a missing dependency version.
func TestIncompleteObservations(t *testing.T) {
	c := seal(t, fixture())
	c.Inventory.Inputs[0].Location = nil
	c.Inventory.Inputs[0].UnavailableReason = "toolchain not supplied"
	c.Checks[0] = bc.InputCheck{InputID: c.Inventory.Inputs[0].ID, Status: bc.Missing}
	c.Inventory.MissingInputs = []bc.MissingInput{{ID: "unknown", Requested: "example:other:${version}", Reason: "version unavailable", SourceSetID: "test"}}
	c.Inventory.SourceSets[1].Classpath = append(c.Inventory.SourceSets[1].Classpath, bc.PathEntry{Kind: bc.EntryMissing, RefID: "unknown"})
	got, err := bc.Seal(c)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != bc.Incomplete || len(got.Diagnostics) != 2 || len(got.Inventory.Inputs) != len(c.Inventory.Inputs) || got.Diagnostics[1].GapID != "unknown" {
		t.Fatalf("missing facts hidden: %+v", got)
	}
	if err := got.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestRejectInvalidInventory(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*bc.Inventory)
		want   string
	}{
		{"duplicate_id", func(i *bc.Inventory) { i.Inputs = append(i.Inputs, i.Inputs[0]) }, "duplicate ID"},
		{"unknown_artifact", func(i *bc.Inventory) { i.SourceSets[0].Classpath[0].RefID = "absent" }, "classpath"},
		{"unknown_jdk", func(i *bc.Inventory) { i.SourceSets[0].JDKID = "absent" }, "jdk_id"},
		{"newer_release", func(i *bc.Inventory) { i.SourceSets[0].TargetRelease = 24 }, "target_release"},
		{"preview_mismatch", func(i *bc.Inventory) { i.SourceSets[0].EnablePreview = true }, "enable_preview"},
		{"absolute_path", func(i *bc.Inventory) { i.Inputs[0].Location.Path = "/tmp/source" }, "location.path"},
		{"parent_path", func(i *bc.Inventory) { i.Inputs[0].Location.Path = "../source" }, "location.path"},
		{"source_outside_checkout", func(i *bc.Inventory) { i.Inputs[0].Location.Root = "elsewhere" }, "ordinary source roots"},
		{"digest_required", func(i *bc.Inventory) { i.Inputs[3].SHA256 = "" }, "sha256"},
		{"competing_availability", func(i *bc.Inventory) { i.Inputs[3].UnavailableReason = "missing" }, "exactly one"},
		{"wrong_role", func(i *bc.Inventory) { i.JDKs[0].HomeInputID = "v1" }, "input kind"},
		{"cycle", func(i *bc.Inventory) {
			i.SourceSets[0].Classpath = append(i.SourceSets[0].Classpath, bc.PathEntry{Kind: bc.EntrySourceSet, RefID: "test"})
		}, "cyclic"},
		{"missing_version", func(i *bc.Inventory) { i.Artifacts[0].Coordinates.Version = "" }, "coordinates.version"},
		{"invalid_source_mapping", func(i *bc.Inventory) {
			i.Artifacts[0].Origin = &bc.SourceMapping{RepositoryID: "repo", SnapshotID: "revision", ModuleID: "\xff"}
		}, "origin.module_id"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			in := fixture()
			tc.mutate(&in)
			err := in.Validate()
			var detail bc.ValidationError
			if !errors.Is(err, bc.ErrInvalidInput) || !strings.Contains(err.Error(), tc.want) || !errors.As(err, &detail) {
				t.Fatalf("got %v, want inspectable %q error", err, tc.want)
			}
		})
	}
}
