package maven

import (
	"context"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func quoteShell(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }

// countingMaven replaces the fixture's Maven with a script that records every
// invocation and serves the fixture model for effective-pom. The returned
// function reports how many invocations happened so far.
func countingMaven(t *testing.T, p *Provider, model, prelude string) func() []string {
	t.Helper()
	log := filepath.Join(t.TempDir(), "commands")
	script := "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$*\" >> " + quoteShell(log) + "\n" + prelude + "for arg in \"$@\"; do\n case \"$arg\" in -Doutput=*) cp " + quoteShell(model) + " \"${arg#-Doutput=}\" ;; esac\ndone\n"
	if err := os.WriteFile(p.config.MavenExecutable, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	return func() []string {
		body, err := os.ReadFile(log)
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			t.Fatal(err)
		}
		return strings.Split(strings.TrimSpace(string(body)), "\n")
	}
}

func TestSecondBuildOnUnchangedCheckoutRunsNoMaven(t *testing.T) {
	p, r, model := observedFixture(t)
	commands := countingMaven(t, p, model, "")
	first, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if first.Status != bc.Complete || len(commands()) != 4 {
		t.Fatalf("status=%s commands=%q", first.Status, commands())
	}
	entries, err := filepath.Glob(filepath.Join(p.config.CacheDir, "buildcontext", "*", "*.json"))
	if err != nil || len(entries) != 1 {
		t.Fatalf("cache entry not written: %v %v", entries, err)
	}
	if _, err = os.Stat(filepath.Join(p.config.CacheDir, "fingerprints.json")); err != nil {
		t.Fatal("fingerprint cache not saved:", err)
	}
	second, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if got := commands(); len(got) != 4 {
		t.Fatalf("cache hit executed Maven: %q", got[4:])
	}
	if !reflect.DeepEqual(first, second) {
		t.Fatalf("cached context differs:\n%+v\n%+v", first, second)
	}
	if err = second.Validate(); err != nil {
		t.Fatal(err)
	}
	scratch, err := os.ReadDir(p.config.WorkDir)
	if err != nil || len(scratch) != 0 {
		t.Fatal("cache hit left scratch state behind", scratch, err)
	}
}

func TestCacheRestoresRetainedOutputsIntoFreshWorktree(t *testing.T) {
	p, r, model := observedFixture(t)
	commands := countingMaven(t, p, model, "")
	first, err := p.Build(context.Background(), r)
	if err != nil || first.Status != bc.Complete {
		t.Fatal(err, first.Diagnostics)
	}
	// A new worktree of the same snapshot: identical sources, no target/.
	fresh, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	err = filepath.WalkDir(r.Checkout.Path, func(name string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		relative, _ := filepath.Rel(r.Checkout.Path, name)
		if entry.IsDir() {
			if entry.Name() == "target" {
				return filepath.SkipDir
			}
			return os.MkdirAll(filepath.Join(fresh, relative), 0700)
		}
		body, err := os.ReadFile(name)
		if err != nil {
			return err
		}
		return os.WriteFile(filepath.Join(fresh, relative), body, 0600)
	})
	if err != nil {
		t.Fatal(err)
	}
	request := r
	request.Checkout.Path = fresh
	second, err := p.Build(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if got := commands(); len(got) != 4 {
		t.Fatalf("fresh worktree of a cached snapshot executed Maven: %q", got[4:])
	}
	if !reflect.DeepEqual(first, second) {
		t.Fatalf("restored context differs:\n%+v\n%+v", first, second)
	}
	for _, module := range []string{"app", "lib"} {
		body, err := os.ReadFile(filepath.Join(fresh, module, "target", "classes", "A.class"))
		if err != nil || string(body) != "compiled fixture" {
			t.Fatalf("compiled output not restored for %s: %q %v", module, body, err)
		}
	}
	// A retained output that was tampered with in the cache cannot verify;
	// the build falls back to Maven and leaves no partial restore behind.
	retained, err := filepath.Glob(filepath.Join(p.config.CacheDir, "outputs", "*", "A.class"))
	if err != nil || len(retained) != 1 {
		t.Fatalf("retained trees: %v %v", retained, err)
	}
	if err = os.WriteFile(retained[0], []byte("corrupted"), 0600); err != nil {
		t.Fatal(err)
	}
	another, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, module := range []string{"app", "lib"} {
		for _, name := range []string{"pom.xml", "src/main/java/A.java", "src/test/java/Test.java"} {
			body, err := os.ReadFile(filepath.Join(fresh, module, filepath.FromSlash(name)))
			if err != nil {
				t.Fatal(err)
			}
			if err = os.MkdirAll(filepath.Dir(filepath.Join(another, module, filepath.FromSlash(name))), 0700); err != nil {
				t.Fatal(err)
			}
			if err = os.WriteFile(filepath.Join(another, module, filepath.FromSlash(name)), body, 0600); err != nil {
				t.Fatal(err)
			}
		}
	}
	request.Checkout.Path = another
	if _, err = p.Build(context.Background(), request); err == nil {
		t.Fatal("expected the fallback Maven run to fail on a checkout without prepared outputs")
	}
	if got := commands(); len(got) < 5 {
		t.Fatalf("corrupt retained output was reused: %q", got)
	}
	if _, err = os.Stat(filepath.Join(another, "app", "target", "classes")); !os.IsNotExist(err) {
		t.Fatal("partial restore left behind", err)
	}
}

func TestModifiedBuildFilesInvalidateCache(t *testing.T) {
	for _, change := range []struct {
		name string
		edit func(t *testing.T, checkout string)
	}{
		{"module_pom", func(t *testing.T, checkout string) {
			name := filepath.Join(checkout, "app", "pom.xml")
			body, err := os.ReadFile(name)
			if err != nil {
				t.Fatal(err)
			}
			if err = os.WriteFile(name, append(body, []byte("<!-- dependency bump -->")...), 0600); err != nil {
				t.Fatal(err)
			}
		}},
		{"mvn_config", func(t *testing.T, checkout string) {
			if err := os.MkdirAll(filepath.Join(checkout, ".mvn"), 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(checkout, ".mvn", "maven.config"), []byte("-Pfast"), 0600); err != nil {
				t.Fatal(err)
			}
		}},
		{"wrapper", func(t *testing.T, checkout string) {
			if err := os.WriteFile(filepath.Join(checkout, "mvnw"), []byte("#!/bin/sh\n"), 0700); err != nil {
				t.Fatal(err)
			}
		}},
		{"snapshot", func(*testing.T, string) {}},
	} {
		t.Run(change.name, func(t *testing.T) {
			p, r, model := observedFixture(t)
			commands := countingMaven(t, p, model, "")
			first, err := p.Build(context.Background(), r)
			if err != nil {
				t.Fatal(err)
			}
			change.edit(t, r.Checkout.Path)
			if change.name == "snapshot" {
				// Same build files, different sources: compiled outputs would
				// differ, so nothing from the entry is reused.
				r.Checkout.SnapshotID = strings.Repeat("b", 40)
			}
			second, err := p.Build(context.Background(), r)
			if err != nil {
				t.Fatal(err)
			}
			if got := commands(); len(got) != 8 {
				t.Fatalf("change %s did not rerun Maven: %d commands", change.name, len(got))
			}
			if change.name == "snapshot" {
				if second.SnapshotID != r.Checkout.SnapshotID || second.ID == first.ID || second.Producer.InputSHA256 != first.Producer.InputSHA256 {
					t.Fatalf("snapshot identity not recomputed: %+v", second)
				}
			} else if !reflect.DeepEqual(first, second) {
				t.Fatal("touching build files without changing the model changed the context")
			}
			if _, err = p.Build(context.Background(), r); err != nil {
				t.Fatal(err)
			}
			if got := commands(); len(got) != 8 {
				t.Fatalf("new entry was not reused: %d commands", len(got))
			}
		})
	}
}

func TestOfflineFailureRetriesOnlineOnceThenStaysOnline(t *testing.T) {
	p, r, model := observedFixture(t)
	// Offline resolution fails the way Maven reports a never-downloaded
	// artifact; online invocations succeed.
	prelude := "for arg in \"$@\"; do case \"$arg\" in -o) echo '[ERROR] Cannot access central (https://repo.maven.apache.org/maven2) in offline mode and the artifact org.apache.maven.plugins:maven-help-plugin:jar:3.5.1 has not been downloaded from it before.'; exit 1 ;; esac; done\n"
	commands := countingMaven(t, p, model, prelude)
	build, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if build.Status != bc.Complete {
		t.Fatal(build.Diagnostics)
	}
	got := commands()
	if len(got) != 5 {
		t.Fatalf("expected one offline attempt and four online stages, got %q", got)
	}
	if !strings.HasPrefix(got[0], "-o ") {
		t.Fatalf("first attempt was not offline: %s", got[0])
	}
	for _, command := range got[1:] {
		if strings.HasPrefix(command, "-o ") || strings.Contains(command, " -o ") {
			t.Fatalf("stage retried offline after an offline failure: %s", command)
		}
	}
	// A genuine failure is final: no second identical run.
	p2, r2, model2 := observedFixture(t)
	commands2 := countingMaven(t, p2, model2, "echo '[ERROR] COMPILATION ERROR'; exit 1\n")
	if _, err = p2.Build(context.Background(), r2); err == nil || !strings.Contains(err.Error(), "COMPILATION ERROR") {
		t.Fatalf("unexpected result: %v", err)
	}
	if got := commands2(); len(got) != 1 {
		t.Fatalf("failed stage was retried: %q", got)
	}
}

func TestRetainedJARIsNeitherReadNorCopiedAgain(t *testing.T) {
	p, r, model := observedFixture(t)
	first, err := p.BuildObserved(context.Background(), r, model)
	if err != nil {
		t.Fatal(err)
	}
	jar := filepath.Join(p.config.CacheDir, "repository/external/library/2/library-2.jar")
	objects, err := filepath.Glob(filepath.Join(p.config.CacheDir, "objects", "*.jar"))
	if err != nil || len(objects) != 1 {
		t.Fatalf("objects: %v %v", objects, err)
	}
	info, err := os.Stat(objects[0])
	if err != nil {
		t.Fatal(err)
	}
	// Neither the repository JAR nor its retained object may be read again.
	for _, name := range []string{jar, objects[0]} {
		if err = os.Chmod(name, 0); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = os.Chmod(name, 0600) })
	}
	if f, err := os.Open(jar); err == nil {
		f.Close()
		t.Skip("running with privileges that ignore file permissions")
	}
	second, err := p.BuildObserved(context.Background(), r, model)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(first, second) {
		t.Fatal("observation without re-reading JARs changed the context")
	}
	again, err := os.Stat(objects[0])
	if err != nil || !again.ModTime().Equal(info.ModTime()) {
		t.Fatal("retained object was rewritten", err)
	}
	if temps, _ := filepath.Glob(filepath.Join(p.config.CacheDir, "objects", "jar-*")); len(temps) != 0 {
		t.Fatalf("temporary copies left behind: %v", temps)
	}
}

func TestBuildFingerprintCoversBuildFilesOnly(t *testing.T) {
	p, r, _ := observedFixture(t)
	ctx := context.Background()
	base, err := p.buildFingerprint(ctx, r)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(r.Checkout.Path, "app", "src", "main", "java", "B.java"), []byte("class B {}"), 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.MkdirAll(filepath.Join(r.Checkout.Path, "target", "generated"), 0700); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(r.Checkout.Path, "target", "generated", "pom.xml"), []byte("<project/>"), 0600); err != nil {
		t.Fatal(err)
	}
	same, err := p.buildFingerprint(ctx, r)
	if err != nil || same != base {
		t.Fatal("sources or build outputs changed the build-file fingerprint", err)
	}
	if err = os.WriteFile(filepath.Join(r.Checkout.Path, "lib", "pom.xml"), []byte("<project><groupId>example</groupId><artifactId>lib</artifactId><version>2</version></project>"), 0600); err != nil {
		t.Fatal(err)
	}
	changed, err := p.buildFingerprint(ctx, r)
	if err != nil || changed == base {
		t.Fatal("POM change did not change the fingerprint", err)
	}
	other, err := New(Config{JavaHome: p.config.JavaHome, MavenExecutable: p.config.MavenExecutable, CacheDir: p.config.CacheDir, WorkDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	if again, err := other.buildFingerprint(ctx, r); err != nil || again != changed {
		t.Fatal("fingerprint is not a pure function of build files and toolchain", err)
	}
	entries := filepath.Join(p.config.CacheDir, "buildcontext")
	for _, id := range []string{"github.com/apache/dubbo", "../escape", "..", "a:b\\c", strings.Repeat("x", 300)} {
		relative, err := filepath.Rel(entries, p.entryPath(id, changed))
		parts := strings.Split(relative, string(filepath.Separator))
		if err != nil || len(parts) != 2 || parts[0] == ".." || parts[0] == "." || strings.ContainsAny(relative, ":\\") || len(parts[0]) > 100 {
			t.Fatalf("repository identifier %q is not sanitized in entry path %q", id, relative)
		}
	}
	if p.entryPath("github.com/apache/dubbo", changed) == p.entryPath("github.com_apache_dubbo", changed) {
		t.Fatal("distinct repositories collide in entry paths")
	}
}

// Opt-in: prepares a small two-module reactor with the real Maven and JDK
// twice, then once more in a fresh worktree, and reports the measured cost
// of each run. The second and third runs must execute no Maven at all.
func TestRealMavenPreparationIsCachedAcrossRuns(t *testing.T) {
	javaHome, mvn := os.Getenv("CODEGRAPH_TEST_JAVA_HOME"), os.Getenv("CODEGRAPH_TEST_MAVEN")
	if javaHome == "" || mvn == "" {
		t.Skip("set CODEGRAPH_TEST_JAVA_HOME and CODEGRAPH_TEST_MAVEN to run Maven")
	}
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	write := func(name, body string) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(name), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(name, []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
	}
	pom := func(artifact, extra string) string {
		return `<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><parent><groupId>example</groupId><artifactId>parent</artifactId><version>1</version></parent><artifactId>` + artifact + `</artifactId><dependencies>` + extra + `<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies></project>`
	}
	write(filepath.Join(root, "pom.xml"), `<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>parent</artifactId><version>1</version><packaging>pom</packaging><properties><maven.compiler.release>17</maven.compiler.release><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties><modules><module>lib</module><module>app</module></modules></project>`)
	write(filepath.Join(root, "lib", "pom.xml"), pom("lib", `<dependency><groupId>org.apache.commons</groupId><artifactId>commons-lang3</artifactId><version>3.14.0</version></dependency>`))
	write(filepath.Join(root, "app", "pom.xml"), pom("app", `<dependency><groupId>example</groupId><artifactId>lib</artifactId><version>1</version></dependency>`))
	write(filepath.Join(root, "lib", "src", "main", "java", "lib", "Lib.java"), "package lib; public class Lib { public static String hi() { return org.apache.commons.lang3.StringUtils.capitalize(\"hi\"); } }")
	write(filepath.Join(root, "lib", "src", "test", "java", "lib", "LibTest.java"), "package lib; public class LibTest {}")
	write(filepath.Join(root, "app", "src", "main", "java", "app", "App.java"), "package app; public class App { public static void main(String[] a) { System.out.println(lib.Lib.hi()); } }")
	write(filepath.Join(root, "app", "src", "test", "java", "app", "AppTest.java"), "package app; public class AppTest {}")
	log := filepath.Join(t.TempDir(), "commands")
	wrapper := filepath.Join(t.TempDir(), "mvn")
	write(wrapper, "#!/bin/sh\nprintf '%s\\n' \"$*\" >> "+quoteShell(log)+"\nexec "+quoteShell(mvn)+" \"$@\"\n")
	if err = os.Chmod(wrapper, 0700); err != nil {
		t.Fatal(err)
	}
	p, err := New(Config{JavaHome: javaHome, MavenExecutable: wrapper, CacheDir: t.TempDir(), WorkDir: t.TempDir(), Timeout: 20 * time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	invocations := func() int {
		body, _ := os.ReadFile(log)
		return len(strings.Split(strings.TrimSpace(string(body)), "\n")) - map[bool]int{true: 1, false: 0}[strings.TrimSpace(string(body)) == ""]
	}
	request := bc.Request{Checkout: bc.Checkout{Path: root, RepositoryID: "example/reactor", SnapshotID: strings.Repeat("c", 40)}, Limits: bc.DefaultLimits()}
	started := time.Now()
	first, err := p.Build(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	cold := time.Since(started)
	coldInvocations := invocations()
	if first.Status != bc.Complete || coldInvocations < 4 {
		t.Fatalf("status=%s invocations=%d diagnostics=%+v", first.Status, coldInvocations, first.Diagnostics)
	}
	started = time.Now()
	second, err := p.Build(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	warm := time.Since(started)
	if invocations() != coldInvocations || !reflect.DeepEqual(first, second) {
		t.Fatalf("second run executed Maven (%d invocations) or changed the context", invocations()-coldInvocations)
	}
	fresh, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	err = filepath.WalkDir(root, func(name string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		relative, _ := filepath.Rel(root, name)
		if entry.IsDir() {
			if entry.Name() == "target" {
				return filepath.SkipDir
			}
			return os.MkdirAll(filepath.Join(fresh, relative), 0700)
		}
		body, err := os.ReadFile(name)
		if err != nil {
			return err
		}
		return os.WriteFile(filepath.Join(fresh, relative), body, 0600)
	})
	if err != nil {
		t.Fatal(err)
	}
	request.Checkout.Path = fresh
	started = time.Now()
	third, err := p.Build(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	restored := time.Since(started)
	if invocations() != coldInvocations || !reflect.DeepEqual(first, third) {
		t.Fatalf("fresh worktree executed Maven (%d invocations) or changed the context", invocations()-coldInvocations)
	}
	if _, err = os.Stat(filepath.Join(fresh, "lib", "target", "classes", "lib", "Lib.class")); err != nil {
		t.Fatal("compiled output not restored:", err)
	}
	// Reactor sibling visibility is source-set output, external JARs are pinned objects.
	sourceSetEntries, jarEntries := 0, 0
	for _, set := range first.Inventory.SourceSets {
		for _, entry := range set.Classpath {
			switch entry.Kind {
			case bc.EntrySourceSet:
				sourceSetEntries++
			case bc.EntryArtifact:
				jarEntries++
			}
		}
	}
	if sourceSetEntries == 0 || jarEntries == 0 {
		t.Fatalf("unexpected classpath shapes: %d source-set entries, %d artifacts", sourceSetEntries, jarEntries)
	}
	t.Logf("cold run: %s with %d Maven invocations; same-worktree hit: %s; fresh-worktree hit with restore: %s; modules=%d source_sets=%d artifacts=%d", cold.Round(time.Millisecond), coldInvocations, warm.Round(time.Millisecond), restored.Round(time.Millisecond), len(first.Inventory.Modules), len(first.Inventory.SourceSets), len(first.Inventory.Artifacts))
}
