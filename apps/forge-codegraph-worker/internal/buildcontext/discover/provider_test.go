package discover

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func write(t *testing.T, root, name, data string) {
	t.Helper()
	p := filepath.Join(root, name)
	if err := os.MkdirAll(filepath.Dir(p), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
}
func request(root string) bc.Request {
	return bc.Request{Checkout: bc.Checkout{Path: root, RepositoryID: "github.com/example/project", SnapshotID: "commit-1"}, Limits: bc.DefaultLimits()}
}
func build(t *testing.T, root string, config Config) bc.BuildContext {
	t.Helper()
	p, err := New(config)
	if err != nil {
		t.Fatal(err)
	}
	c, err := p.Build(context.Background(), request(root))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	return c
}
func sets(c bc.BuildContext) map[string]bc.SourceSet {
	modules := map[bc.ModuleID]string{}
	for _, m := range c.Inventory.Modules {
		modules[m.ID] = m.Directory
	}
	out := map[string]bc.SourceSet{}
	for _, s := range c.Inventory.SourceSets {
		out[modules[s.ModuleID]+"/"+s.Name] = s
	}
	return out
}
func roots(c bc.BuildContext, s bc.SourceSet) []string {
	in := map[bc.InputID]bc.Input{}
	for _, i := range c.Inventory.Inputs {
		in[i.ID] = i
	}
	var out []string
	for _, id := range s.SourceRootIDs {
		out = append(out, in[id].Location.Path)
	}
	return out
}
func hasGap(c bc.BuildContext, text string) bool {
	for _, g := range c.Inventory.MissingInputs {
		if strings.Contains(g.Reason, text) {
			return true
		}
	}
	return false
}

func mavenFixture(t *testing.T, root string) {
	write(t, root, "pom.xml", `<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>parent</artifactId><version>1.0</version><packaging>pom</packaging>
<properties><maven.compiler.release>17</maven.compiler.release><lib.version>2.0</lib.version></properties>
<modules><module>app</module><module>util</module></modules>
<dependencyManagement><dependencies><dependency><groupId>example</groupId><artifactId>lib</artifactId><version>${lib.version}</version></dependency></dependencies></dependencyManagement>
</project>`)
	write(t, root, "app/pom.xml", `<project><parent><groupId>example</groupId><artifactId>parent</artifactId><version>1.0</version></parent><artifactId>app</artifactId>
<properties><maven.compiler.release>21</maven.compiler.release><lib.version>3.0</lib.version></properties>
<build><sourceDirectory>code/java</sourceDirectory><plugins>
<plugin><artifactId>maven-compiler-plugin</artifactId><configuration><testRelease>17</testRelease></configuration></plugin>
<plugin><groupId>org.codehaus.mojo</groupId><artifactId>build-helper-maven-plugin</artifactId><executions><execution><goals><goal>add-source</goal></goals><configuration><sources><source>extra/java</source></sources></configuration></execution></executions></plugin>
</plugins></build>
<dependencies><dependency><groupId>example</groupId><artifactId>lib</artifactId></dependency><dependency><groupId>example</groupId><artifactId>test-lib</artifactId><version>4.0</version><scope>test</scope></dependency></dependencies></project>`)
	write(t, root, "util/pom.xml", `<project><parent><groupId>example</groupId><artifactId>parent</artifactId><version>1.0</version></parent><artifactId>util</artifactId></project>`)
	for _, name := range []string{"app/code/java/App.java", "app/extra/java/Extra.java", "app/src/test/java/AppTest.java", "util/src/main/java/Util.java"} {
		write(t, root, name, "class Example {}")
	}
}

func TestMavenIndependentExpectedInventory(t *testing.T) {
	root := t.TempDir()
	mavenFixture(t, root)
	c := build(t, root, Config{})
	got := sets(c)
	if len(c.Inventory.Modules) != 3 || len(got) != 3 || got["app/main"].TargetRelease != 21 || got["app/test"].TargetRelease != 17 || got["util/main"].TargetRelease != 17 {
		t.Fatalf("modules=%+v sets=%+v", c.Inventory.Modules, got)
	}
	if !reflect.DeepEqual(roots(c, got["app/main"]), []string{"app/code/java", "app/extra/java"}) {
		t.Fatal("custom source roots", roots(c, got["app/main"]))
	}
	if got["app/test"].Classpath[0] != (bc.PathEntry{Kind: bc.EntrySourceSet, RefID: string(got["app/main"].ID)}) {
		t.Fatal("test visibility")
	}
	versions := map[string]string{}
	for _, a := range c.Inventory.Artifacts {
		versions[a.Coordinates.Name] = a.Coordinates.Version
	}
	if !reflect.DeepEqual(versions, map[string]string{"lib": "3.0", "test-lib": "4.0"}) {
		t.Fatal("managed property override", versions)
	}
	for _, g := range c.Inventory.MissingInputs {
		if g.SourceSetID == got["app/main"].ID && strings.Contains(g.Requested, "test-lib") {
			t.Fatal("test dependency leaked into main")
		}
	}
	if c.Status != bc.Incomplete || !hasGap(c, "Compiler classpath") || hasGap(c, "fallback profile") {
		t.Fatal("incorrect discovery claims", c.Diagnostics)
	}
}

func TestGradleGroovyExpectedInventory(t *testing.T) {
	root := t.TempDir()
	write(t, root, "settings.gradle", `rootProject.name = 'sample'
include ':app', ':shared'
project(':shared').projectDir = file('libraries/shared')`)
	write(t, root, "gradle.properties", "javaRelease=17\nlibVersion=3.2.1\n")
	write(t, root, "build.gradle", `subprojects {
    java { toolchain { languageVersion = JavaLanguageVersion.of(javaRelease) } }
}
// sourceCompatibility = 8
def message = "sourceCompatibility = 11"
if (false) { sourceCompatibility = 8 }`)
	write(t, root, "app/build.gradle", `plugins { id 'java' }
tasks.withType(JavaCompile).configureEach { options.release.set(21) }
sourceSets {
  main { java { srcDirs = ['code/java'] } }
  integrationTest { java.srcDir 'integration/java' }
}
dependencies { implementation "example:lib:$libVersion" }
if (false) { sourceSets { main { java.srcDir 'bad' } } }`)
	write(t, root, "libraries/shared/build.gradle", `plugins { id 'java-library' }`)
	for _, name := range []string{"app/code/java/App.java", "app/src/test/java/AppTest.java", "app/integration/java/IT.java", "libraries/shared/src/main/java/Shared.java"} {
		write(t, root, name, "class Example {}")
	}
	c := build(t, root, Config{})
	got := sets(c)
	if len(got) != 4 || got["app/main"].TargetRelease != 21 || got["app/test"].TargetRelease != 21 || got["libraries/shared/main"].TargetRelease != 17 {
		t.Fatalf("sets=%+v", got)
	}
	if !reflect.DeepEqual(roots(c, got["app/main"]), []string{"app/code/java"}) {
		t.Fatal("replacement or conditional roots", roots(c, got["app/main"]))
	}
	if !reflect.DeepEqual(roots(c, got["app/integrationTest"]), []string{"app/src/integrationTest/java", "app/integration/java"}) {
		t.Fatal("custom set", roots(c, got["app/integrationTest"]))
	}
	if len(c.Inventory.Artifacts) != 1 || c.Inventory.Artifacts[0].Coordinates.Version != "3.2.1" {
		t.Fatal("dependency", c.Inventory.Artifacts)
	}
	if hasGap(c, "fallback profile") || !hasGap(c, "plugins, conditions") {
		t.Fatal(c.Diagnostics)
	}
}

func TestGradleKotlinExpectedInventory(t *testing.T) {
	root := t.TempDir()
	write(t, root, "settings.gradle.kts", `rootProject.name = "kotlin-build"`)
	write(t, root, "build.gradle.kts", `plugins { java }
java { toolchain { languageVersion.set(JavaLanguageVersion.of(21)) } }
tasks.named<JavaCompile>("compileJava") { options.release.set(17) }
tasks.named<JavaCompile>("compileTestJava") { options.release.set(21) }
sourceSets {
  named("main") { java.setSrcDirs(listOf("source/java", "more/java")) }
  val integrationTest by creating { java.srcDir("integration/java") }
}
dependencies {
 implementation("example:lib:1.2")
 testImplementation(libs.junit)
 implementation(project(":other"))
}
val doc = """sourceCompatibility = 8 { broken text }"""
/* toolchain { languageVersion.set(JavaLanguageVersion.of(8)) } */`)
	for _, name := range []string{"source/java/App.java", "more/java/More.java", "src/test/java/Test.java", "integration/java/IT.java"} {
		write(t, root, name, "class Example {}")
	}
	c := build(t, root, Config{})
	got := sets(c)
	if got["./main"].TargetRelease != 17 || got["./test"].TargetRelease != 21 || got["./integrationTest"].TargetRelease != 21 {
		t.Fatal(got)
	}
	if !reflect.DeepEqual(roots(c, got["./main"]), []string{"source/java", "more/java"}) {
		t.Fatal(roots(c, got["./main"]))
	}
	if !hasGap(c, "Project, catalog") || len(c.Inventory.Artifacts) != 1 {
		t.Fatal("unsupported dependency diagnostics")
	}
}

func TestUnknownReleaseAndNoBuildFallback(t *testing.T) {
	root := t.TempDir()
	write(t, root, "Main.java", "class Main {}")
	c := build(t, root, Config{FallbackRelease: 17})
	if c.Inventory.SourceSets[0].TargetRelease != 17 || !hasGap(c, "fallback profile") || !hasGap(c, "No Java compilation") {
		t.Fatal(c)
	}
	write(t, root, "pom.xml", `<project><artifactId>app</artifactId><properties><maven.compiler.release>${cycle}</maven.compiler.release><cycle>${maven.compiler.release}</cycle></properties></project>`)
	c = build(t, root, Config{})
	if c.Inventory.SourceSets[0].TargetRelease != 21 || !hasGap(c, "fallback profile") {
		t.Fatal(c)
	}
}

func TestMissingParentProfilesAndBOMAreExplicit(t *testing.T) {
	root := t.TempDir()
	write(t, root, "pom.xml", `<project><parent><groupId>x</groupId><artifactId>parent</artifactId><version>1</version><relativePath/></parent><artifactId>a</artifactId>
<dependencyManagement><dependencies><dependency><groupId>x</groupId><artifactId>bom</artifactId><version>1</version><type>pom</type><scope>import</scope></dependency></dependencies></dependencyManagement>
<profiles><profile><id>jdk21</id><properties><maven.compiler.release>21</maven.compiler.release></properties></profile></profiles></project>`)
	c := build(t, root, Config{})
	for _, text := range []string{"parent is unavailable", "profile activation", "Imported BOM", "fallback profile"} {
		if !hasGap(c, text) {
			t.Fatal("missing gap", text, c.Diagnostics)
		}
	}
}

func TestDiscoveryDeterministicConcurrentOwnedAndMountIndependent(t *testing.T) {
	a, b := t.TempDir(), t.TempDir()
	mavenFixture(t, a)
	mavenFixture(t, b)
	p, _ := New(Config{})
	want := build(t, a, Config{})
	other := build(t, b, Config{})
	if want.ID != other.ID {
		t.Fatal("mount-dependent identity")
	}
	var wg sync.WaitGroup
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			got, err := p.Build(context.Background(), request(a))
			if err != nil || got.ID != want.ID {
				t.Errorf("concurrent Build: %v", err)
			}
			got.Inventory.Modules[0].Name = "mutated"
		}()
	}
	wg.Wait()
	again, err := p.Build(context.Background(), request(a))
	if err != nil || again.ID != want.ID {
		t.Fatal("shared output", err)
	}
	data, _ := json.Marshal(want)
	if strings.Contains(string(data), a) {
		t.Fatal("checkout path persisted")
	}
}

func TestLimitsCancellationAndInvalidBuilds(t *testing.T) {
	root := t.TempDir()
	mavenFixture(t, root)
	p, _ := New(Config{})
	for _, adjust := range []func(*bc.Limits){func(l *bc.Limits) { l.MaxInputBytes = 16 }, func(l *bc.Limits) { l.MaxRecords = 2 }, func(l *bc.Limits) { l.MaxFiles = 2 }, func(l *bc.Limits) { l.MaxDepth = 2 }, func(l *bc.Limits) { l.MaxDiagnostics = 1 }, func(l *bc.Limits) { l.MaxOutputBytes = 8 }} {
		r := request(root)
		adjust(&r.Limits)
		if _, err := p.Build(context.Background(), r); !errors.Is(err, bc.ErrLimitExceeded) {
			t.Fatalf("expected limit, got %v", err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.Build(ctx, request(root)); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	for _, xml := range []string{"<project>", "<!DOCTYPE project><project/>", "<project/><project/>", "<project><artifactId>a</artifactId><artifactId>b</artifactId></project>"} {
		t.Run(xml, func(t *testing.T) {
			dir := t.TempDir()
			write(t, dir, "pom.xml", xml)
			if _, err := p.Build(context.Background(), request(dir)); !errors.Is(err, bc.ErrInvalidInput) {
				t.Fatal(err)
			}
		})
	}
}

func TestAmbiguityForcedModesAndMalformedManifest(t *testing.T) {
	root := t.TempDir()
	write(t, root, "pom.xml", "<project><artifactId>a</artifactId></project>")
	write(t, root, "build.gradle", "plugins { id 'java' }")
	write(t, root, "src/main/java/App.java", "class App {}")
	p, _ := New(Config{})
	if _, err := p.Build(context.Background(), request(root)); !errors.Is(err, bc.ErrInvalidInput) {
		t.Fatal(err)
	}
	for _, mode := range []string{"maven", "gradle"} {
		c := build(t, root, Config{Mode: mode})
		if !strings.Contains(c.Producer.Name, mode) {
			t.Fatal(c.Producer)
		}
	}
	write(t, root, ".codegraph/build-context.json", "not json")
	if _, err := p.Build(context.Background(), request(root)); !errors.Is(err, bc.ErrInvalidInput) {
		t.Fatal("malformed manifest silently fell back", err)
	}
}

func TestParentCyclesAndPathEscape(t *testing.T) {
	root := t.TempDir()
	write(t, root, "pom.xml", `<project><groupId>x</groupId><artifactId>a</artifactId><version>1</version><parent><groupId>x</groupId><artifactId>b</artifactId><version>1</version><relativePath>b/pom.xml</relativePath></parent></project>`)
	write(t, root, "b/pom.xml", `<project><groupId>x</groupId><artifactId>b</artifactId><version>1</version><parent><groupId>x</groupId><artifactId>a</artifactId><version>1</version></parent></project>`)
	p, _ := New(Config{})
	if _, err := p.Build(context.Background(), request(root)); !errors.Is(err, bc.ErrInvalidInput) {
		t.Fatal(err)
	}
	write(t, root, "pom.xml", `<project><artifactId>a</artifactId><build><sourceDirectory>../../outside</sourceDirectory></build></project>`)
	c := build(t, root, Config{})
	if !hasGap(c, "Source directory could not be resolved") {
		t.Fatal(c.Diagnostics)
	}
	for _, input := range c.Inventory.Inputs {
		if input.Location != nil && !bc.ValidPath(input.Location.Path) {
			t.Fatal("escaped source", input)
		}
	}
}

func TestNoRepositoryCodeExecuted(t *testing.T) {
	root := t.TempDir()
	marker := filepath.Join(t.TempDir(), "executed")
	write(t, root, "build.gradle", `throw new RuntimeException("Must not execute")
sourceCompatibility = JavaVersion.VERSION_17`)
	write(t, root, "gradlew", "#!/bin/sh\ntouch "+marker+"\n")
	if err := os.Chmod(filepath.Join(root, "gradlew"), 0700); err != nil {
		t.Fatal(err)
	}
	write(t, root, "src/main/java/App.java", "class App {}")
	c := build(t, root, Config{})
	if c.Inventory.SourceSets[0].TargetRelease != 17 {
		t.Fatal(c)
	}
	if _, err := os.Stat(marker); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("repository wrapper executed", err)
	}
}

func TestCompilerExecutionBoundariesAndInheritance(t *testing.T) {
	root := t.TempDir()
	write(t, root, "pom.xml", `<project><groupId>x</groupId><artifactId>parent</artifactId><version>1</version><packaging>pom</packaging><modules><module>app</module></modules><build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><inherited>false</inherited><configuration><release>8</release></configuration></plugin></plugins></build></project>`)
	write(t, root, "app/pom.xml", `<project><parent><groupId>x</groupId><artifactId>parent</artifactId><version>1</version></parent><artifactId>app</artifactId><properties><maven.compiler.release>17</maven.compiler.release></properties><build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><executions><execution><id>default-compile</id><configuration><release>21</release><source>11</source></configuration></execution></executions></plugin></plugins></build></project>`)
	write(t, root, "app/src/test/java/Test.java", "class Test {}")
	c := build(t, root, Config{})
	got := sets(c)
	if got["app/main"].TargetRelease != 21 || got["app/test"].TargetRelease != 17 {
		t.Fatal("compile execution affected tests or noninherited plugin leaked", got)
	}
}

func TestMixedMonorepoAndUnmappedJava(t *testing.T) {
	root := t.TempDir()
	write(t, root, "service/pom.xml", `<project><artifactId>service</artifactId><properties><maven.compiler.release>11</maven.compiler.release></properties></project>`)
	write(t, root, "service/src/main/java/Service.java", "class Service {}")
	write(t, root, "library/build.gradle", `sourceCompatibility = JavaVersion.VERSION_17`)
	write(t, root, "library/src/main/java/Library.java", "class Library {}")
	write(t, root, "unmapped/Extra.java", "class Extra {}")
	write(t, root, "library/src/test/resources/fixture/pom.xml", "deliberately not a real build")
	c := build(t, root, Config{})
	got := sets(c)
	if len(got) != 2 || got["service/main"].TargetRelease != 11 || got["library/main"].TargetRelease != 17 || !strings.Contains(c.Producer.Name, "mixed") {
		t.Fatal(c)
	}
	if !hasGap(c, "1 directories contain Java files") {
		t.Fatal(c.Diagnostics)
	}
}

func TestSymlinkBuildAndSourceInputs(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	write(t, outside, "pom.xml", "<project/>")
	if err := os.Symlink(filepath.Join(outside, "pom.xml"), filepath.Join(root, "pom.xml")); err != nil {
		t.Fatal(err)
	}
	p, _ := New(Config{})
	if _, err := p.Build(context.Background(), request(root)); !errors.Is(err, bc.ErrInvalidInput) {
		t.Fatal("followed build symlink", err)
	}
	if err := os.Remove(filepath.Join(root, "pom.xml")); err != nil {
		t.Fatal(err)
	}
	write(t, root, "pom.xml", `<project><artifactId>a</artifactId><build><sourceDirectory>linked</sourceDirectory></build></project>`)
	if err := os.Symlink(outside, filepath.Join(root, "linked")); err != nil {
		t.Fatal(err)
	}
	c := build(t, root, Config{})
	for _, check := range c.Checks {
		if strings.HasPrefix(string(check.InputID), "source:") && check.Status == bc.Available {
			t.Fatal("followed source symlink")
		}
	}
}
