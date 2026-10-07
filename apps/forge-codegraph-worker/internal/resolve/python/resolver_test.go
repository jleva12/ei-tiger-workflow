package python

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/graphanalysis"
	"ei-aitiger-codegraph/worker/internal/languages/python/environment"
)

func requireAnalyzer(t *testing.T) {
	t.Helper()
	if _, err := os.Stat(environment.AnalyzerPath("")); err != nil {
		if os.Getenv("CODEGRAPH_REQUIRE_PYTHON_TESTS") == "1" {
			t.Fatal("build apps/forge-codegraph-worker/python-analyzer first")
		}
		t.Skip("requires npm ci && npm run build in apps/forge-codegraph-worker/python-analyzer")
	}
}
func (f *fixture) resolve() semantic.ResolutionResult {
	f.t.Helper()
	requireAnalyzer(f.t)
	r, err := New(Config{}).Resolve(context.Background(), f.req, f.w)
	must(f.t, err)
	return r
}

const stores = `class SqlStore:
    def save(self, value: str = "") -> str:
        return value

class MemoryStore:
    def save(self, value: str = "") -> str:
        return value

def factory() -> SqlStore:
    return SqlStore()
`
const app = `from store import SqlStore, MemoryStore, factory as make
from pathlib import Path

def direct() -> str:
    return make().save("😀")

def persist(store: SqlStore | MemoryStore) -> str:
    return store.save()

def narrowed(store: SqlStore | MemoryStore) -> str:
    if isinstance(store, SqlStore):
        return store.save()
    return ""

def unknown(value):
    return value.save()

def external():
    return Path("x").exists()
`

func TestPyrightCrossFileInferenceUnionAndExternal(t *testing.T) {
	f := newFixture(t)
	f.add("store", "store.py", stores, true, true)
	f.add("app", "app.py", app, true, true)
	r := f.resolve()
	if r.Resolved == 0 || r.Ambiguous == 0 || r.Unresolved == 0 {
		t.Fatalf("resolution: %+v", r)
	}
	check := func(text, want string) {
		t.Helper()
		l := f.lookup("app", text, 0)
		if l.Status != semantic.LookupResolved || l.SelectedSymbolID != want {
			t.Fatalf("%s: %+v", text, l)
		}
	}
	check("make()", f.symbol("store", ir.DeclarationFunction, "factory"))
	check("make().save(\"😀\")", f.symbol("store", ir.DeclarationMethod, "save"))
	union := f.lookup("app", "store.save()", 0)
	if union.Status != semantic.LookupAmbiguous || len(union.CandidateIDs) != 2 || union.CandidateRole != "binding_alternative" {
		t.Fatalf("union: %+v", union)
	}
	narrowed := f.lookup("app", "store.save()", 1)
	if narrowed.Status != semantic.LookupResolved || narrowed.SelectedSymbolID != f.symbol("store", ir.DeclarationMethod, "save") {
		t.Fatalf("narrowing: %+v", narrowed)
	}
	if l := f.lookup("app", "value.save()", 0); l.Status == semantic.LookupResolved || !l.HasUnknownCandidates {
		t.Fatalf("unknown guessed: %+v", l)
	}
	l := f.lookup("app", "Path(\"x\").exists()", 0)
	if l.Status != semantic.LookupResolved || f.w.symbols[l.SelectedSymbolID].External == nil {
		t.Fatalf("external: %+v", l)
	}
	// Verify real projection, including persistent candidate IDs and no exact
	// calls edge for the ambiguous occurrence.
	_, err := (graphanalysis.Matcher{}).Match(context.Background(), semantic.MatchRequest{Run: f.req.Run, Files: f.w.files}, f.w)
	must(t, err)
	var candidateNodes, callEdges int
	_, err = (graphanalysis.Projector{}).Project(context.Background(), semantic.ProjectRequest{Run: f.req.Run, Files: f.w.files, SyntaxLimits: f.req.SyntaxLimits}, f.w, func(_ context.Context, fact graph.Fact) error {
		if err := fact.Validate(); err != nil {
			return err
		}
		if n := fact.Node; n != nil && graph.Text(n.Properties, "status") == "ambiguous" {
			v := n.Properties["candidate_target_ids"]
			if v.Strings == nil || len(*v.Strings) != 2 {
				return errors.New("candidate IDs dropped")
			}
			for _, id := range *v.Strings {
				if strings.HasPrefix(id, "python-symbol") {
					return errors.New("run-local symbol ID leaked")
				}
			}
			candidateNodes++
		}
		if fact.Edge != nil && fact.Edge.Kind == graph.EdgeCalls {
			callEdges++
		}
		return nil
	})
	must(t, err)
	if candidateNodes == 0 || callEdges == 0 {
		t.Fatalf("projection: %d candidates, %d calls", candidateNodes, callEdges)
	}
}

// A call with an argument of the wrong type still calls that method: the
// error does not change the target. A site that binds nothing keeps the
// error as its reason.
func TestPyrightUnchangedSourceAndDiagnostics(t *testing.T) {
	f := newFixture(t)
	f.add("store", "store.py", stores, false, false)
	f.add("app", "app.py", "from store import factory\ndef run():\n    factory().missing()\n    return factory().save(1)\n", true, true)
	f.resolve()
	if l := f.lookup("app", "factory().save(1)", 0); l.Status != semantic.LookupResolved || f.w.symbols[l.SelectedSymbolID].Name != "save" {
		t.Fatalf("a call with a wrong argument lost its target: %+v", l)
	}
	if l := f.lookup("app", "factory().missing()", 0); l.Status == semantic.LookupResolved || l.DiagnosticCode == "" || l.Cause != semantic.CauseSourceDiagnostic {
		t.Fatalf("a call to nothing: %+v", l)
	}
	if l := f.lookup("app", "factory()", 0); l.Status != semantic.LookupResolved {
		t.Fatalf("unchanged source target: %+v", l)
	}
}

// Overloads of one function, and a name defined again in another branch,
// are one target: the implementation, or the last definition.
func TestPyrightOverloadsAreOneTarget(t *testing.T) {
	f := newFixture(t)
	f.add("app", "app.py", `import os
from typing import overload

@overload
def pick(value: int) -> int: ...
@overload
def pick(value: str) -> str: ...
def pick(value):
    return value

def run(flag: bool) -> None:
    pick(1)
    value = pick
    home = os.getenv("HOME")
    print(home)
`, true, true)
	f.resolve()
	for _, text := range []string{"pick(1)", "print(home)"} {
		if l := f.lookup("app", text, 0); l.Status != semantic.LookupResolved {
			t.Fatalf("%s: %+v", text, l)
		}
	}
	for _, l := range f.w.lookups["app"] {
		if l.Status == semantic.LookupAmbiguous {
			t.Fatalf("overloads left ambiguous: %+v", l)
		}
	}
}

func TestPyrightDependencyFingerprintAndCancellation(t *testing.T) {
	requireAnalyzer(t)
	f := newFixture(t)
	f.add("app", "app.py", "from library import work\nwork()\n", true, true)
	dep := t.TempDir()
	must(t, os.WriteFile(filepath.Join(dep, "library.pyi"), []byte("def work() -> str: ...\n"), 0600))
	hash, err := environment.DigestTree(context.Background(), dep, 1<<20)
	must(t, err)
	options, err := environment.Encode(environment.Settings{Version: "3.12", Platform: "Linux", Roots: []string{"."}, Dependencies: []environment.Dependency{{Path: dep, SHA256: hash}}})
	must(t, err)
	f.w.build.Inventory.SourceSets[0].LanguageOptions = options
	f.resolve()
	l := f.lookup("app", "work()", 0)
	if l.Status != semantic.LookupResolved || f.w.symbols[l.SelectedSymbolID].External == nil {
		t.Fatalf("stub: %+v", l)
	}
	must(t, os.WriteFile(filepath.Join(dep, "library.pyi"), []byte("def work() -> int: ...\n"), 0600))
	_, err = New(Config{}).Resolve(context.Background(), f.req, f.w)
	if !errors.Is(err, semantic.ErrIntegrity) {
		t.Fatalf("changed dependency accepted: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err = New(Config{Timeout: time.Second}).Resolve(ctx, f.req, f.w)
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestPyrightCallableIdentityAndPythonDispatch(t *testing.T) {
	f := newFixture(t)
	f.add("app", "app.py", `from typing import Any, Callable
from dataclasses import dataclass

def first() -> int: return 1
def second() -> int: return 2

def choose(flag: bool):
    fn = first if flag else second
    return fn()

class Base:
    def run(self) -> int: return 1

class Child(Base):
    def run(self) -> int: return super().run()

class Worker:
    def __call__(self) -> int: return 1

def invoke(worker: Worker): return worker()

def unknown_union(value: Base | Any): return value.run()

def callback(fn: Callable[[], int]): return fn()

@dataclass
class Record:
    name: str

def record(): return Record("x")
`, true, true)
	f.resolve()
	for _, l := range f.w.lookups["app"] {
		if l.Kind == semantic.LookupCall {
			t.Logf("call %q: %s %s candidates=%d unknown=%v", string(f.bytes["app"][l.Evidence.Span.Start.ByteOffset:l.Evidence.Span.End.ByteOffset]), l.Status, l.Reason, len(l.CandidateIDs), l.HasUnknownCandidates)
		}
	}
	if l := f.lookup("app", "fn()", 0); l.Status == semantic.LookupResolved {
		t.Fatalf("conditional callable identity collapsed: %+v", l)
	}
	if l := f.lookup("app", "fn()", 1); l.Status == semantic.LookupResolved {
		t.Fatalf("callable signature mistaken for implementation: %+v", l)
	}
	if l := f.lookup("app", "value.run()", 0); l.Status == semantic.LookupResolved {
		t.Fatalf("Any alternative ignored: %+v", l)
	}
	for _, s := range []string{"super().run()", "worker()", "Record(\"x\")"} {
		if l := f.lookup("app", s, 0); l.Status != semantic.LookupResolved {
			t.Fatalf("%s: %+v", s, l)
		}
	}
}

func TestPyrightModuleMembersInheritanceAndOverrides(t *testing.T) {
	f := newFixture(t)
	f.add("empty", "empty.py", "", true, true)
	f.add("base", "base.py", "class Parent[T]:\n    def run(self) -> int: return 1\n", true, true)
	f.add("app", "app.py", `import empty
import base
class Child(base.Parent[int]):
    def __init__(self):
        self.value = 1
    def run(self) -> int:
        return self.value
Child().run()
`, true, true)
	f.resolve()
	var module, derived, inherited, override bool
	for _, s := range f.w.symbols {
		module = module || (s.Module != nil && s.Module.FileID == "empty")
		derived = derived || (s.Derived != nil && s.Name == "value")
	}
	for _, l := range f.w.lookups["app"] {
		inherited = inherited || (l.Kind == semantic.LookupInheritance && l.Status == semantic.LookupResolved)
		override = override || (l.Kind == semantic.LookupOverride && l.Status == semantic.LookupResolved)
	}
	if !module || !derived || !inherited || !override {
		t.Fatalf("module=%v derived=%v inherited=%v override=%v", module, derived, inherited, override)
	}
	_, err := (graphanalysis.Matcher{}).Match(context.Background(), semantic.MatchRequest{Run: f.req.Run, Files: f.w.files}, f.w)
	must(t, err)
	nodes := map[string]*graph.Node{}
	var edges []*graph.Edge
	_, err = (graphanalysis.Projector{}).Project(context.Background(), semantic.ProjectRequest{Run: f.req.Run, Files: f.w.files, SyntaxLimits: f.req.SyntaxLimits}, f.w, func(_ context.Context, fact graph.Fact) error {
		if fact.Node != nil {
			nodes[fact.Key().ID] = fact.Node
		}
		if fact.Edge != nil {
			edges = append(edges, fact.Edge)
		}
		return fact.Validate()
	})
	must(t, err)
	// The inherits edge starts at the subclass, not at the module that
	// defines it: base expressions are resolved in the enclosing scope but
	// the inheritance is the class's own fact.
	var inherits bool
	for _, e := range edges {
		if e.Kind != graph.EdgeInherits {
			continue
		}
		src, dst := nodes[e.SourceID], nodes[e.TargetID]
		if src == nil || dst == nil {
			t.Fatalf("inherits edge with unknown ends: %+v", e)
		}
		if src.Kind != "class" || src.Name != "Child" {
			t.Fatalf("inherits edge starts at %s %q, want class Child", src.Kind, src.Name)
		}
		inherits = inherits || dst.Name == "Parent"
	}
	if !inherits {
		t.Fatal("no inherits edge from Child to Parent was projected")
	}
}

func TestPyrightPreviouslyMissingModuleCanResolve(t *testing.T) {
	f := newFixture(t)
	f.add("app", "app.py", "from added import run\nrun()\n", true, true)
	f.resolve()
	// Not in the repository and not installed: named as a package's.
	if l := f.lookup("app", "run()", 0); l.Status != semantic.LookupResolved || f.w.symbols[l.SelectedSymbolID].External == nil || f.w.symbols[l.SelectedSymbolID].Key.CanonicalSignature != "added.run" {
		t.Fatalf("missing import: %+v", l)
	}
	f.add("added", "added.py", "def run(): return 1\n", true, true)
	f.resolve()
	if l := f.lookup("app", "run()", 0); l.Status != semantic.LookupResolved || f.w.symbols[l.SelectedSymbolID].External != nil {
		t.Fatalf("negative import retained: %+v", l)
	}
}

// What an uninstalled package provides is named by its path from the
// module: names, members of values made from it, calls on them, and members
// a class inherits from one of its classes.
func TestUninstalledPackagesAreNamed(t *testing.T) {
	f := newFixture(t)
	f.add("log", "log.py", "import structlog\n\ndef get_logger(name):\n    return structlog.get_logger(name)\n", true, true)
	f.add("app", "app.py", `import numpy as np
from fastapi import FastAPI
from beanie import Document
from google.adk.runners import Runner
from log import get_logger

app = FastAPI()
logger = get_logger(__name__)

class User(Document):
    name: str

@app.get("/users")
async def users():
    np.array([1])
    logger.info("listing")
    async for event in Runner().run_async():
        event.get_function_calls()
    return await User.find_one(User.name == "x")
`, true, true)
	f.resolve()
	for text, want := range map[string]string{
		`app.get("/users")`:               "fastapi.FastAPI().get",
		"np.array([1])":                   "numpy.array",
		`User.find_one(User.name == "x")`: "beanie.Document.find_one",
		"FastAPI()":                       "fastapi.FastAPI",
		`logger.info("listing")`:          "structlog.get_logger().info",
		"event.get_function_calls()":      "google.adk.runners.Runner().run_async()[].get_function_calls",
	} {
		l := f.lookup("app", text, 0)
		sym, ok := f.w.symbols[l.SelectedSymbolID]
		if l.Status != semantic.LookupResolved || !ok || sym.External == nil || sym.Key.CanonicalSignature != want {
			t.Fatalf("%s: %+v %+v", text, l, sym)
		}
	}
}

// A monorepo's projects each resolve imports as they run: their own
// packages first (both services have an app package), then the shared ones.
// A reference to an annotated parameter binds to it, and a migrations
// directory named alembic does not hide the alembic package.
func TestMonorepoProjectsResolveTheirOwnImports(t *testing.T) {
	options, err := environment.Encode(environment.Settings{Version: "3.12", Platform: "Linux", Roots: []string{".", "src"}, Projects: []environment.Project{
		{Root: "apps/api", Shared: true}, {Root: "apps/worker", Shared: true}, {Root: "packages/common", Paths: []string{"packages/common/src"}, Shared: true},
	}})
	must(t, err)
	f := newFixtureWithOptions(t, options)
	f.add("common-init", "packages/common/src/common_pkg/__init__.py", "", true, true)
	f.add("models", "packages/common/src/common_pkg/models.py", "class Post:\n    title: str = \"\"\n", true, true)
	f.add("api-init", "apps/api/app/__init__.py", "", true, true)
	f.add("api-helpers", "apps/api/app/helpers.py", "def shout(s: str) -> str:\n    return s.upper()\n", true, true)
	f.add("migration", "apps/api/alembic/env.py", "print(\"migrations\")\n", true, true)
	f.add("service", "apps/api/app/service.py", `from common_pkg.models import Post
from app.helpers import shout
from alembic import op


def title(post: Post, limit: int = 10) -> str:
    op.create_table("posts")
    return shout(post.title)[:limit]
`, true, true)
	f.add("worker-init", "apps/worker/app/__init__.py", "", true, true)
	f.add("worker-helpers", "apps/worker/app/helpers.py", "def shout(s: str) -> str:\n    return s\n", true, true)
	f.add("job", "apps/worker/app/job.py", "from app.helpers import shout\n\n\ndef run() -> str:\n    return shout(\"x\")\n", true, true)
	f.resolve()
	for _, c := range []struct{ file, text, want string }{
		{"service", "title", f.symbol("models", ir.DeclarationField, "title")},
		{"service", "shout(post.title)", f.symbol("api-helpers", ir.DeclarationFunction, "shout")},
		{"service", "post", f.symbol("service", ir.DeclarationParameter, "post")},
		{"service", "limit", f.symbol("service", ir.DeclarationParameter, "limit")},
		{"job", `shout("x")`, f.symbol("worker-helpers", ir.DeclarationFunction, "shout")},
	} {
		if l := f.lookup(c.file, c.text, 0); l.Status != semantic.LookupResolved || l.SelectedSymbolID != c.want {
			t.Fatalf("%s: %+v, want %s", c.text, l, c.want)
		}
	}
	l := f.lookup("service", `op.create_table("posts")`, 0)
	if sym := f.w.symbols[l.SelectedSymbolID]; l.Status != semantic.LookupResolved || sym.External == nil || sym.Key.CanonicalSignature != "alembic.op.create_table" {
		t.Fatalf("alembic: %+v %+v", l, sym)
	}
}

func TestPyrightHeapExhaustionIsExplicit(t *testing.T) {
	requireAnalyzer(t)
	f := newFixture(t)
	f.add("app", "app.py", "def run():\n    return 1\n", true, true)
	stub := filepath.Join(t.TempDir(), "node")
	must(t, os.WriteFile(stub, []byte("#!/bin/sh\necho '<--- Last few GCs --->' >&2\necho 'FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory' >&2\nexit 134\n"), 0700))
	// The run goes on without the context's references, and its warning
	// says how to fix it.
	result, err := New(Config{NodePath: stub, MaxHeapMiB: 256}).Resolve(context.Background(), f.req, f.w)
	if err != nil {
		t.Fatalf("heap exhaustion failed the run: %v", err)
	}
	warning := strings.Join(result.Warnings, "\n")
	if !strings.Contains(warning, ErrAnalyzerHeap.Error()) || !strings.Contains(warning, "max_heap_mib=256") || !strings.Contains(warning, "1 files") || strings.Contains(warning, "Last few GCs") || result.Skipped != 1 {
		t.Fatalf("unhelpful heap warning: %+v", result)
	}
	if r := New(Config{}); r.config.MaxHeapMiB != DefaultMaxHeapMiB {
		t.Fatalf("default heap %d", r.config.MaxHeapMiB)
	}
}

const oneEntity = `import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

logger = logging.getLogger(__name__)


class Settings:
    environment: Literal["development", "production"] = "development"
    name: str = ""

    def __init__(self, name: str) -> None:
        self.name = name


@asynccontextmanager
async def transaction(name: str) -> AsyncIterator[Settings]:
    yield Settings(name)


async def use(name: str) -> str:
    async with transaction(name) as s:
        return s.name


def create(settings: Settings | None = None) -> str:
    settings = settings or Settings("x")

    def inner() -> str:
        return settings.name

    return inner()


def counter() -> int:
    status = 500

    def update() -> None:
        nonlocal status
        status = 200

    update()
    return status
`

// A call of a decorated function binds to the function, whatever the
// decorator returns; one variable's assignments, a class attribute also
// assigned through self, and a module's implicit names bind to one target;
// Literal's strings are values, not references.
func TestPyrightBindsDecoratedCallsVariablesAndImplicitNames(t *testing.T) {
	f := newFixture(t)
	f.add("app", "app.py", oneEntity, true, true)
	r := f.resolve()
	if r.Unresolved != 0 || r.Ambiguous != 0 {
		for _, l := range f.w.lookups["app"] {
			if l.Status != semantic.LookupResolved {
				span := l.Evidence.Span
				t.Logf("%s %q %s", l.Status, oneEntity[span.Start.ByteOffset:span.End.ByteOffset], l.Reason)
			}
		}
		t.Fatalf("resolution: %+v", r)
	}
	// bound are what the lookups of text on a line bind to.
	bound := func(text string, line uint32) []string {
		t.Helper()
		var ids []string
		for _, l := range f.w.lookups["app"] {
			span := l.Evidence.Span
			if span.Start.Line == line && oneEntity[span.Start.ByteOffset:span.End.ByteOffset] == text {
				ids = append(ids, l.SelectedSymbolID)
			}
		}
		if len(ids) == 0 {
			t.Fatalf("no lookup of %q on line %d", text, line)
		}
		return ids
	}
	at := func(text string, line uint32) semantic.Symbol { return f.w.symbols[bound(text, line)[0]] }
	declaredAt := func(s semantic.Symbol) uint32 {
		if s.Source == nil {
			return 0
		}
		return s.Source.Evidence.Span.Start.Line
	}
	if s := at("transaction(name)", 23); s.ID != f.symbol("app", ir.DeclarationFunction, "transaction") {
		t.Fatalf("decorated call: %+v", s)
	}
	if s := at("settings", 31); declaredAt(s) != 27 {
		t.Fatalf("a captured parameter assigned again: %+v", s)
	}
	if a, b := at("status", 41), at("status", 44); declaredAt(a) != 37 || a.ID != b.ID {
		t.Fatalf("a nonlocal variable: %+v %+v", a, b)
	}
	if field := at("name", 24); declaredAt(field) != 11 || !slices.Contains(bound("name", 14), field.ID) {
		t.Fatalf("a class attribute assigned through self: %+v, line 14 %v", field, bound("name", 14))
	}
	logger := f.w.symbols[f.lookup("app", "__name__", 0).SelectedSymbolID]
	if logger.External == nil || logger.Key == nil || !strings.HasPrefix(logger.Key.CanonicalSignature, "types.ModuleType.__name__@") {
		t.Fatalf("__name__: %+v", logger)
	}
}
