package maven

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func TestAddedRootsAreRelativeToTheirDeclaringModule(t *testing.T) {
	for _, name := range []string{".", "target/optional/generated", "../../escape", "${unresolved}"} {
		t.Run(name, func(t *testing.T) {
			p, r, model := observedFixture(t)
			body, err := os.ReadFile(model)
			if err != nil {
				t.Fatal(err)
			}
			absolute := filepath.Join(r.Checkout.Path, "app", name)
			plugin := `<plugin><artifactId>build-helper-maven-plugin</artifactId><executions><execution><goals><goal>add-source</goal></goals><configuration><sources><source>` + name + `</source><source>` + absolute + `</source></sources></configuration></execution></executions></plugin>`
			if err = os.WriteFile(model, []byte(strings.Replace(string(body), "</plugins>", plugin+"</plugins>", 1)), 0600); err != nil {
				t.Fatal(err)
			}
			err = p.materializeDeclaredRoots(context.Background(), r, model)
			if name == "../../escape" || name == "${unresolved}" {
				if err == nil {
					t.Fatal("invalid module-relative root accepted")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			build, err := p.BuildObserved(context.Background(), r, model)
			if err != nil {
				t.Fatal(err)
			}
			want := filepath.ToSlash(filepath.Join("app", name))
			kind := bc.InputGeneratedRoot
			if name == "." {
				kind = bc.InputSourceRoot
			}
			matches := 0
			for _, input := range build.Inventory.Inputs {
				if input.Kind == kind && input.Location != nil && input.Location.Path == want {
					matches++
				}
			}
			if matches != 1 {
				t.Fatalf("expected one canonical module root %s, found %d", want, matches)
			}
			if _, err = os.Stat(absolute); err != nil {
				t.Fatal("declared directory missing", err)
			}
		})
	}
}

func TestAddedModuleRootExcludesBuildBookkeepingFromInputFingerprint(t *testing.T) {
	for _, goal := range []string{"add-source", "add-test-source"} {
		t.Run(goal, func(t *testing.T) {
			p, request, model := observedFixture(t)
			body, err := os.ReadFile(model)
			if err != nil {
				t.Fatal(err)
			}
			plugin := `<plugin><artifactId>build-helper-maven-plugin</artifactId><executions><execution><goals><goal>` + goal + `</goal></goals><configuration><sources><source>.</source><source>target/generated-fixture</source></sources></configuration></execution></executions></plugin>`
			if err = os.WriteFile(model, []byte(strings.Replace(string(body), "</plugins>", plugin+"</plugins>", 1)), 0600); err != nil {
				t.Fatal(err)
			}
			if err = p.materializeDeclaredRoots(context.Background(), request, model); err != nil {
				t.Fatal(err)
			}
			observe := func() bc.BuildContext {
				t.Helper()
				build, err := p.BuildObserved(context.Background(), request, model)
				if err != nil || build.Status != bc.Complete {
					t.Fatalf("observation failed: %v, %v", build.Diagnostics, err)
				}
				return build
			}
			before := observe()
			bookkeeping := filepath.Join(request.Checkout.Path, "app/target/compiler-input-paths.txt")
			if err = os.WriteFile(bookkeeping, []byte("/different/owned-checkout/A.java"), 0600); err != nil {
				t.Fatal(err)
			}
			if after := observe(); before.ID != after.ID {
				t.Fatal("non-input Maven bookkeeping changed the build context")
			}
			generated := filepath.Join(request.Checkout.Path, "app/target/generated-fixture/Generated.java")
			if err = os.WriteFile(generated, []byte("class Generated {}"), 0600); err != nil {
				t.Fatal(err)
			}
			afterGenerated := observe()
			if before.ID == afterGenerated.ID {
				t.Fatal("generated source changes lost their input fingerprint")
			}
			if err = os.WriteFile(filepath.Join(request.Checkout.Path, "app/target/classes/A.class"), []byte("changed binary"), 0600); err != nil {
				t.Fatal(err)
			}
			if after := observe(); after.ID == afterGenerated.ID {
				t.Fatal("compiled output changes lost their input fingerprint")
			}
		})
	}
}
