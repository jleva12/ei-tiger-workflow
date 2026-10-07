package syntax

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func TestLanguageProfilesWithoutJDKAndOwnedSettings(t *testing.T) {
	r := bc.Request{Checkout: bc.Checkout{Path: t.TempDir(), RepositoryID: "repo", SnapshotID: "snapshot"}, Limits: bc.DefaultLimits()}
	profile := Profile{Language: "python", Version: "3.12", Roots: []string{"."}, Settings: map[string]string{"mode": "strict"}}
	p, err := NewProfiles(profile)
	if err != nil {
		t.Fatal(err)
	}
	profile.Roots[0], profile.Settings["mode"] = "mutated", "mutated"
	c, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Inventory.JDKs) != 0 || c.Inventory.SourceSets[0].LanguageOptions["mode"] != "strict" || c.Status != bc.Incomplete {
		t.Fatal("invented Java toolchain or aliased settings", c)
	}
	c.Inventory.SourceSets[0].LanguageOptions["mode"] = "mutated result"
	again, err := p.Build(context.Background(), r)
	if err != nil || again.Inventory.SourceSets[0].LanguageOptions["mode"] != "strict" {
		t.Fatal("result aliases provider", err)
	}
	other, _ := NewProfiles(Profile{Language: "python", Version: "3.13", Roots: []string{"."}, Settings: map[string]string{"mode": "strict"}})
	d, err := other.Build(context.Background(), r)
	if err != nil || again.ID == d.ID {
		t.Fatal("language version omitted from identity", err)
	}
	other, _ = NewProfiles(Profile{Language: "python", Version: "3.12", Roots: []string{"."}, Settings: map[string]string{"mode": "loose"}})
	d, err = other.Build(context.Background(), r)
	if err != nil || again.ID == d.ID {
		t.Fatal("options omitted from identity", err)
	}
}

func TestLanguageProfileValidationAndRootChecks(t *testing.T) {
	for _, profiles := range [][]Profile{
		nil,
		{{Language: "python", Roots: []string{"."}}},
		{{Language: "python", Version: "3.12"}},
		{{Language: "python", Version: "3.12", Roots: []string{"../escape"}}},
		{{Language: "python", Version: "3.12", Roots: []string{"."}, EnablePreview: true}},
		{{Language: "python", Version: "3.12", Roots: []string{"."}}, {Language: "python", Version: "3.13", Roots: []string{"."}}},
	} {
		if _, err := NewProfiles(profiles...); err == nil {
			t.Fatal("accepted invalid profiles", profiles)
		}
	}
	r := bc.Request{Checkout: bc.Checkout{Path: t.TempDir(), RepositoryID: "repo", SnapshotID: "snapshot"}, Limits: bc.DefaultLimits()}
	if err := os.Symlink(t.TempDir(), filepath.Join(r.Checkout.Path, "link")); err != nil {
		t.Fatal(err)
	}
	p, err := NewProfiles(Profile{Language: "python", Version: "3.12", Roots: []string{"missing", "link/child"}})
	if err != nil {
		t.Fatal(err)
	}
	c, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if c.Checks[0].Status != bc.Missing || c.Checks[1].Status != bc.Unsupported {
		t.Fatalf("incorrect source root checks: %+v", c.Checks)
	}
}
