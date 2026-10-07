package java

import (
	"context"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/graphanalysis"
)

var (
	producerSource = []byte(`package p; public class A {
 public int run(String value){return 1;}
 public int run(int value){return 2;}
}`)
	consumerSource = []byte(`package p; class B { int work(){ return new A().run("hello"); } }`)
)

// noMainOutput is a fixture whose build left main without compiled output,
// as when Maven could not compile it; its tests still see main.
func noMainOutput(t *testing.T, home string, producer []byte) *fixture {
	t.Helper()
	f := newFixture(t, home, func(in *bc.Inventory, _ *[]bc.InputCheck) {
		in.MissingInputs = append(in.MissingInputs, bc.MissingInput{ID: "main-output", Requested: bc.GapCompiledOutput, Reason: "Maven could not compile it", ModuleID: "app", SourceSetID: "main"})
	})
	f.addParsed("A", "main", "src/p/A.java", producer, true)
	f.addParsed("B", "test", "src/p/B.java", consumerSource, true)
	return f
}

func runDeclaration(t *testing.T, f *fixture) string {
	t.Helper()
	for _, d := range f.files["A"].Declarations {
		if d.Kind == ir.DeclarationMethod && d.Name == "run" && d.Callable != nil && len(d.Callable.ParameterIDs) == 1 {
			for _, p := range f.files["A"].Declarations {
				if p.ID == d.Callable.ParameterIDs[0] && p.Variable != nil && string(producerSource[p.Span.Start.ByteOffset:p.Span.End.ByteOffset]) == "String value" {
					return symbolID("A", d.ID)
				}
			}
		}
	}
	t.Fatal("no run(String)")
	return ""
}

// The build left main without classes, but it compiles: the resolver writes
// its classes for the tests, whose calls into main bind to its sources.
func TestMissingBuildOutputIsCompiledForDependents(t *testing.T) {
	home := testJDK(t)
	f := noMainOutput(t, home, producerSource)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unresolved != 0 || result.Unsupported != 0 || len(result.CompiledOutputs) != 1 || result.CompiledOutputs[0] != "main" {
		for _, l := range f.lookups("B") {
			if l.Status != semantic.LookupResolved {
				t.Logf("%+v", l)
			}
		}
		t.Fatalf("result: %+v", result)
	}
	if l := f.occurrenceLookup("B", semantic.LookupCall, `new A().run("hello")`, consumerSource); l.SelectedSymbolID != runDeclaration(t, f) || len(l.CandidateIDs) != 2 {
		t.Fatalf("call into compiled main: %+v", l)
	}
}

// Main that does not compile gets no classes; the tests' lookups into it
// stay unresolved and the run goes on.
func TestMissingBuildOutputThatDoesNotCompileStaysUnresolved(t *testing.T) {
	home := testJDK(t)
	broken := []byte(`package p; public class A {
 public int run(String value){return missing();}
 public int run(int value){return 2;}
}`)
	f := noMainOutput(t, home, broken)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if len(result.CompiledOutputs) != 0 {
		t.Fatalf("broken main compiled: %+v", result)
	}
	if l := f.occurrenceLookup("B", semantic.LookupCall, `new A().run("hello")`, consumerSource); l.Status == semantic.LookupResolved {
		t.Fatalf("call into main without classes resolved: %+v", l)
	}
}

// Only the tests changed: main is compiled for its classes alone, and the
// tests' calls bind to main's declarations from the previous generation.
func TestMissingBuildOutputIsCompiledWhenOnlyDependentsChange(t *testing.T) {
	home := testJDK(t)
	first := noMainOutput(t, home, producerSource)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	_, err = r.Resolve(context.Background(), first.req, first.w)
	must(t, err)
	_, err = graphanalysis.Matcher{}.Match(context.Background(), semantic.MatchRequest{Run: first.req.Run, Files: first.w.files}, first.w)
	must(t, err)

	f := noMainOutput(t, home, producerSource)
	for i, in := range f.w.files {
		if in.Source.SourceSetID == "main" {
			f.w.files[i].Affected = false
			f.w.previous[in.Lineage] = first.w.current[in.Source.FileID]
		}
	}
	f.req.Contexts = []bc.SourceSetID{"test"}
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unresolved != 0 || result.Unsupported != 0 || len(result.CompiledOutputs) != 1 {
		for _, l := range f.lookups("B") {
			if l.Status != semantic.LookupResolved {
				t.Logf("%+v", l)
			}
		}
		t.Fatalf("result: %+v", result)
	}
	if l := f.occurrenceLookup("B", semantic.LookupCall, `new A().run("hello")`, consumerSource); l.SelectedSymbolID != runDeclaration(t, f) {
		t.Fatalf("call into main compiled only for classes: %+v", l)
	}
	if len(f.lookups("A")) != 0 {
		t.Fatal("main was attributed although only compiled for its classes")
	}
}

// One file of main does not compile, and nothing else needs it: main is
// compiled without it, the tests' calls into the rest bind, and the run
// names the file left out.
func TestMissingBuildOutputIsCompiledWithoutTheFilesThatFail(t *testing.T) {
	home := testJDK(t)
	f := noMainOutput(t, home, producerSource)
	broken := []byte(`package p; class Broken { int run(){ return missing(); } }`)
	uses := []byte(`package p; class UsesBroken { int run(){ return new Broken().run(); } }`)
	f.addParsed("Broken", "main", "src/p/Broken.java", broken, true)
	f.addParsed("UsesBroken", "main", "src/p/UsesBroken.java", uses, true)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if len(result.CompiledOutputs) != 1 || result.CompiledOutputs[0] != "main" {
		t.Fatalf("main was not compiled without its broken file: %+v", result)
	}
	if l := f.occurrenceLookup("B", semantic.LookupCall, `new A().run("hello")`, consumerSource); l.SelectedSymbolID != runDeclaration(t, f) {
		t.Fatalf("call into the compiled part of main: %+v", l)
	}
	named := false
	for _, w := range result.Warnings {
		named = named || (strings.Contains(w, "without the files that do not compile") && strings.Contains(w, "Broken.java") && strings.Contains(w, "UsesBroken.java"))
	}
	if !named {
		t.Fatalf("warnings: %q", result.Warnings)
	}
}
