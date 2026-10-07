package maven

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func observedFixture(t *testing.T) (*Provider, bc.Request, string) {
	t.Helper()
	root := t.TempDir()
	root, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	jdk := t.TempDir()
	cache := t.TempDir()
	write := func(name, body string) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(name), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(name, []byte(body), 0700); err != nil {
			t.Fatal(err)
		}
	}
	write(filepath.Join(jdk, "release"), "JAVA_VERSION=\"21.0.8\"\nIMPLEMENTOR=\"Fixture\"\n")
	write(filepath.Join(jdk, "bin/java"), "#!/bin/sh\nexit 0\n")
	mvn := filepath.Join(t.TempDir(), "mvn")
	write(mvn, "#!/bin/sh\nexit 0\n")
	p, err := New(Config{JavaHome: jdk, MavenExecutable: mvn, CacheDir: cache, WorkDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	for _, module := range []string{"app", "lib"} {
		write(filepath.Join(root, module, "pom.xml"), "<project><groupId>example</groupId><artifactId>"+module+"</artifactId><version>1</version></project>")
		write(filepath.Join(root, module, "src/main/java/A.java"), "class A {}")
		write(filepath.Join(root, module, "src/test/java/Test.java"), "class Test {}")
		write(filepath.Join(root, module, "target/classes/A.class"), "compiled fixture")
		write(filepath.Join(root, module, "target/codegraph-compile-classpath.txt"), "")
		write(filepath.Join(root, module, "target/codegraph-test-classpath.txt"), "")
	}
	dependency := filepath.Join(cache, "repository/example/lib/1/lib-1.jar")
	write(dependency, "reactor binary replaced with source visibility")
	external := filepath.Join(cache, "repository/external/library/2/library-2.jar")
	write(external, "external jar bytes")
	write(filepath.Join(root, "app/target/codegraph-compile-classpath.txt"), dependency+string(os.PathListSeparator)+external)
	write(filepath.Join(root, "app/target/codegraph-test-classpath.txt"), dependency+string(os.PathListSeparator)+external)
	model := "<projects>"
	for _, module := range []string{"app", "lib"} {
		base := filepath.Join(root, module)
		model += fmt.Sprintf(`<project><groupId>example</groupId><artifactId>%s</artifactId><version>1</version><packaging>jar</packaging><properties><maven.compiler.release>8</maven.compiler.release></properties><build><directory>%s/target</directory><sourceDirectory>%s/src/main/java</sourceDirectory><testSourceDirectory>%s/src/test/java</testSourceDirectory><outputDirectory>%s/target/classes</outputDirectory><testOutputDirectory>%s/target/test-classes</testOutputDirectory><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><configuration><release>8</release><excludes><exclude>**/Excluded.java</exclude></excludes></configuration></plugin></plugins></build></project>`, module, base, base, base, base, base)
	}
	model += "</projects>"
	path := filepath.Join(t.TempDir(), "effective.xml")
	write(path, model)
	return p, bc.Request{Checkout: bc.Checkout{Path: root, RepositoryID: "repo", SnapshotID: strings.Repeat("a", 40)}, Limits: bc.DefaultLimits()}, path
}
func TestObservedMavenKeepsOrderedVisibilityAndMissingInputs(t *testing.T) {
	p, r, model := observedFixture(t)
	ctx := context.Background()
	build, err := p.BuildObserved(ctx, r, model)
	if err != nil {
		t.Fatal(err)
	}
	if build.Status != bc.Complete || len(build.Inventory.SourceSets) != 4 || len(build.Inventory.Artifacts) != 1 {
		t.Fatalf("bad inventory: %+v", build)
	}
	modules := map[bc.ModuleID]string{}
	for _, module := range build.Inventory.Modules {
		modules[module.ID] = module.Name
	}
	for _, set := range build.Inventory.SourceSets {
		if modules[set.ModuleID] == "app" {
			if set.Kind == bc.SourceSetMain {
				if len(set.Classpath) != 2 || set.Classpath[0].Kind != bc.EntrySourceSet || set.Classpath[1].Kind != bc.EntryArtifact {
					t.Fatalf("classpath order: %+v", set.Classpath)
				}
			} else {
				if len(set.Classpath) != 3 || set.Classpath[0].Kind != bc.EntrySourceSet {
					t.Fatalf("test visibility: %+v", set.Classpath)
				}
			}
		}
		exclusions := 1
		if set.Kind == bc.SourceSetTest {
			exclusions = 0
		}
		if len(set.ExcludePatterns) != exclusions || set.TargetRelease != 8 {
			t.Fatalf("effective compiler settings lost: %+v", set)
		}
	}
	if err = os.Remove(filepath.Join(r.Checkout.Path, "app/target/codegraph-test-classpath.txt")); err != nil {
		t.Fatal(err)
	}
	missing, err := p.BuildObserved(ctx, r, model)
	if err != nil {
		t.Fatal(err)
	}
	if missing.Status != bc.Incomplete {
		t.Fatal("missing test dependency observation labeled complete")
	}
}

func TestReactorClassifierWithoutCompiledOutputKeepsPinnedBinary(t *testing.T) {
	p, request, model := observedFixture(t)
	for _, classifier := range []string{"tests", "custom"} {
		jar := filepath.Join(p.config.CacheDir, "repository/example/lib/1/lib-1-"+classifier+".jar")
		if err := os.WriteFile(jar, []byte("actual classifier artifact "+classifier), 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(request.Checkout.Path, "app/target/codegraph-test-classpath.txt"), []byte(jar), 0600); err != nil {
			t.Fatal(err)
		}
		build, err := p.BuildObserved(context.Background(), request, model)
		if err != nil {
			t.Fatal(err)
		}
		if classifier == "tests" && build.Status != bc.Incomplete {
			t.Fatal("uncompiled consumed test helpers labeled complete")
		}
		modules := map[bc.ModuleID]string{}
		for _, m := range build.Inventory.Modules {
			modules[m.ID] = m.Name
		}
		artifacts := map[string]bc.Artifact{}
		for _, a := range build.Inventory.Artifacts {
			artifacts[string(a.ID)] = a
		}
		found := false
		for _, set := range build.Inventory.SourceSets {
			if modules[set.ModuleID] != "app" || set.Kind != bc.SourceSetTest {
				continue
			}
			if len(set.Classpath) != 2 || set.Classpath[1].Kind != bc.EntryArtifact {
				t.Fatalf("invented classifier source visibility: %+v", set.Classpath)
			}
			a := artifacts[set.Classpath[1].RefID]
			if a.Coordinates.Classifier != classifier || a.BinaryInputID == "" {
				t.Fatalf("classifier bytes lost: %+v", a)
			}
			found = true
		}
		if !found {
			t.Fatal("test consumer missing")
		}
	}
}

func TestPreparationCompilesOnlyConsumedReactorTestHelpers(t *testing.T) {
	p, r, model := observedFixture(t)
	jar := filepath.Join(p.config.CacheDir, "repository/example/lib/1/lib-1-tests.jar")
	if err := os.WriteFile(jar, []byte("empty skipped-compile classifier"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(r.Checkout.Path, "app/target/codegraph-test-classpath.txt"), []byte(jar), 0600); err != nil {
		t.Fatal(err)
	}
	projects, err := p.consumedTestProjects(context.Background(), r, model)
	if err != nil {
		t.Fatal(err)
	}
	if len(projects) != 1 || projects[0] != "example:lib" {
		t.Fatalf("incorrect helper scope: %v", projects)
	}
	log := filepath.Join(t.TempDir(), "commands")
	quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }
	output := filepath.Join(r.Checkout.Path, "lib/target/test-classes")
	script := "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$*\" >> " + quote(log) + "\nfor arg in \"$@\"; do\ncase \"$arg\" in\n-Doutput=*) cp " + quote(model) + " \"${arg#-Doutput=}\" ;;\ntest-compile) mkdir -p " + quote(output) + "; printf helper > " + quote(filepath.Join(output, "Helper.class")) + " ;;\nesac\ndone\n"
	if err = os.WriteFile(p.config.MavenExecutable, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	build, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if build.Status != bc.Complete {
		t.Fatalf("prepared helper output incomplete: %+v", build.Diagnostics)
	}
	body, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	compileStages := 0
	for _, line := range strings.Split(strings.TrimSpace(string(body)), "\n") {
		if !strings.Contains(line, "-DskipTests") {
			t.Fatal("test execution not disabled")
		}
		if strings.HasSuffix(line, " test-compile") {
			compileStages++
			if !strings.Contains(line, "-Dmaven.test.skip=false") || strings.Contains(line, "-Dmaven.test.skip=true") || !strings.Contains(line, "-pl example:lib") {
				t.Fatalf("wrong test helper command %s", line)
			}
		}
		for _, arg := range strings.Fields(line) {
			if arg == "test" || arg == "verify" || arg == "deploy" {
				t.Fatal("test execution or remote publication goal invoked")
			}
		}
	}
	if compileStages != 1 {
		t.Fatalf("helper compilation stages %d", compileStages)
	}
}

func TestAnnotationOnlyCompilerExecutionKeepsExcludedSourceVariant(t *testing.T) {
	p, request, model := observedFixture(t)
	body, err := os.ReadFile(model)
	if err != nil {
		t.Fatal(err)
	}
	execution := `<executions><execution><id>metadata</id><phase>process-resources</phase><goals><goal>compile</goal></goals><configuration combine.self="override"><proc>only</proc><source>1.8</source><target>1.8</target><includes><include>**/Excluded.java</include></includes></configuration></execution></executions>`
	text := strings.Replace(string(body), `<maven.compiler.release>8</maven.compiler.release>`, `<maven.compiler.source>1.8</maven.compiler.source><maven.compiler.target>1.8</maven.compiler.target>`, 1)
	text = strings.Replace(text, `<artifactId>maven-compiler-plugin</artifactId>`, `<artifactId>maven-compiler-plugin</artifactId>`+execution, 1)
	if err = os.WriteFile(model, []byte(text), 0600); err != nil {
		t.Fatal(err)
	}
	build, err := p.BuildObserved(context.Background(), request, model)
	if err != nil {
		t.Fatal(err)
	}
	if build.Status != bc.Complete {
		t.Fatalf("%+v", build.Diagnostics)
	}
	var custom *bc.SourceSet
	for i := range build.Inventory.SourceSets {
		set := &build.Inventory.SourceSets[i]
		if set.Kind == bc.SourceSetCustom {
			custom = set
		}
	}
	if custom == nil || custom.TargetRelease != 8 || custom.OutputInputID != "" || len(custom.Classpath) != 2 || !custom.SelectsSource("p/Excluded.java") || custom.SelectsSource("p/Other.java") || custom.LanguageOptions["java.compiler.mode"] != "source-target" || custom.LanguageOptions["java.compiler.proc"] != "only" {
		t.Fatalf("annotation-only context lost: %+v", custom)
	}
	for _, set := range build.Inventory.SourceSets {
		for _, entry := range set.Classpath {
			if entry.Kind == bc.EntrySourceSet && entry.RefID == string(custom.ID) {
				t.Fatal("annotation-only source set exposed as compiled output")
			}
		}
	}
}
func TestObservedMavenGeneratedInputsCannotDisappear(t *testing.T) {
	p, r, model := observedFixture(t)
	body, err := os.ReadFile(model)
	if err != nil {
		t.Fatal(err)
	}
	generated := filepath.Join(r.Checkout.Path, "app/target/generated-sources/java")
	plugin := `<plugin><artifactId>build-helper-maven-plugin</artifactId><executions><execution><goals><goal>add-source</goal></goals><configuration><sources><source>` + generated + `</source></sources></configuration></execution></executions></plugin>`
	modified := strings.Replace(string(body), "</plugins>", plugin+"</plugins>", 1)
	if err = os.WriteFile(model, []byte(modified), 0600); err != nil {
		t.Fatal(err)
	}
	build, err := p.BuildObserved(context.Background(), r, model)
	if err != nil {
		t.Fatal(err)
	}
	if build.Status != bc.Incomplete {
		t.Fatal("absent generated inputs labeled complete")
	}
	if err = os.MkdirAll(generated, 0700); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(generated, "Generated.java"), []byte("class Generated {}"), 0600); err != nil {
		t.Fatal(err)
	}
	build, err = p.BuildObserved(context.Background(), r, model)
	if err != nil {
		t.Fatal(err)
	}
	if build.Status != bc.Complete {
		t.Fatalf("materialized generated input remains incomplete: %+v", build.Diagnostics)
	}
}

func TestEmptyDeclaredRootsRemainInsideOwnedCheckout(t *testing.T) {
	for _, symlink := range []bool{false, true} {
		t.Run(fmt.Sprint(symlink), func(t *testing.T) {
			p, request, model := observedFixture(t)
			generated := filepath.Join(request.Checkout.Path, "app/target/optional/generated")
			body, err := os.ReadFile(model)
			if err != nil {
				t.Fatal(err)
			}
			plugin := `<plugin><artifactId>build-helper-maven-plugin</artifactId><executions><execution><goals><goal>add-source</goal></goals><configuration><sources><source>` + generated + `</source></sources></configuration></execution></executions></plugin>`
			if err = os.WriteFile(model, []byte(strings.Replace(string(body), "</plugins>", plugin+"</plugins>", 1)), 0600); err != nil {
				t.Fatal(err)
			}
			outside := t.TempDir()
			if symlink {
				if err = os.Symlink(outside, filepath.Dir(generated)); err != nil {
					t.Fatal(err)
				}
			}
			err = p.materializeDeclaredRoots(context.Background(), request, model)
			if symlink {
				if err == nil {
					t.Fatal("traversed declared root symlink")
				}
				if _, err = os.Stat(filepath.Join(outside, "generated")); !os.IsNotExist(err) {
					t.Fatal("wrote outside owned checkout")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			entries, err := os.ReadDir(generated)
			if err != nil || len(entries) != 0 {
				t.Fatalf("empty input not retained: %v %v", entries, err)
			}
			build, err := p.BuildObserved(context.Background(), request, model)
			if err != nil {
				t.Fatal(err)
			}
			if build.Status != bc.Complete {
				t.Fatalf("empty observed source input invalid: %+v", build.Diagnostics)
			}
		})
	}
}
func TestMavenModelBudgetsAndBoundary(t *testing.T) {
	p, r, model := observedFixture(t)
	r.Limits.MaxInputBytes = 100
	if _, err := p.BuildObserved(context.Background(), r, model); err == nil {
		t.Fatal("XML project limit ignored")
	}
	r.Limits = bc.DefaultLimits()
	body, err := os.ReadFile(model)
	if err != nil {
		t.Fatal(err)
	}
	body = []byte(strings.Replace(string(body), r.Checkout.Path, "/different-checkout", -1))
	if err = os.WriteFile(model, body, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := p.BuildObserved(context.Background(), r, model); err == nil {
		t.Fatal("foreign checkout model accepted")
	}
}

func TestResolvedPreparationNeverInvokesTestsOrRemotePublication(t *testing.T) {
	p, r, model := observedFixture(t)
	log := filepath.Join(t.TempDir(), "commands")
	quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }
	script := "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$*\" >> " + quote(log) + "\nfor arg in \"$@\"; do\n case \"$arg\" in -Doutput=*) cp " + quote(model) + " \"${arg#-Doutput=}\" ;; esac\ndone\n"
	if err := os.WriteFile(p.config.MavenExecutable, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	build, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if build.Status != bc.Complete {
		t.Fatal(build.Diagnostics)
	}
	body, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	commands := strings.Split(strings.TrimSpace(string(body)), "\n")
	// effective-pom once, install, compile classpath, test classpath.
	if len(commands) != 4 {
		t.Fatalf("commands=%q", commands)
	}
	for i, command := range commands {
		if !strings.Contains(command, "-Dmaven.test.skip=true") || !strings.Contains(command, "-DskipTests") {
			t.Fatalf("test skip absent: %s", command)
		}
		for _, arg := range strings.Fields(command) {
			if arg == "test" || arg == "test-compile" || arg == "deploy" || arg == "deploy:deploy-file" {
				t.Fatalf("unrequested execution %s", arg)
			}
		}
		if i == 1 && !strings.HasSuffix(command, " install") {
			t.Fatal("missing local input preparation")
		}
	}
	scratch, err := os.ReadDir(p.config.WorkDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(scratch) != 0 {
		t.Fatal("intermediate Maven logs/model retained after success")
	}
}
func TestExternalClasspathInputsRetainImmutableCopies(t *testing.T) {
	p, r, model := observedFixture(t)
	build, err := p.BuildObserved(context.Background(), r, model)
	if err != nil {
		t.Fatal(err)
	}
	var saved bc.Input
	for _, input := range build.Inventory.Inputs {
		if input.Kind == bc.InputJAR {
			saved = input
			break
		}
	}
	if saved.Location == nil || !strings.HasPrefix(saved.Location.Path, "objects/") {
		t.Fatal("mutable Maven repository used as compiler input")
	}
	jar := filepath.Join(p.config.CacheDir, "repository/external/library/2/library-2.jar")
	if err = os.WriteFile(jar, []byte("new installation with same coordinates"), 0600); err != nil {
		t.Fatal(err)
	}
	retained, err := os.ReadFile(filepath.Join(p.config.CacheDir, saved.Location.Path))
	if err != nil {
		t.Fatal(err)
	}
	if string(retained) != "external jar bytes" {
		t.Fatal("later Maven install changed pinned compiler input")
	}
}

func TestCheckoutWithoutPOMReportsNoBuild(t *testing.T) {
	p, r, _ := observedFixture(t)
	empty := t.TempDir()
	if err := os.MkdirAll(filepath.Join(empty, "target"), 0o700); err != nil {
		t.Fatal(err)
	}
	// A POM under target is a build output, not a build.
	if err := os.WriteFile(filepath.Join(empty, "target", "pom.xml"), []byte("<project/>"), 0o600); err != nil {
		t.Fatal(err)
	}
	r.Checkout.Path = empty
	if _, err := p.Build(context.Background(), r); !errors.Is(err, bc.ErrNoBuild) {
		t.Fatalf("a checkout without a POM must report no build, got %v", err)
	}
}

// buildJDKFixture gives the fixture's provider build JDKs of the given majors
// and a Maven that logs its JAVA_HOME with each command. Installs exit 1 under
// build JDK fail, or under every JDK when fail is -1. sourceTarget rewrites
// the model from release to source/target 1.8.
func buildJDKFixture(t *testing.T, sourceTarget bool, fail int, majors ...int) (*Provider, bc.Request, map[int]string, string) {
	t.Helper()
	p, r, model := observedFixture(t)
	homes := map[int]string{}
	for _, major := range majors {
		home := t.TempDir()
		version := fmt.Sprintf("%d.0.2", major)
		if major == 8 {
			version = "1.8.0_442"
		}
		for name, body := range map[string]string{"release": "JAVA_VERSION=\"" + version + "\"\n", "bin/java": "#!/bin/sh\nexit 0\n"} {
			if err := os.MkdirAll(filepath.Dir(filepath.Join(home, name)), 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(home, name), []byte(body), 0o700); err != nil {
				t.Fatal(err)
			}
		}
		homes[major] = home
	}
	if sourceTarget {
		body, err := os.ReadFile(model)
		if err != nil {
			t.Fatal(err)
		}
		text := strings.ReplaceAll(string(body), `<maven.compiler.release>8</maven.compiler.release>`, `<maven.compiler.source>1.8</maven.compiler.source><maven.compiler.target>1.8</maven.compiler.target>`)
		text = strings.ReplaceAll(text, `<release>8</release>`, "")
		if err = os.WriteFile(model, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	log := filepath.Join(t.TempDir(), "commands")
	quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }
	failing := "[ \"$JAVA_HOME\" = " + quote(homes[fail]) + " ]"
	if fail == -1 {
		failing = "true"
	}
	script := "#!/bin/sh\nset -eu\nprintf '%s %s\\n' \"$JAVA_HOME\" \"$*\" >> " + quote(log) + "\nfor arg in \"$@\"; do\n case \"$arg\" in -Doutput=*) cp " + quote(model) + " \"${arg#-Doutput=}\" ;;\n install) if " + failing + "; then echo 'Invalid CEN header'; exit 1; fi ;; esac\ndone\n"
	if err := os.WriteFile(p.config.MavenExecutable, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	config := p.config
	config.BuildJavaHomes = homes
	p, err := New(config)
	if err != nil {
		t.Fatal(err)
	}
	return p, r, homes, log
}

// commandHomes returns the JAVA_HOME of every logged Maven command.
func commandHomes(t *testing.T, log string) []string {
	t.Helper()
	body, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	var homes []string
	for _, line := range strings.Split(strings.TrimSpace(string(body)), "\n") {
		home, _, _ := strings.Cut(line, " ")
		homes = append(homes, home)
	}
	return homes
}

func TestSourceTargetProjectBuildsWithItsJDK(t *testing.T) {
	p, r, homes, log := buildJDKFixture(t, true, 0, 8, 11)
	build, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if build.Status != bc.Complete {
		t.Fatal(build.Diagnostics)
	}
	// The service JDK reads the model; JDK 8 reads it again and builds.
	want := []string{p.config.JavaHome, homes[8], homes[8], homes[8], homes[8]}
	if got := commandHomes(t, log); strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("JAVA_HOME per command = %q, want %q", got, want)
	}
	// Attribution still pins the service JDK.
	if len(build.Inventory.JDKs) != 1 || build.Inventory.JDKs[0].Major != 21 {
		t.Fatalf("inventory JDKs = %+v", build.Inventory.JDKs)
	}
}

func TestReleaseProjectNeedsJDK9OrLater(t *testing.T) {
	p, r, homes, log := buildJDKFixture(t, false, 0, 8, 11)
	if _, err := p.Build(context.Background(), r); err != nil {
		t.Fatal(err)
	}
	want := []string{p.config.JavaHome, homes[11], homes[11], homes[11], homes[11]}
	if got := commandHomes(t, log); strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("JAVA_HOME per command = %q, want %q", got, want)
	}

	// Without a JDK that has --release, the service JDK builds it.
	p, r, _, log = buildJDKFixture(t, false, 0, 8)
	if _, err := p.Build(context.Background(), r); err != nil {
		t.Fatal(err)
	}
	for _, home := range commandHomes(t, log) {
		if home != p.config.JavaHome {
			t.Fatalf("release 8 built with %s", home)
		}
	}
}

func TestFailedBuildWithProjectJDKRetriesWithServiceJDK(t *testing.T) {
	p, r, homes, log := buildJDKFixture(t, true, 8, 8)
	build, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if build.Status != bc.Complete {
		t.Fatal(build.Diagnostics)
	}
	want := []string{p.config.JavaHome, homes[8], homes[8], p.config.JavaHome, p.config.JavaHome, p.config.JavaHome}
	if got := commandHomes(t, log); strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("JAVA_HOME per command = %q, want %q", got, want)
	}

	// When both fail, the service JDK goes past the failures, one module
	// at a time, and records what failed.
	p, r, _, log = buildJDKFixture(t, true, -1, 8)
	if _, err = p.Build(context.Background(), r); err != nil {
		t.Fatalf("error = %v", err)
	}
	body, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(body)), "\n")
	past := lines[4]
	if !strings.HasPrefix(past, p.config.JavaHome+" ") || !strings.Contains(past, failOnErrorOff+" --fail-at-end") || !strings.Contains(past, "-T 1 ") || !strings.HasSuffix(past, " install") {
		t.Fatalf("commands =\n%s", body)
	}
}

func TestBuildJDKsAreValidatedAndKeyTheCache(t *testing.T) {
	p, r, homes, _ := buildJDKFixture(t, true, 0, 8)
	config := p.config
	config.BuildJavaHomes = map[int]string{11: homes[8]}
	if _, err := New(config); err == nil {
		t.Fatal("a JDK 8 accepted as build JDK 11")
	}
	config.BuildJavaHomes = map[int]string{8: "jdk8"}
	if _, err := New(config); err == nil {
		t.Fatal("relative build JDK accepted")
	}
	with, err := p.buildFingerprint(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	config.BuildJavaHomes = nil
	plain, err := New(config)
	if err != nil {
		t.Fatal(err)
	}
	without, err := plain.buildFingerprint(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if with == without {
		t.Fatal("build JDKs left out of the cache key")
	}
}

// Some JDK 8 builds have no release file; the launcher reports the version.
func TestBuildJDKWithoutReleaseFileAsksItsLauncher(t *testing.T) {
	home := t.TempDir()
	if err := os.MkdirAll(filepath.Join(home, "bin"), 0o700); err != nil {
		t.Fatal(err)
	}
	launcher := "#!/bin/sh\necho 'Property settings:' >&2\necho '    java.specification.version = 1.8' >&2\necho 'openjdk version \"1.8.0_442\"' >&2\n"
	if err := os.WriteFile(filepath.Join(home, "bin", "java"), []byte(launcher), 0o700); err != nil {
		t.Fatal(err)
	}
	if major, err := jdkMajor(home); err != nil || major != 8 {
		t.Fatalf("major = %d, %v", major, err)
	}
	p, _, _ := observedFixture(t)
	config := p.config
	config.BuildJavaHomes = map[int]string{8: home}
	if p, err := New(config); err != nil || len(p.buildJDKs) != 1 {
		t.Fatal(err)
	}
}

func TestFailedStageKeepsMavenErrorsWithoutFooter(t *testing.T) {
	m := &mavenRunner{provider: &Provider{config: Config{CacheDir: "/cache"}}, request: bc.Request{Checkout: bc.Checkout{Path: "/work/run/checkout"}}}
	tail := strings.Join([]string{
		"[INFO] Compiling 8 source files to /work/run/checkout/target/classes",
		"[INFO] BUILD FAILURE",
		"[ERROR] COMPILATION ERROR : ",
		"[ERROR] error reading /cache/repository/org/aspectj/aspectjweaver/1.8.9/aspectjweaver-1.8.9.jar; Invalid CEN header (invalid zip64 extra data field size)",
		"[ERROR] /work/run/checkout/src/main/java/A.java:[1,1] cannot access p",
		"[ERROR] -> [Help 1]",
		"[ERROR] ",
		"[ERROR] To see the full stack trace of the errors, re-run Maven with the -e switch.",
		"[ERROR] Re-run Maven using the -X switch to enable full debug logging.",
		"[ERROR] For more information about the errors and possible solutions, please read the following articles:",
		"[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/MojoFailureException",
	}, "\n")
	want := "[ERROR] COMPILATION ERROR :\n[ERROR] error reading <maven-repository>/org/aspectj/aspectjweaver/1.8.9/aspectjweaver-1.8.9.jar; Invalid CEN header (invalid zip64 extra data field size)\n[ERROR] src/main/java/A.java:[1,1] cannot access p"
	log := filepath.Join(t.TempDir(), "prepare.log")
	if err := os.WriteFile(log, []byte(tail), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := m.errors(log, []byte("[ERROR] only the tail")); got != want {
		t.Fatalf("errors =\n%s\nwant\n%s", got, want)
	}
	if got := m.errors(filepath.Join(t.TempDir(), "absent.log"), []byte("  killed by signal\n")); got != "killed by signal" {
		t.Fatalf("without [ERROR] lines = %q", got)
	}
}

// compileFailureFixture gives the fixture a Maven that answers install the
// way maven-compiler-plugin does when javac rejects app's sources: a build
// failure, or success past the errors with failOnError off. Every other
// failure is failure: install without the property exits for another reason
// when reason is set. Past the errors, a later plugin fails with later when
// it is set.
func compileFailureFixture(t *testing.T, reason, later string) (*Provider, bc.Request, string) {
	t.Helper()
	p, r, model := observedFixture(t)
	log := filepath.Join(t.TempDir(), "commands")
	quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }
	source := filepath.Join(r.Checkout.Path, "app/src/main/java/A.java")
	failure := "echo '[ERROR] COMPILATION ERROR : '; echo " + quote("[ERROR] "+source+":[1,9] cannot find symbol") + "; echo '[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.13.0:compile (default-compile) on project app: Compilation failure'; exit 1"
	if reason != "" {
		failure = "echo " + quote("[ERROR] "+reason) + "; exit 1"
	}
	past := ""
	if later != "" {
		past = "; echo " + quote("[ERROR] "+later) + "; exit 1"
	}
	script := "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$*\" >> " + quote(log) + "\npast=false\nfor arg in \"$@\"; do\n case \"$arg\" in -Dmaven.compiler.failOnError=false) past=true ;; esac\ndone\nfor arg in \"$@\"; do\n case \"$arg\" in -Doutput=*) cp " + quote(model) + " \"${arg#-Doutput=}\" ;;\n install) if $past; then echo " + quote("[ERROR] "+source+":[1,9] cannot find symbol") + "; echo " + quote("[ERROR] "+source+":[2,3] cannot find symbol") + past + "; else " + failure + "; fi ;; esac\ndone\n"
	if err := os.WriteFile(p.config.MavenExecutable, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	return p, r, log
}

func TestCompileErrorsLeaveOutputOutInsteadOfFailing(t *testing.T) {
	p, r, log := compileFailureFixture(t, "", "")
	build, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if build.Status != bc.Incomplete {
		t.Fatalf("status = %s, want incomplete", build.Status)
	}
	sets := map[string]bc.SourceSet{}
	for _, set := range build.Inventory.SourceSets {
		for _, module := range build.Inventory.Modules {
			if module.ID == set.ModuleID {
				sets[module.Name+":"+set.Name] = set
			}
		}
	}
	// javac wrote nothing for app main, whatever target/classes holds.
	if sets["app:main"].OutputInputID != "" {
		t.Fatal("app main kept output javac never wrote")
	}
	if sets["lib:main"].OutputInputID == "" {
		t.Fatal("lib main lost its output")
	}
	// The tests still see main: resolution compiles it for them.
	sees := false
	for _, entry := range sets["app:test"].Classpath {
		sees = sees || (entry.Kind == bc.EntrySourceSet && entry.RefID == string(sets["app:main"].ID))
	}
	if !sees {
		t.Fatal("app tests lost sight of app main")
	}
	var gaps []bc.MissingInput
	for _, gap := range build.Inventory.MissingInputs {
		if gap.Requested == bc.GapCompiledOutput {
			gaps = append(gaps, gap)
		}
	}
	want := "Maven could not compile it: app/src/main/java/A.java:[1,9] cannot find symbol; app/src/main/java/A.java:[2,3] cannot find symbol"
	if len(gaps) != 1 || gaps[0].SourceSetID != sets["app:main"].ID || gaps[0].Reason != want {
		t.Fatalf("gaps = %+v, want one for app main: %s", gaps, want)
	}
	// One failing install, then past the errors; the classpaths still run.
	body, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	commands := strings.Split(strings.TrimSpace(string(body)), "\n")
	if len(commands) != 5 || strings.Contains(commands[1], failOnErrorOff) || !strings.Contains(commands[2], failOnErrorOff+" --fail-at-end") || !strings.HasSuffix(commands[2], "install") || !strings.Contains(commands[1], "-Dspring-boot.repackage.skip=true") || !strings.Contains(commands[2], "-T 1C") {
		t.Fatalf("commands =\n%s", body)
	}
	// Output that is missing is never cached: the next run builds again.
	fingerprint, err := p.buildFingerprint(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(p.entryPath(r.Checkout.RepositoryID, fingerprint)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("a build with compile errors was cached: %v", err)
	}
}

// A step that fails for another reason than javac's (a plugin that cannot
// be resolved, a parallel build that raced) no longer ends the build: it
// runs again past the failures, one module at a time with a larger heap.
func TestBuildFailuresOtherThanCompileErrorsGoPastTheFailures(t *testing.T) {
	p, r, log := compileFailureFixture(t, "Plugin org.example:broken:1 could not be resolved", "")
	if _, err := p.Build(context.Background(), r); err != nil {
		t.Fatalf("error = %v", err)
	}
	body, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	commands := strings.Split(strings.TrimSpace(string(body)), "\n")
	if len(commands) != 5 || strings.Contains(commands[1], failOnErrorOff) || !strings.Contains(commands[2], failOnErrorOff+" --fail-at-end") || !strings.Contains(commands[2], "-T 1 ") || !strings.Contains(commands[3], "--fail-at-end") {
		t.Fatalf("commands =\n%s", body)
	}
}

// Past javac's errors, a plugin that reads the classes it never wrote can
// fail (Spring Boot's repackage finds no main class); the build goes on.
func TestLaterStepFailingPastCompileErrorsStillBuilds(t *testing.T) {
	p, r, _ := compileFailureFixture(t, "", "Failed to execute goal org.springframework.boot:spring-boot-maven-plugin:3.3.4:repackage (repackage) on project app: Unable to find main class -> [Help 1]")
	build, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	var gaps int
	for _, gap := range build.Inventory.MissingInputs {
		if gap.Requested == bc.GapCompiledOutput {
			gaps++
		}
	}
	if gaps != 1 {
		t.Fatalf("gaps = %+v", build.Inventory.MissingInputs)
	}
}

// The failed step leads the message, ahead of the compiler lines that would
// otherwise fill it.
func TestFailedStageNamesTheFailedGoalFirst(t *testing.T) {
	m := &mavenRunner{provider: &Provider{config: Config{CacheDir: "/cache"}}, request: bc.Request{Checkout: bc.Checkout{Path: "/work/run/checkout"}}}
	log := filepath.Join(t.TempDir(), "prepare.log")
	body := strings.Join([]string{
		"[ERROR] /work/run/checkout/src/main/java/A.java:[1,1] cannot find symbol",
		"[ERROR] /work/run/checkout/src/main/java/B.java:[2,2] cannot find symbol",
		"[INFO] BUILD FAILURE",
		"[ERROR] Failed to execute goal org.springframework.boot:spring-boot-maven-plugin:3.3.4:repackage (repackage) on project app: Unable to find main class -> [Help 1]",
		"[ERROR] -> [Help 1]",
	}, "\n")
	if err := os.WriteFile(log, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	want := "[ERROR] Failed to execute goal org.springframework.boot:spring-boot-maven-plugin:3.3.4:repackage (repackage) on project app: Unable to find main class\n[ERROR] src/main/java/A.java:[1,1] cannot find symbol\n[ERROR] src/main/java/B.java:[2,2] cannot find symbol"
	if got := m.errors(log, nil); got != want {
		t.Fatalf("errors =\n%s\nwant\n%s", got, want)
	}
}

// A dependency module javac rejected installed an empty JAR. Its dependents
// see its source set instead, for resolution to compile.
func TestDependentsSeeAModuleJavacRejectedAsItsSourceSet(t *testing.T) {
	p, r, _ := compileFailureFixture(t, "", "")
	script, err := os.ReadFile(p.config.MavenExecutable)
	if err != nil {
		t.Fatal(err)
	}
	moved := strings.ReplaceAll(string(script), filepath.Join(r.Checkout.Path, "app/src/main/java/A.java"), filepath.Join(r.Checkout.Path, "lib/src/main/java/A.java"))
	if err := os.WriteFile(p.config.MavenExecutable, []byte(moved), 0o700); err != nil {
		t.Fatal(err)
	}
	build, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	var lib, app bc.SourceSet
	for _, set := range build.Inventory.SourceSets {
		for _, module := range build.Inventory.Modules {
			if module.ID == set.ModuleID && set.Name == "main" {
				switch module.Name {
				case "lib":
					lib = set
				case "app":
					app = set
				}
			}
		}
	}
	if lib.OutputInputID != "" || app.OutputInputID == "" {
		t.Fatalf("outputs: lib %q app %q", lib.OutputInputID, app.OutputInputID)
	}
	for _, entry := range app.Classpath {
		if entry.Kind == bc.EntrySourceSet && entry.RefID == string(lib.ID) {
			return
		}
	}
	t.Fatalf("app classpath %+v does not see lib's source set", app.Classpath)
}
