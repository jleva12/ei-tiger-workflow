package maven

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func writeFile(t *testing.T, name, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(name), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(name, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

// Maven builds the checkout's own project: the root POM, the one project in
// a subdirectory, or every top-level project through an aggregator. POMs in
// a module's sources (fixtures, archetype templates) and in installed
// packages are not projects.
func TestRootPOMFindsTheProjectsToBuild(t *testing.T) {
	ctx := context.Background()
	request := func(root string) bc.Request {
		return bc.Request{Checkout: bc.Checkout{Path: root, RepositoryID: "repo", SnapshotID: strings.Repeat("a", 40)}, Limits: bc.DefaultLimits()}
	}
	pom := "<project><groupId>g</groupId><artifactId>a</artifactId><version>1</version></project>"

	root := t.TempDir()
	writeFile(t, filepath.Join(root, "pom.xml"), pom)
	writeFile(t, filepath.Join(root, "other/pom.xml"), pom)
	if got, err := rootPOM(ctx, request(root), t.TempDir()); err != nil || got != "" {
		t.Fatalf("root POM: %q %v", got, err)
	}

	root = t.TempDir()
	writeFile(t, filepath.Join(root, "backend/pom.xml"), pom)
	writeFile(t, filepath.Join(root, "backend/api/pom.xml"), pom)
	writeFile(t, filepath.Join(root, "web/node_modules/x/pom.xml"), pom)
	if got, err := rootPOM(ctx, request(root), t.TempDir()); err != nil || got != filepath.Join("backend", "pom.xml") {
		t.Fatalf("one project in a subdirectory: %q %v", got, err)
	}

	root = t.TempDir()
	writeFile(t, filepath.Join(root, "app/pom.xml"), pom)
	writeFile(t, filepath.Join(root, "app/src/test/resources/fixture/pom.xml"), pom)
	writeFile(t, filepath.Join(root, "services/lib/pom.xml"), pom)
	scratch := t.TempDir()
	got, err := rootPOM(ctx, request(root), scratch)
	if err != nil || got != filepath.Join(scratch, "aggregator", "pom.xml") {
		t.Fatalf("several projects: %q %v", got, err)
	}
	body, err := os.ReadFile(got)
	if err != nil {
		t.Fatal(err)
	}
	modules := regexp.MustCompile(`<module>([^<]+)</module>`).FindAllStringSubmatch(string(body), -1)
	if len(modules) != 2 || !strings.Contains(string(body), "<artifactId>"+aggregatorArtifact+"</artifactId>") {
		t.Fatalf("aggregator:\n%s", body)
	}
	for i, want := range []string{"app", "services/lib"} {
		from, _ := filepath.EvalSymlinks(filepath.Dir(got))
		dir, err := filepath.EvalSymlinks(filepath.Join(from, filepath.FromSlash(modules[i][1])))
		expected, _ := filepath.EvalSymlinks(filepath.Join(root, want))
		if err != nil || dir != expected {
			t.Fatalf("module %s resolves to %s (%v)", modules[i][1], dir, err)
		}
	}
}

// The observer turns what it cannot place into gaps instead of failing the
// build: a POM that does not parse, coordinates two POMs declare, a
// system-scoped JAR in the checkout, a POM-typed dependency, a timestamped
// snapshot, a JAR outside every pinned root, and Java levels the analysis
// JDK does not compile.
func TestObserverPlacesOrLeavesOutWhatItCannotPin(t *testing.T) {
	p, r, model := observedFixture(t)
	root, cache := r.Checkout.Path, p.config.CacheDir
	writeFile(t, filepath.Join(root, "tools/broken/pom.xml"), "<project><artifactId>unclosed")
	writeFile(t, filepath.Join(root, "examples/app-copy/pom.xml"), "<project><groupId>example</groupId><artifactId>app</artifactId><version>1</version></project>")
	system := filepath.Join(root, "app/lib/system.jar")
	writeFile(t, system, "system jar bytes")
	pomDependency := filepath.Join(cache, "repository/example/bom/1/bom-1.pom")
	writeFile(t, pomDependency, "<project/>")
	snapshot := filepath.Join(cache, "repository/example/snap/1.0-SNAPSHOT/snap-1.0-20240101.010101-1.jar")
	writeFile(t, snapshot, "snapshot bytes")
	outside := filepath.Join(t.TempDir(), "outside.jar")
	writeFile(t, outside, "outside bytes")
	dependency := filepath.Join(cache, "repository/example/lib/1/lib-1.jar")
	sep := string(os.PathListSeparator)
	writeFile(t, filepath.Join(root, "app/target/codegraph-compile-classpath.txt"), strings.Join([]string{dependency, system, pomDependency, snapshot, outside}, sep))

	body, err := os.ReadFile(model)
	if err != nil {
		t.Fatal(err)
	}
	text := strings.Replace(string(body), "<release>8</release>", "<release>7</release>", 1)
	text = strings.Replace(text, "<release>8</release>", "<release>${java.version}</release>", 1)
	if err = os.WriteFile(model, []byte(text), 0o600); err != nil {
		t.Fatal(err)
	}

	build, err := p.BuildObserved(context.Background(), r, model)
	if err != nil {
		t.Fatal(err)
	}
	modules := map[bc.ModuleID]bc.Module{}
	for _, m := range build.Inventory.Modules {
		modules[m.ID] = m
	}
	artifacts := map[bc.ArtifactID]bc.Artifact{}
	for _, a := range build.Inventory.Artifacts {
		artifacts[a.ID] = a
	}
	var appMain, libMain bc.SourceSet
	for _, set := range build.Inventory.SourceSets {
		switch modules[set.ModuleID].Name + ":" + set.Name {
		case "app:main":
			appMain = set
		case "lib:main":
			libMain = set
		}
	}
	if modules[appMain.ModuleID].Directory != "app" {
		t.Fatalf("app is placed at %q", modules[appMain.ModuleID].Directory)
	}
	var names []string
	for _, entry := range appMain.Classpath {
		if entry.Kind == bc.EntrySourceSet {
			names = append(names, "set")
			continue
		}
		names = append(names, artifacts[bc.ArtifactID(entry.RefID)].Coordinates.Name)
	}
	if got := strings.Join(names, ","); got != "set,system,snap" {
		t.Fatalf("app main classpath = %s", got)
	}
	gaps := map[string]string{}
	for _, g := range build.Inventory.MissingInputs {
		gaps[g.Requested] = g.Reason
	}
	if !strings.Contains(gaps["classpath_entry:outside.jar"], "outside.jar") {
		t.Fatalf("gaps = %v", gaps)
	}
	if appMain.TargetRelease != 8 || libMain.TargetRelease != 21 {
		t.Fatalf("levels: app %d, lib %d", appMain.TargetRelease, libMain.TargetRelease)
	}
	if !strings.Contains(gaps["compiler_level:"+string(libMain.ID)], "${java.version}") {
		t.Fatalf("an unreadable level is a gap: %v", gaps)
	}
	if _, ok := gaps["compiler_level:"+string(appMain.ID)]; ok {
		t.Fatal("Java 7 is analysed as Java 8 without a gap")
	}
}

// A Maven that talks more than the log budget never fails the build: the
// head and the end of the log are kept, whole lines, around a marker.
func TestOverlongMavenLogKeepsItsHeadAndEnd(t *testing.T) {
	var file bytes.Buffer
	w := &boundedLog{writer: &file, remaining: 64, keep: 200}
	for i := 0; i < 1000; i++ {
		line := fmt.Sprintf("[INFO] line %d\n", i)
		if n, err := w.Write([]byte(line)); err != nil || n != len(line) {
			t.Fatalf("write %d: %d %v", i, n, err)
		}
	}
	if _, err := w.Write([]byte("[ERROR] Failed to execute goal x on project app: boom\n")); err != nil {
		t.Fatal(err)
	}
	if err := w.finish(); err != nil {
		t.Fatal(err)
	}
	text := file.String()
	head, rest, ok := strings.Cut(text, "\n[codegraph: ")
	if !ok || !strings.HasPrefix(head, "[INFO] line 0\n") || len(head) > 64 {
		t.Fatalf("head:\n%s", text)
	}
	_, end, _ := strings.Cut(rest, "]\n")
	if !strings.HasSuffix(end, "[ERROR] Failed to execute goal x on project app: boom\n") || len(end) > 200 || !strings.HasPrefix(end, "[INFO] line ") {
		t.Fatalf("end:\n%s", end)
	}
	if !strings.Contains(string(w.tail), "boom") {
		t.Fatal("the failure tail is kept")
	}
}

func TestRecordedFailuresNameTheirProject(t *testing.T) {
	p, r, _ := observedFixture(t)
	log := filepath.Join(t.TempDir(), "stage.log")
	writeFile(t, log, strings.Join([]string{
		"[INFO] building",
		"[ERROR] Failed to execute goal org.codehaus.mojo:exec-maven-plugin:3.1.0:exec (npm) on project web: Command execution failed. -> [Help 1]",
		"[ERROR] Failed to execute goal on project api: Could not resolve dependencies for project g:api:jar:1: x:y:jar:2 was not found",
		"",
	}, "\n"))
	m := &mavenRunner{provider: p, ctx: context.Background(), request: r}
	m.recordFailures(log)
	if len(m.stageFailures) != 2 || m.stageFailures[0].project != "web" || m.stageFailures[1].project != "api" || strings.Contains(m.stageFailures[0].line, "[Help") {
		t.Fatalf("failures = %+v", m.stageFailures)
	}
	s := &observer{stageFailures: m.stageFailures}
	if got := s.unbuilt(project{Name: "api"}); !strings.HasPrefix(got, "Maven could not build it: Failed to execute goal on project api") {
		t.Fatalf("own failure: %q", got)
	}
	if got := s.unbuilt(project{Name: "app"}); !strings.HasPrefix(got, "Maven did not build it after another step failed: ") {
		t.Fatalf("another's failure: %q", got)
	}
}

// A log far past its budget, written a line at a time, costs time in
// proportion to its size.
func TestOverlongMavenLogIsCheapToWrite(t *testing.T) {
	var file bytes.Buffer
	w := &boundedLog{writer: &file, remaining: 1 << 10, keep: 1 << 20}
	line := []byte(strings.Repeat("x", 99) + "\n")
	start := time.Now()
	for i := 0; i < 200_000; i++ { // 20 MB, 20 times the kept end
		if _, err := w.Write(line); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.finish(); err != nil {
		t.Fatal(err)
	}
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Fatalf("20 MB took %s", elapsed)
	}
	if file.Len() > (1<<10)+(1<<20)+128 {
		t.Fatalf("kept %d bytes", file.Len())
	}
}
