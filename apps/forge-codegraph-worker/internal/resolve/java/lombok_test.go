package java

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
)

// testLombok is a Lombok JAR that runs on the test JDK.
func testLombok(t *testing.T, name string) string {
	t.Helper()
	jar := os.Getenv(name)
	if jar == "" {
		t.Skip("set " + name + " to a Lombok JAR")
	}
	return jar
}

var (
	lombokBase = []byte(`package p;
@lombok.experimental.SuperBuilder
@lombok.Getter
public abstract class Base {
    private String id;
}
`)
	lombokPoint = []byte(`package p;
import lombok.*;
@AllArgsConstructor @Data
public class Point {
    private final String host;
    private final int port;
}
`)
	lombokUse = []byte(`package p;
class Use {
    String host(Point p) { return p.getHost() + p.getPort(); }
    Point make() { return new Point("h", 1); }
    String id(Base b) { return b.getId(); }
    @SuppressWarnings("unchecked")
    <V extends Base.BaseBuilder<?, ?>> V withId(V builder) { return (V) builder.id("x"); }
}
`)
)

// lombokFixture is the main source set of a module with jar, as the
// project's Lombok, on its classpath.
func lombokFixture(t *testing.T, home, jar string) *fixture {
	t.Helper()
	var f *fixture
	f = newFixture(t, home, func(in *bc.Inventory, checks *[]bc.InputCheck) {
		in.Inputs = append(in.Inputs, bc.Input{ID: "lombok-jar", Kind: bc.InputJAR, Location: &bc.Location{Root: "checkout", Path: "lib/lombok.jar"}, SHA256: strings.Repeat("0", 64)})
		*checks = append(*checks, bc.InputCheck{InputID: "lombok-jar", Status: bc.Available, ObservedSHA256: strings.Repeat("0", 64)})
		in.Artifacts = append(in.Artifacts, bc.Artifact{ID: "lombok", Coordinates: bc.Coordinates{Group: "org.projectlombok", Name: "lombok", Version: "1", Extension: "jar"}, BinaryInputID: "lombok-jar"})
		in.SourceSets[0].Classpath = append(in.SourceSets[0].Classpath, bc.PathEntry{Kind: bc.EntryArtifact, RefID: "lombok"})
	})
	body, err := os.ReadFile(jar)
	must(t, err)
	must(t, os.MkdirAll(filepath.Join(f.checkout, "lib"), 0o700))
	must(t, os.WriteFile(filepath.Join(f.checkout, "lib", "lombok.jar"), body, 0o600))
	fp, err := manifest.Fingerprint(context.Background(), f.checkout, "lib/lombok.jar", bc.InputJAR, bc.DefaultLimits())
	must(t, err)
	for i := range f.build.Inventory.Inputs {
		if f.build.Inventory.Inputs[i].ID == "lombok-jar" {
			f.build.Inventory.Inputs[i].SHA256 = fp
		}
	}
	f.w.build = f.build
	f.addParsed("Base", "main", "src/p/Base.java", lombokBase, true)
	f.addParsed("Point", "main", "src/p/Point.java", lombokPoint, true)
	f.addParsed("Use", "main", "src/p/Use.java", lombokUse, true)
	return f
}

// lombokMember checks that the call text resolves to a Lombok-derived
// symbol named name, owned by the symbol owner, contributed by the source
// type contributor.
func lombokMember(t *testing.T, f *fixture, kind semantic.LookupKind, text, name, owner, contributor string) semantic.Symbol {
	t.Helper()
	l := f.occurrenceLookup("Use", kind, text, lombokUse)
	sym := f.w.symbols[l.SelectedSymbolID]
	if l.Status != semantic.LookupResolved || sym.Derived == nil || sym.Derived.Rule != lombokRule || sym.Name != name || sym.OwnerSymbolID != owner || len(sym.Derived.SourceSymbolIDs) != 1 || sym.Derived.SourceSymbolIDs[0] != contributor {
		t.Fatalf("%s: %+v -> %+v (derived %+v)", text, l, sym, sym.Derived)
	}
	return sym
}

// Lombok runs in the bridge when the set's classpath has it: calls to the
// getters, constructors and builders it generates bind to derived symbols of
// the annotated type instead of staying unresolved.
func TestLombokMembersResolveToDerivedSymbols(t *testing.T) {
	home := testJDK(t)
	jar := testLombok(t, "CODEGRAPH_TEST_LOMBOK_JAR")
	f := lombokFixture(t, home, jar)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unresolved != 0 || result.Unsupported != 0 || len(result.Warnings) != 0 {
		for _, id := range []string{"Base", "Point", "Use"} {
			for _, l := range f.lookups(id) {
				if l.Status != semantic.LookupResolved {
					t.Logf("%s %+v", id, l)
				}
			}
		}
		t.Fatalf("result: %+v", result)
	}
	point := symbolID("Point", f.declaration("Point", ir.DeclarationClass, "Point").ID)
	base := symbolID("Base", f.declaration("Base", ir.DeclarationClass, "Base").ID)
	lombokMember(t, f, semantic.LookupCall, `p.getHost()`, "getHost", point, point)
	lombokMember(t, f, semantic.LookupCall, `p.getPort()`, "getPort", point, point)
	lombokMember(t, f, semantic.LookupCall, `new Point("h", 1)`, "<init>", point, point)
	lombokMember(t, f, semantic.LookupCall, `b.getId()`, "getId", base, base)
	// The builder Lombok generates inside Base owns its setters.
	builder := lombokMember(t, f, semantic.LookupType, `Base.BaseBuilder<?, ?>`, "BaseBuilder", base, base)
	if builder.Key == nil || builder.Key.CanonicalSignature != "p.Base.BaseBuilder" || builder.Key.Kind != ir.DeclarationClass {
		t.Fatalf("builder key: %+v", builder.Key)
	}
	lombokMember(t, f, semantic.LookupCall, `builder.id("x")`, "id", builder.ID, base)
	// Code Lombok generated is in no source file: the only declarations
	// the sources report are their own.
	for _, id := range []string{"Base", "Point"} {
		for _, l := range f.lookups(id) {
			if l.Evidence.Span.End.ByteOffset > uint64(len(f.w.bytes[ir.FileID(id)])) {
				t.Fatalf("%s lookup outside the source: %+v", id, l)
			}
		}
	}
}

// A project's Lombok that cannot run on the service JDK is skipped for the
// service's; with neither, the run goes on with the calls unresolved and a
// warning says why.
func TestLombokThatCannotRunFallsBackThenWarns(t *testing.T) {
	home := testJDK(t)
	jar := testLombok(t, "CODEGRAPH_TEST_LOMBOK_JAR")
	old := testLombok(t, "CODEGRAPH_TEST_OLD_LOMBOK_JAR")
	f := lombokFixture(t, home, old)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir(), LombokJAR: jar})
	must(t, err)
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unresolved != 0 || len(result.Warnings) != 0 {
		t.Fatalf("with the service's Lombok: %+v", result)
	}
	point := symbolID("Point", f.declaration("Point", ir.DeclarationClass, "Point").ID)
	lombokMember(t, f, semantic.LookupCall, `p.getHost()`, "getHost", point, point)

	f = lombokFixture(t, home, old)
	r, err = New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	result, err = r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unresolved == 0 || len(result.Warnings) != 1 || !strings.Contains(result.Warnings[0], "Lombok could not run") || !strings.Contains(result.Warnings[0], "lombok_jar") {
		t.Fatalf("without a Lombok that runs: %+v", result)
	}
	if l := f.occurrenceLookup("Use", semantic.LookupCall, `p.getHost()`, lombokUse); l.Status != semantic.LookupUnresolved {
		t.Fatalf("getHost without Lombok: %+v", l)
	}
}

// Another source set sees the producer's Lombok members in its compiled
// output, where nothing says which field they came from. They map to the
// same derived symbols as references inside the producer.
func TestLombokMembersInReactorBytecode(t *testing.T) {
	home := testJDK(t)
	jar := testLombok(t, "CODEGRAPH_TEST_LOMBOK_JAR")
	consumer := []byte(`package p;
class Consumer { String host() { return new Point("h", 1).getHost(); } }
`)
	f := lombokFixture(t, home, jar)
	output := filepath.Join(f.checkout, "target", "classes")
	must(t, os.MkdirAll(output, 0o700))
	src := filepath.Join(t.TempDir(), "Point.java")
	must(t, os.WriteFile(src, lombokPoint, 0o600))
	if body, err := exec.Command(filepath.Join(home, "bin", "javac"), "-processorpath", jar, "-cp", jar, "-d", output, src).CombinedOutput(); err != nil {
		t.Fatalf("fixture bytecode: %v %s", err, body)
	}
	fp, err := manifest.Fingerprint(context.Background(), f.checkout, "target/classes", bc.InputClasses, bc.DefaultLimits())
	must(t, err)
	f.build.Inventory.Inputs = append(f.build.Inventory.Inputs, bc.Input{ID: "main-output", Kind: bc.InputClasses, Location: &bc.Location{Root: "checkout", Path: "target/classes"}, SHA256: fp})
	f.build.Checks = append(f.build.Checks, bc.InputCheck{InputID: "main-output", Status: bc.Available, ObservedSHA256: fp})
	f.build.Inventory.SourceSets[0].OutputInputID = "main-output"
	f.w.build = f.build
	f.addParsed("Consumer", "test", "src/p/Consumer.java", consumer, true)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unresolved != 0 || result.Unsupported != 0 {
		for _, l := range f.lookups("Consumer") {
			if l.Status != semantic.LookupResolved {
				t.Logf("%+v", l)
			}
		}
		t.Fatalf("result: %+v", result)
	}
	point := symbolID("Point", f.declaration("Point", ir.DeclarationClass, "Point").ID)
	inside := lombokMember(t, f, semantic.LookupCall, `p.getHost()`, "getHost", point, point)
	l := f.occurrenceLookup("Consumer", semantic.LookupCall, `new Point("h", 1).getHost()`, consumer)
	if l.SelectedSymbolID != inside.ID {
		t.Fatalf("bytecode getHost -> %+v, want %s", f.w.symbols[l.SelectedSymbolID], inside.ID)
	}
	constructor := lombokMember(t, f, semantic.LookupCall, `new Point("h", 1)`, "<init>", point, point)
	if l := f.occurrenceLookup("Consumer", semantic.LookupCall, `new Point("h", 1)`, consumer); l.SelectedSymbolID != constructor.ID {
		t.Fatalf("bytecode constructor -> %+v, want %s", f.w.symbols[l.SelectedSymbolID], constructor.ID)
	}
}

// With a processor, javac leaves an erroneous generic call's arguments
// unattributed; reading their element attributes them and reports the errors
// while the bridge walks the unit. They are written with the unit, not after
// every unit, where the resolver would reject a second group for the file.
func TestErrorsReportedWhileWalkingStayWithTheirFile(t *testing.T) {
	home := testJDK(t)
	jar := testLombok(t, "CODEGRAPH_TEST_LOMBOK_JAR")
	late := []byte(`package p;
import q.Missing;
class Late {
    private final Missing a = java.util.Objects.requireNonNull(make(Missing.class));
    static <V> V make(Class<V> type) { return null; }
}
`)
	f := lombokFixture(t, home, jar)
	f.addParsed("Late", "main", "src/p/Late.java", late, true)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unresolved == 0 {
		t.Fatalf("the missing type resolved: %+v", result)
	}
	if l := f.occurrenceLookup("Late", semantic.LookupType, `Missing`, late); l.Status != semantic.LookupUnresolved {
		t.Fatalf("Missing: %+v", l)
	}
}
