package maven

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDeclaredTestHelpersMatchExactReactorArtifacts(t *testing.T) {
	for _, test := range []struct {
		name, dependency string
		want             bool
	}{
		{"default_test_jar", "<version>1</version><type>test-jar</type>", true},
		{"tests_classifier", "<version>1</version><classifier>tests</classifier>", true},
		{"different_version", "<version>2</version><type>test-jar</type>", false},
		{"main_jar", "<version>1</version>", false},
		{"custom_classifier", "<version>1</version><type>test-jar</type><classifier>custom</classifier>", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			p, r, model := observedFixture(t)
			addTestDependency(t, model, test.dependency)
			got, err := p.declaredTestProjects(context.Background(), r, model)
			if err != nil {
				t.Fatal(err)
			}
			if test.want && (len(got) != 1 || got[0] != "example:lib") || !test.want && len(got) != 0 {
				t.Fatalf("unexpected producers: %v", got)
			}
		})
	}
}

func addTestDependency(t *testing.T, model, fields string) {
	t.Helper()
	body, err := os.ReadFile(model)
	if err != nil {
		t.Fatal(err)
	}
	dependency := "<dependencies><dependency><groupId>example</groupId><artifactId>lib</artifactId>" + fields + "</dependency></dependencies>"
	body = []byte(strings.Replace(string(body), "<build>", dependency+"<build>", 1))
	if err = os.WriteFile(model, body, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestColdPreparationCreatesConsumedTestJarBeforeMainInstall(t *testing.T) {
	p, r, model := observedFixture(t)
	addTestDependency(t, model, "<version>1</version><type>test-jar</type><scope>test</scope>")
	jar := filepath.Join(p.config.CacheDir, "repository/example/lib/1/lib-1-tests.jar")
	output := filepath.Join(r.Checkout.Path, "lib/target/test-classes")
	if err := os.WriteFile(filepath.Join(r.Checkout.Path, "app/target/codegraph-test-classpath.txt"), []byte(jar), 0600); err != nil {
		t.Fatal(err)
	}
	quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }
	// Model a reactor whose full install cannot resolve a consumed tests JAR
	// until its producer has compiled and installed that classifier. Enabling
	// test compilation for the whole reactor would also fail this fixture.
	script := `#!/bin/sh
set -eu
compile=false
selected=
previous=
goal=
skip=false
also_make=false
for arg in "$@"; do
  if test "$previous" = '-pl'; then selected="$arg"; fi
  case "$arg" in
    -Doutput=*) cp ` + quote(model) + ` "${arg#-Doutput=}" ;;
    -Dmaven.test.skip=false) compile=true ;;
    -DskipTests) skip=true ;;
    -am) also_make=true ;;
    install|test-compile) goal="$arg" ;;
    test|verify|deploy) exit 90 ;;
  esac
  previous="$arg"
done
test "$skip" = true
if test "$compile" = true; then
  test "$selected" = 'example:lib'
  mkdir -p ` + quote(output) + `
  printf helper > ` + quote(filepath.Join(output, "Helper.class")) + `
  if test "$goal" = install; then
    test "$also_make" = true
    printf 'compiled helper jar' > ` + quote(jar) + `
  fi
elif test "$goal" = install; then
  test -f ` + quote(jar) + ` || exit 91
fi
`
	if err := os.WriteFile(p.config.MavenExecutable, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	build, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if len(build.Inventory.MissingInputs) != 0 {
		t.Fatalf("test helper inputs missing: %+v", build.Diagnostics)
	}
}
