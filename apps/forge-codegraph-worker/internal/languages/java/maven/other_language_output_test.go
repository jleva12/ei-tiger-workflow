package maven

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func TestJavaTestsRetainMainOutputWithoutJavaSources(t *testing.T) {
	p, request, model := observedFixture(t)
	if err := os.RemoveAll(filepath.Join(request.Checkout.Path, "app/src/main/java")); err != nil {
		t.Fatal(err)
	}
	build, err := p.BuildObserved(context.Background(), request, model)
	if err != nil {
		t.Fatal(err)
	}
	if build.Status != bc.Complete || len(build.Inventory.SourceSets) != 3 {
		t.Fatalf("unexpected context: %+v", build)
	}
	var app bc.ModuleID
	for _, module := range build.Inventory.Modules {
		if module.Name == "app" {
			app = module.ID
		}
	}
	for _, set := range build.Inventory.SourceSets {
		if set.ModuleID != app {
			continue
		}
		if set.Kind != bc.SourceSetTest || len(set.Classpath) != 3 || set.Classpath[0].Kind != bc.EntryArtifact {
			t.Fatalf("main class output missing or misordered: %+v", set)
		}
		for _, artifact := range build.Inventory.Artifacts {
			if string(artifact.ID) != set.Classpath[0].RefID {
				continue
			}
			if artifact.Coordinates.Name != "app" {
				t.Fatalf("wrong main output provenance: %+v", artifact)
			}
			for _, input := range build.Inventory.Inputs {
				if input.ID != artifact.BinaryInputID {
					continue
				}
				if input.Kind != bc.InputClasses || input.Location == nil || input.Location.Root != "checkout" || input.Location.Path != "app/target/classes" || input.SHA256 == "" {
					t.Fatalf("main output not fingerprinted: %+v", input)
				}
				return
			}
		}
	}
	t.Fatal("main output artifact absent")
}
