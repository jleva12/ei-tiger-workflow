package typescript

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/graphanalysis"
)

// requireCompiler skips without node or a built bridge, unless the tests
// are required to run.
func requireCompiler(t *testing.T) Compiler {
	t.Helper()
	_, nodeErr := exec.LookPath("node")
	bridge := AnalyzerPath("")
	if _, err := os.Stat(bridge); nodeErr != nil || err != nil {
		if os.Getenv("CODEGRAPH_REQUIRE_TS_COMPILER") == "1" {
			t.Fatal("build apps/forge-codegraph-worker/typescript-analyzer first")
		}
		t.Skip("requires node and npm ci && npm run build in apps/forge-codegraph-worker/typescript-analyzer")
	}
	return Compiler{AnalyzerPath: bridge}
}

func (f *fixture) resolveWith(c Compiler) semantic.ResolutionResult {
	f.t.Helper()
	result, err := NewWithCompiler(c).Resolve(context.Background(), f.req, f.w)
	must(f.t, err)
	return result
}

func writeTree(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for name, text := range files {
		p := filepath.Join(root, filepath.FromSlash(name))
		must(t, os.MkdirAll(filepath.Dir(p), 0o700))
		must(t, os.WriteFile(p, []byte(text), 0o600))
	}
}

const repoSource = `export class Owner {
  name = "o";
  greet() { return this.name; }
}
export class Repo {
  find(id: string) { return { id, title: "x", owner: new Owner() }; }
}
export function makeRepo() { return new Repo(); }
`

const useSource = `import { makeRepo } from "./repo";
import { create } from "fakepkg";
const repo = makeRepo();
const item = repo.find("1");
item.owner.greet();
create().get("/x").then((r) => r.data);
console.log(item.title);
`

// The compiler binds what the syntax tier cannot type: a member through
// inferred return types, a package's members through its installed
// declarations, the platform's by their declaring interface.
func TestCompilerBindsWhatSyntaxCannot(t *testing.T) {
	c := requireCompiler(t)
	f := newFixture(t)
	installation := t.TempDir()
	writeTree(t, installation, map[string]string{
		"node_modules/fakepkg/package.json": `{"name":"fakepkg","types":"index.d.ts"}`,
		"node_modules/fakepkg/index.d.ts":   "export interface Reply { data: string }\nexport interface Client { get(path: string): Promise<Reply> }\nexport declare function create(): Client;\n",
	})
	f.configure(Settings{Installs: []Install{{Dir: ".", Path: installation, SHA256: strings.Repeat("0", 64)}}})
	f.add("repo", "src/repo.ts", repoSource, true, true)
	f.add("use", "src/use.ts", useSource, true, true)
	result := f.resolveWith(c)
	if len(result.Warnings) > 0 || len(result.Degraded) > 0 {
		t.Fatalf("compiler degraded: %+v", result)
	}
	greet := f.lookup("use", "item.owner.greet()", 0)
	if greet.Status != semantic.LookupResolved || greet.Provenance != "compiler" || greet.SelectedSymbolID != f.symbol("repo", ir.DeclarationMethod, "greet") {
		t.Fatalf("greet: %+v", greet)
	}
	external := func(text string, kind string) {
		t.Helper()
		var l semantic.Lookup
		for _, candidate := range f.w.lookups[ir.FileID("use")] {
			span := candidate.Evidence.Span
			if string(f.bytes["use"][span.Start.ByteOffset:span.End.ByteOffset]) == text {
				l = candidate
			}
		}
		sym := f.w.symbols[l.SelectedSymbolID]
		if l.Status != semantic.LookupResolved || l.Provenance != "compiler" || sym.Key == nil || sym.Key.CanonicalSignature != kind {
			t.Fatalf("%s: %+v %+v", text, l, sym)
		}
	}
	external(`create().get("/x")`, "fakepkg#Client.get")
	external("r.data", "fakepkg#Reply.data")
	log := f.lookup("use", "console.log(item.title)", 0)
	if sym := f.w.symbols[log.SelectedSymbolID]; log.Provenance != "compiler" || sym.Intrinsic == nil || sym.Intrinsic.Name != "Console.log" {
		t.Fatalf("console.log: %+v %+v", log, sym)
	}
	// A member of a returned object literal has no declaration of its own:
	// it is derived from the method returning it.
	title := f.lookup("use", "item.title", 0)
	sym := f.w.symbols[title.SelectedSymbolID]
	if title.Status != semantic.LookupResolved || sym.Derived == nil || sym.Key.CanonicalSignature != "src/repo#Repo.find(id).title" || sym.OwnerSymbolID != f.symbol("repo", ir.DeclarationMethod, "find") {
		t.Fatalf("item.title: %+v %+v", title, sym)
	}
	// The projector takes every symbol the compiler named.
	_, err := (graphanalysis.Matcher{}).Match(context.Background(), semantic.MatchRequest{Run: f.req.Run, Files: f.w.files}, f.w)
	must(t, err)
	derived := 0
	_, err = (graphanalysis.Projector{}).Project(context.Background(), semantic.ProjectRequest{Run: f.req.Run, Files: f.w.files, SyntaxLimits: f.req.SyntaxLimits}, f.w, func(_ context.Context, fact graph.Fact) error {
		if fact.Node != nil && fact.Node.Kind == "derived_field" {
			derived++
		}
		return fact.Validate()
	})
	must(t, err)
	if derived == 0 {
		t.Fatal("the derived member was not projected")
	}
}

// A bridge that stops part way leaves the sites it had not answered to the
// syntax tier, says so, and has the context resolved again.
func TestCompilerThatStopsPartWayDegrades(t *testing.T) {
	requireCompiler(t)
	bridge := filepath.Join(t.TempDir(), "bridge.cjs")
	must(t, os.WriteFile(bridge, []byte(`const fs = require('fs');
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
const first = request.files.find(f => f.sites && f.sites.length);
process.stdout.write(JSON.stringify({ event: 'hello', protocol: 1, context: request.context }) + '\n');
process.stdout.write(JSON.stringify({ event: 'lookup', file: first.id, site: first.sites[0].id, targets: [{ module: 'lib', name: 'x', qualified: 'Platform.x' }] }) + '\n');
process.exit(3);
`), 0o600))
	f := newFixture(t)
	f.add("repo", "src/repo.ts", repoSource, true, true)
	f.add("use", "src/use.ts", useSource, true, true)
	result := f.resolveWith(Compiler{AnalyzerPath: bridge})
	if len(result.Warnings) != 1 || !strings.Contains(result.Warnings[0], "the bridge failed") || len(result.Degraded) != 1 || result.Degraded[0] != setID {
		t.Fatalf("result: %+v", result)
	}
	compiled := 0
	for _, lookups := range f.w.lookups {
		for _, l := range lookups {
			if l.Provenance == "compiler" {
				compiled++
			}
		}
	}
	if compiled != 1 {
		t.Fatalf("%d compiler bindings, want the one answered", compiled)
	}
	if l := f.lookup("use", "makeRepo()", 0); l.Provenance != "syntax" || l.Status != semantic.LookupResolved {
		t.Fatalf("an unanswered site lost its syntax binding: %+v", l)
	}
}

func TestMissingBridgeDegrades(t *testing.T) {
	f := newFixture(t)
	f.add("repo", "src/repo.ts", repoSource, true, true)
	result := f.resolveWith(Compiler{AnalyzerPath: filepath.Join(t.TempDir(), "missing.cjs")})
	if len(result.Warnings) != 1 || !strings.Contains(result.Warnings[0], "not built") || len(result.Degraded) != 1 {
		t.Fatalf("result: %+v", result)
	}
}

func TestCompilerAnswersBecomeBindings(t *testing.T) {
	l := semantic.Lookup{Status: semantic.LookupUnresolved, Cause: semantic.CauseAnalysisLimitation, Reason: "receiver_type_unknown", Provenance: "syntax"}
	if applyCompiler(&l, nil, true) || l.Provenance != "syntax" {
		t.Fatalf("no symbol changed the lookup: %+v", l)
	}
	if applyCompiler(&l, []string{"a"}, true) {
		t.Fatal("a target the IR does not declare was taken as the only one")
	}
	if !applyCompiler(&l, []string{"b", "a", "b"}, false) || l.Status != semantic.LookupAmbiguous || len(l.CandidateIDs) != 2 || l.CandidateRole != "binding_alternative" {
		t.Fatalf("union: %+v", l)
	}
	if !applyCompiler(&l, []string{"a"}, false) || l.Status != semantic.LookupResolved || l.SelectedSymbolID != "a" || l.Cause != "" || l.Reason != "" || l.Provenance != "compiler" {
		t.Fatalf("resolved: %+v", l)
	}
}
