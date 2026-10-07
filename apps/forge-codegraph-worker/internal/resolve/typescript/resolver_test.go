package typescript

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	tsparser "ei-aitiger-codegraph/worker/internal/parser/typescript"
)

// fakeWorkspace is an in-memory semantic.Workspace.
type fakeWorkspace struct {
	mu       sync.Mutex
	build    bc.BuildContext
	files    []semantic.SourceInput
	syntax   map[ir.FileID]ir.SourceFile
	bytes    map[ir.FileID][]byte
	symbols  map[string]semantic.Symbol
	lookups  map[ir.FileID][]semantic.Lookup
	current  map[ir.FileID]semantic.FileIdentities
	previous map[string]semantic.FileIdentities
}

func newFakeWorkspace(build bc.BuildContext) *fakeWorkspace {
	return &fakeWorkspace{build: build, syntax: map[ir.FileID]ir.SourceFile{}, bytes: map[ir.FileID][]byte{}, symbols: map[string]semantic.Symbol{}, lookups: map[ir.FileID][]semantic.Lookup{}, current: map[ir.FileID]semantic.FileIdentities{}, previous: map[string]semantic.FileIdentities{}}
}

var _ semantic.Workspace = (*fakeWorkspace)(nil)

func (w *fakeWorkspace) Build() bc.BuildContext { return w.build }
func (w *fakeWorkspace) Files(context.Context) ([]semantic.SourceInput, error) {
	out := append([]semantic.SourceInput{}, w.files...)
	sort.Slice(out, func(i, j int) bool { return out[i].Source.Path < out[j].Source.Path })
	return out, nil
}
func (w *fakeWorkspace) File(_ context.Context, id ir.FileID) (semantic.SourceInput, error) {
	for _, f := range w.files {
		if f.Source.FileID == id {
			return f, nil
		}
	}
	return semantic.SourceInput{}, semantic.ErrNotFound
}
func (w *fakeWorkspace) FileByPath(_ context.Context, set bc.SourceSetID, path string) (semantic.SourceInput, error) {
	for _, f := range w.files {
		if f.Source.SourceSetID == string(set) && f.Source.Path == path {
			return f, nil
		}
	}
	return semantic.SourceInput{}, semantic.ErrNotFound
}
func (w *fakeWorkspace) Syntax(_ context.Context, in semantic.SourceInput) (ir.SourceFile, error) {
	f, ok := w.syntax[in.Source.FileID]
	if !ok {
		return ir.SourceFile{}, semantic.ErrNotFound
	}
	return f, nil
}
func (w *fakeWorkspace) SourceBytes(_ context.Context, in semantic.SourceInput) ([]byte, error) {
	b, ok := w.bytes[in.Source.FileID]
	if !ok {
		return nil, semantic.ErrNotFound
	}
	return b, nil
}
func (w *fakeWorkspace) PutSymbols(_ context.Context, symbols []semantic.Symbol) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, s := range symbols {
		if s.ID == "" {
			return fmt.Errorf("symbol without ID")
		}
		w.symbols[s.ID] = s
	}
	return nil
}
func (w *fakeWorkspace) Symbol(_ context.Context, id string) (semantic.Symbol, error) {
	s, ok := w.symbols[id]
	if !ok {
		return semantic.Symbol{}, semantic.ErrNotFound
	}
	return s, nil
}
func (w *fakeWorkspace) Symbols(_ context.Context, ids []string) (map[string]semantic.Symbol, error) {
	out := map[string]semantic.Symbol{}
	for _, id := range ids {
		if s, ok := w.symbols[id]; ok {
			out[id] = s
		}
	}
	return out, nil
}
func (w *fakeWorkspace) SymbolsByFile(_ context.Context, id ir.FileID) ([]semantic.Symbol, error) {
	var out []semantic.Symbol
	for _, s := range w.symbols {
		if s.Source != nil && s.Source.FileID == id {
			out = append(out, s)
		}
	}
	return out, nil
}
func (w *fakeWorkspace) EachSymbol(_ context.Context, fn func(semantic.Symbol) error) error {
	ids := make([]string, 0, len(w.symbols))
	for id := range w.symbols {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		if err := fn(w.symbols[id]); err != nil {
			return err
		}
	}
	return nil
}
func (w *fakeWorkspace) PutLookups(_ context.Context, id ir.FileID, lookups []semantic.Lookup) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.lookups[id] = append([]semantic.Lookup{}, lookups...)
	return nil
}
func (w *fakeWorkspace) Lookups(_ context.Context, id ir.FileID) ([]semantic.Lookup, error) {
	return w.lookups[id], nil
}
func (w *fakeWorkspace) PutIdentities(_ context.Context, f semantic.FileIdentities) error {
	w.current[f.FileID] = f
	return nil
}
func (w *fakeWorkspace) Identities(_ context.Context, id ir.FileID) (semantic.FileIdentities, error) {
	f, ok := w.current[id]
	if !ok {
		return semantic.FileIdentities{}, semantic.ErrNotFound
	}
	return f, nil
}
func (w *fakeWorkspace) PreviousIdentities(_ context.Context, lineage string) (semantic.FileIdentities, error) {
	f, ok := w.previous[lineage]
	if !ok {
		return semantic.FileIdentities{}, semantic.ErrNotFound
	}
	return f, nil
}
func (w *fakeWorkspace) Entity(_ context.Context, id ir.FileID, declaration ir.DeclarationID) (string, error) {
	if f, ok := w.current[id]; ok {
		if d, ok := f.Declaration(declaration); ok {
			return d.EntityID, nil
		}
	}
	return "", semantic.ErrNotFound
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func rawSum(b []byte) string { s := sha256.Sum256(b); return hex.EncodeToString(s[:]) }

const setID = "syntax-typescript"

type fixture struct {
	t     *testing.T
	w     *fakeWorkspace
	build bc.BuildContext
	files map[string]ir.SourceFile
	bytes map[string][]byte
	req   semantic.ResolveRequest
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	in := bc.Inventory{
		Inputs:        []bc.Input{{ID: "checkout-sources", Kind: bc.InputSourceRoot, Location: &bc.Location{Root: "checkout", Path: "."}}},
		Modules:       []bc.Module{{ID: "syntax", Name: "explicit syntax scan", Directory: "."}},
		SourceSets:    []bc.SourceSet{{ID: setID, ModuleID: "syntax", Name: "typescript sources", Kind: bc.SourceSetCustom, Language: "typescript", LanguageVersion: "5", SourceRootIDs: []bc.InputID{"checkout-sources"}}},
		MissingInputs: []bc.MissingInput{{ID: "build-inventory", Requested: "modules and dependencies", Reason: "syntax-only scan"}},
	}
	digest, err := in.Digest()
	must(t, err)
	commit := "0123456789abcdef0123456789abcdef01234567"
	build, err := bc.Seal(bc.BuildContext{RepositoryID: "repo", SnapshotID: commit, Producer: bc.Producer{Name: "fixture", Version: "1", InputSHA256: digest}, Inventory: in, Checks: []bc.InputCheck{{InputID: "checkout-sources", Status: bc.Available}}})
	must(t, err)
	f := &fixture{t: t, w: newFakeWorkspace(build), build: build, files: map[string]ir.SourceFile{}, bytes: map[string][]byte{}}
	f.req = semantic.ResolveRequest{Run: deployment.RunKey{RepositoryID: "repo", RunID: "run"}, CommitSHA: commit, CheckoutPath: t.TempDir(), SyntaxLimits: parser.DefaultLimits(), BuildLimits: bc.DefaultLimits()}
	return f
}

// add parses a TypeScript file and registers it. When syntax is false the
// file is unchanged and known only through its previous identities.
func (f *fixture) add(id, path, content string, affected, syntax bool) semantic.SourceInput {
	f.t.Helper()
	ctx := context.Background()
	p, err := tsparser.New()
	must(f.t, err)
	defer p.Close(ctx)
	raw := []byte(content)
	src := ir.Source{FileID: ir.FileID(id), RepositoryID: "repo", SnapshotID: f.build.SnapshotID, Path: path, ContentSHA256: rawSum(raw), SizeBytes: uint64(len(raw)), Language: "typescript", LanguageVersion: "5", ModuleID: "syntax", SourceSetID: setID, BuildContextID: string(f.build.ID)}
	file, err := p.Parse(ctx, parser.Input{Source: src, Content: raw, Limits: parser.DefaultLimits()})
	must(f.t, err)
	if file.Coverage.Status != ir.ExtractionComplete {
		f.t.Fatalf("%s: %+v", path, file.Coverage)
	}
	f.files[id], f.bytes[id] = file, raw
	in := semantic.SourceInput{Source: src, Lineage: graph.Lineage("repo", "syntax", setID, path), Affected: affected}
	f.w.files = append(f.w.files, in)
	f.w.bytes[src.FileID] = raw
	if syntax {
		f.w.syntax[src.FileID] = file
	} else {
		ids := semantic.FileIdentities{Lineage: in.Lineage, FileID: src.FileID, Path: path, ContentSHA256: src.ContentSHA256}
		for _, d := range file.Declarations {
			ids.Declarations = append(ids.Declarations, semantic.DeclarationIdentity{DeclarationID: d.ID, EntityID: "entity:" + string(d.ID), Kind: d.Kind, Name: d.Name, Span: d.Span, OwnerID: d.OwnerID})
		}
		f.w.previous[in.Lineage] = ids
	}
	return in
}

func (f *fixture) decl(id string, kind ir.DeclarationKind, name string) ir.Declaration {
	f.t.Helper()
	for _, d := range f.files[id].Declarations {
		if d.Kind == kind && d.Name == name {
			return d
		}
	}
	f.t.Fatalf("%s: no %s %s", id, kind, name)
	return ir.Declaration{}
}

func (f *fixture) symbol(id string, kind ir.DeclarationKind, name string) string {
	return symbolID(ir.FileID(id), f.decl(id, kind, name).ID)
}

// lookup finds the nth lookup (0-based) whose evidence spells text.
func (f *fixture) lookup(id, text string, nth int) semantic.Lookup {
	f.t.Helper()
	raw := f.bytes[id]
	seen := 0
	for _, l := range f.w.lookups[ir.FileID(id)] {
		span := l.Evidence.Span
		if span.End.ByteOffset > uint64(len(raw)) || string(raw[span.Start.ByteOffset:span.End.ByteOffset]) != text {
			continue
		}
		if seen == nth {
			return l
		}
		seen++
	}
	f.t.Fatalf("%s: no lookup %d spelling %q", id, nth, text)
	return semantic.Lookup{}
}

// configure stores project settings on the fixture's source set.
func (f *fixture) configure(settings Settings) {
	f.t.Helper()
	options, err := EncodeSettings(settings)
	must(f.t, err)
	for i := range f.build.Inventory.SourceSets {
		f.build.Inventory.SourceSets[i].LanguageOptions = options
	}
	f.w.build = f.build
}

func (f *fixture) resolve() semantic.ResolutionResult {
	f.t.Helper()
	result, err := New().Resolve(context.Background(), f.req, f.w)
	must(f.t, err)
	return result
}

const apiSource = `export class ApiClient {
  get(path: string): Promise<User> {
    return fetch(path).then((r) => r.json());
  }
  protected log(message: string): void {
    console.log(message);
  }
}

export interface User {
  name: string;
}

export function createClient(): ApiClient {
  return new ApiClient();
}

export const VERSION = "1";

export default ApiClient;
`

const appSource = `import { ApiClient, createClient, type User } from "./api";
import * as api from "./api";
import Client from "./api";
import { missing } from "./nowhere";
import React from "react";

export class AppService extends ApiClient {
  private client: ApiClient = createClient();

  run(user: User): void {
    this.client.get("/users").then((u) => console.log(u));
    super.log(user.name);
    this.log("done");
    api.createClient();
    new Client().get("/x");
    React.createElement("div");
    missing();
    const items = new Map<string, User>();
    items.set(user.name, user);
  }

  log(message: string): void {}
}
`

func expectResolved(t *testing.T, l semantic.Lookup, kind semantic.LookupKind, symbol string) {
	t.Helper()
	if l.Status != semantic.LookupResolved || l.Kind != kind || l.SelectedSymbolID != symbol || l.Provenance != "syntax" {
		t.Fatalf("want resolved %s to %s, got %+v", kind, symbol, l)
	}
}

// expectNamed wants a lookup resolved to a symbol outside the repository:
// an external one with the canonical signature, or an intrinsic by name.
func expectNamed(t *testing.T, f *fixture, l semantic.Lookup, kind semantic.LookupKind, name string) {
	t.Helper()
	sym, ok := f.w.symbols[l.SelectedSymbolID]
	if l.Status != semantic.LookupResolved || l.Kind != kind || !ok || l.Provenance != "syntax" {
		t.Fatalf("want resolved %s to %s, got %+v", kind, name, l)
	}
	switch {
	case sym.External != nil && sym.Key != nil && sym.Key.CanonicalSignature == name:
	case sym.Intrinsic != nil && sym.Intrinsic.Name == name:
	default:
		t.Fatalf("want %s, got %+v", name, sym)
	}
}

func expectUnresolved(t *testing.T, l semantic.Lookup, cause semantic.LookupCause, reason string) {
	t.Helper()
	if l.Status != semantic.LookupUnresolved || l.Cause != cause || !strings.Contains(l.Reason, reason) || l.Provenance != "syntax" || l.SelectedSymbolID != "" {
		t.Fatalf("want unresolved %s %q, got %+v", cause, reason, l)
	}
}

func TestResolveBindsAcrossModules(t *testing.T) {
	f := newFixture(t)
	f.add("api", "web/src/api.ts", apiSource, true, true)
	f.add("app", "web/src/app.ts", appSource, true, true)
	result := f.resolve()
	if result.Symbols == 0 || result.Resolved == 0 || result.Unresolved == 0 {
		t.Fatalf("result: %+v", result)
	}
	apiClient := f.symbol("api", ir.DeclarationClass, "ApiClient")
	get := f.symbol("api", ir.DeclarationMethod, "get")
	apiLog := f.symbol("api", ir.DeclarationMethod, "log")
	createClient := f.symbol("api", ir.DeclarationFunction, "createClient")
	user := f.symbol("api", ir.DeclarationInterface, "User")
	userName := f.symbol("api", ir.DeclarationField, "name")
	appLog := f.symbol("app", ir.DeclarationMethod, "log")

	expectResolved(t, f.lookup("app", "createClient()", 0), semantic.LookupCall, createClient)
	expectResolved(t, f.lookup("app", `this.client.get("/users")`, 0), semantic.LookupCall, get)
	expectNamed(t, f, f.lookup("app", `this.client.get("/users").then((u) => console.log(u))`, 0), semantic.LookupCall, "Promise.then")
	expectNamed(t, f, f.lookup("app", "console.log(u)", 0), semantic.LookupCall, "console.log")
	expectResolved(t, f.lookup("app", "super.log(user.name)", 0), semantic.LookupCall, apiLog)
	expectResolved(t, f.lookup("app", "user.name", 0), semantic.LookupMember, userName)
	expectResolved(t, f.lookup("app", `this.log("done")`, 0), semantic.LookupCall, appLog)
	expectResolved(t, f.lookup("app", "api.createClient()", 0), semantic.LookupCall, createClient)
	expectResolved(t, f.lookup("app", "new Client()", 0), semantic.LookupCall, apiClient)
	expectResolved(t, f.lookup("app", `new Client().get("/x")`, 0), semantic.LookupCall, get)
	expectNamed(t, f, f.lookup("app", `React.createElement("div")`, 0), semantic.LookupCall, "react#createElement")
	expectUnresolved(t, f.lookup("app", "missing()", 0), semantic.CauseAnalysisLimitation, "module_not_found")
	expectNamed(t, f, f.lookup("app", "items.set(user.name, user)", 0), semantic.LookupCall, "Map.set")
	if l := f.lookup("app", "new Map<string, User>()", 0); l.Status != semantic.LookupResolved || f.w.symbols[l.SelectedSymbolID].Intrinsic == nil {
		t.Fatalf("intrinsic construction: %+v", l)
	}
	expectResolved(t, f.lookup("app", "ApiClient", 0), semantic.LookupInheritance, apiClient)
	expectResolved(t, f.lookup("app", "ApiClient", 1), semantic.LookupType, apiClient)
	expectResolved(t, f.lookup("app", "User", 0), semantic.LookupType, user)
	if l := f.lookup("app", "void", 0); l.Status != semantic.LookupResolved || f.w.symbols[l.SelectedSymbolID].Intrinsic == nil {
		t.Fatalf("predefined type: %+v", l)
	}
	override := false
	for _, l := range f.w.lookups["app"] {
		if l.Kind == semantic.LookupOverride {
			override = true
			if l.DeclarationID != f.decl("app", ir.DeclarationMethod, "log").ID || l.SelectedSymbolID != apiLog || l.Status != semantic.LookupResolved {
				t.Fatalf("override: %+v", l)
			}
		}
		if l.Provenance != "syntax" {
			t.Fatalf("provenance: %+v", l)
		}
		if l.Status == semantic.LookupResolved {
			if _, ok := f.w.symbols[l.SelectedSymbolID]; !ok {
				t.Fatalf("resolved lookup names an unwritten symbol: %+v", l)
			}
		}
	}
	if !override {
		t.Fatal("no override lookup")
	}
	if key := f.w.symbols[get].Key; key == nil || key.OwnerKey != "web/src/api#ApiClient" || key.CanonicalSignature != "web/src/api#ApiClient.get(path)" || key.Kind != ir.DeclarationMethod {
		t.Fatalf("method key: %+v", key)
	}
	if key := f.w.symbols[apiClient].Key; key == nil || key.OwnerKey != "web/src/api" || key.CanonicalSignature != "web/src/api#ApiClient" {
		t.Fatalf("class key: %+v", key)
	}
	if sym := f.w.symbols[f.symbol("api", ir.DeclarationParameter, "path")]; sym.Key != nil || sym.OwnerSymbolID != get {
		t.Fatalf("parameter symbol: %+v", sym)
	}
	// api.ts binds its own calls too.
	expectResolved(t, f.lookup("api", "new ApiClient()", 0), semantic.LookupCall, apiClient)
	if l := f.lookup("api", "fetch(path)", 0); l.Status != semantic.LookupResolved || f.w.symbols[l.SelectedSymbolID].Intrinsic == nil || f.w.symbols[l.SelectedSymbolID].Intrinsic.Name != "fetch" {
		t.Fatalf("global function: %+v", l)
	}
	expectNamed(t, f, f.lookup("api", "fetch(path).then((r) => r.json())", 0), semantic.LookupCall, "fetch().then")
}

func TestUnaffectedFileIsReparsedFromCheckout(t *testing.T) {
	f := newFixture(t)
	f.add("api", "web/src/api.ts", apiSource, false, false)
	f.add("app", "web/src/app.ts", appSource, true, true)
	result := f.resolve()
	if len(f.w.lookups["api"]) != 0 {
		t.Fatal("unaffected file received lookups")
	}
	expectResolved(t, f.lookup("app", "createClient()", 0), semantic.LookupCall, f.symbol("api", ir.DeclarationFunction, "createClient"))
	expectResolved(t, f.lookup("app", `this.client.get("/users")`, 0), semantic.LookupCall, f.symbol("api", ir.DeclarationMethod, "get"))
	// The unchanged file's IR left the cache, so it is parsed again from
	// its bytes and its default export is still known.
	expectResolved(t, f.lookup("app", "new Client()", 0), semantic.LookupCall, f.symbol("api", ir.DeclarationClass, "ApiClient"))
	if result.Symbols < uint64(len(f.files["api"].Declarations)+len(f.files["app"].Declarations)) {
		t.Fatalf("symbols: %+v", result)
	}
}

func TestUnaffectedFileFromIdentitiesAlone(t *testing.T) {
	f := newFixture(t)
	api := f.add("api", "web/src/api.ts", apiSource, false, false)
	f.add("app", "web/src/app.ts", appSource, true, true)
	delete(f.w.bytes, api.Source.FileID)
	f.resolve()
	expectResolved(t, f.lookup("app", "createClient()", 0), semantic.LookupCall, f.symbol("api", ir.DeclarationFunction, "createClient"))
	// Identity maps record neither exports nor re-exports.
	expectUnresolved(t, f.lookup("app", "new Client()", 0), semantic.CauseAnalysisLimitation, "default_export_not_identified")
}

func TestResolveIsDeterministic(t *testing.T) {
	var outputs [][]byte
	for i := 0; i < 2; i++ {
		f := newFixture(t)
		f.add("api", "web/src/api.ts", apiSource, true, true)
		f.add("app", "web/src/app.ts", appSource, true, true)
		f.resolve()
		out, err := json.Marshal(map[string]any{"app": f.w.lookups["app"], "api": f.w.lookups["api"], "symbols": f.w.symbols})
		must(t, err)
		outputs = append(outputs, out)
	}
	if string(outputs[0]) != string(outputs[1]) {
		t.Fatal("resolution output differs between runs")
	}
}

func TestResolveRejectsBadRequests(t *testing.T) {
	f := newFixture(t)
	f.add("app", "web/src/app.ts", appSource, true, true)
	req := f.req
	req.Contexts = []bc.SourceSetID{"unknown"}
	if _, err := New().Resolve(context.Background(), req, f.w); !errors.Is(err, semantic.ErrInvalid) {
		t.Fatalf("unknown context: %v", err)
	}
	req = f.req
	req.CommitSHA = strings.Repeat("f", 40)
	if _, err := New().Resolve(context.Background(), req, f.w); !errors.Is(err, semantic.ErrInvalid) {
		t.Fatalf("foreign commit: %v", err)
	}
	if New().Version() != Version || New().PolicyDigest() == "" {
		t.Fatal("version and policy digest required")
	}
}

func TestParserSkippedFileBindsNothing(t *testing.T) {
	f := newFixture(t)
	f.add("api", "web/src/api.ts", apiSource, true, true)
	bundle := f.add("bundle", "web/dist/chunk-ABC123.js", "export const x = 1;\n", true, false)
	// The parse stage skipped the bundle: no syntax, no bytes to re-parse.
	delete(f.w.bytes, bundle.Source.FileID)
	delete(f.w.previous, bundle.Lineage)
	result := f.resolve()
	if result.Skipped != 1 || result.Resolved == 0 {
		t.Fatalf("result: %+v", result)
	}
	if len(f.w.lookups["bundle"]) != 0 {
		t.Fatal("a skipped file received lookups")
	}
	for _, sym := range f.w.symbols {
		if sym.Source != nil && sym.Source.FileID == "bundle" {
			t.Fatalf("a skipped file wrote a symbol: %+v", sym)
		}
	}
}

// An unchanged file the parser always declines has neither syntax nor
// identities; the incremental run binds around it instead of failing.
func TestUnchangedFileWithoutSyntaxOrIdentitiesBindsNothing(t *testing.T) {
	f := newFixture(t)
	f.add("api", "web/src/api.ts", apiSource, true, true)
	bundle := f.add("bundle", "web/dist/chunk-ABC123.js", "export const x = 1;\n", false, false)
	delete(f.w.bytes, bundle.Source.FileID)
	delete(f.w.previous, bundle.Lineage)
	if result := f.resolve(); result.Resolved == 0 {
		t.Fatalf("result: %+v", result)
	}
}

// Field initializers that type each other, and cyclic heritage, end in an
// unsupported lookup; before, inference recursed until the process died.
func TestCyclicInferenceTerminates(t *testing.T) {
	f := newFixture(t)
	f.add("cycle", "web/src/cycle.ts", "export class A { b = this.c.m(); c = this.b.n(); d = this.e.f; e = this.d.g; }\nexport class B extends C { run() { return this.x(); } }\nexport class C extends B { }\n", true, true)
	f.resolve()
}

const packageSource = `import axios from "axios";
import { useState, Component } from "react";
import * as fs from "fs";
const { join } = require("node:path");

export class Panel extends Component {
  load(): void {
    const [count, setCount] = useState(0);
    axios.create().get("/x");
    fs.readFileSync("a");
    join("a", "b");
    this.setState({});
    const data = JSON.parse("{}").data;
    const mode = process.env.NODE_ENV;
    expect(count).toBe(1);
  }
}
`

// Values from packages, Node built-ins, the platform library and test
// globals bind to symbols named by where they come from and the path taken
// from there, as Java binds JDK and JAR members to their external symbols.
func TestPackageAndPlatformValuesBindToNamedSymbols(t *testing.T) {
	f := newFixture(t)
	f.add("panel", "web/src/panel.ts", packageSource, true, true)
	f.resolve()
	for _, c := range []struct {
		text string
		kind semantic.LookupKind
		name string
	}{
		{"useState(0)", semantic.LookupCall, "react#useState"},
		{"axios.create()", semantic.LookupCall, "axios#create"},
		{`axios.create().get("/x")`, semantic.LookupCall, "axios#create().get"},
		{`fs.readFileSync("a")`, semantic.LookupCall, "node:fs#readFileSync"},
		{`join("a", "b")`, semantic.LookupCall, "node:path#join"},
		{"this.setState({})", semantic.LookupCall, "react#Component.setState"},
		{`JSON.parse("{}")`, semantic.LookupCall, "JSON.parse"},
		{"expect(count)", semantic.LookupCall, "global:test-api#expect"},
		{"expect(count).toBe(1)", semantic.LookupCall, "global:test-api#expect().toBe"},
		{"Component", semantic.LookupInheritance, "react#Component"},
	} {
		expectNamed(t, f, f.lookup("panel", c.text, 0), c.kind, c.name)
	}
	named := map[string]string{}
	for _, sym := range f.w.symbols {
		if sym.External != nil {
			named[sym.Key.CanonicalSignature] = sym.External.ArtifactID
		}
	}
	for signature, artifact := range map[string]string{"react#useState": "npm:react", "axios#create().get": "npm:axios", "node:fs#readFileSync": "node:fs", "node:path#join": "node:path", "global:test-api#expect": "global:test-api"} {
		if named[signature] != artifact {
			t.Fatalf("%s: artifact %q (all: %v)", signature, named[signature], named)
		}
	}
}

const literalSource = `export function build(name: string, fallback?: string[]): string {
  const args = ["run", "-i"];
  args.push("--rm");
  const label = "user " + name;
  const parts = fallback ?? [];
  parts.push(label.trim());
  const pattern = /^a+$/;
  pattern.test(label);
  return args.join(" ");
}
`

// Values whose type the syntax states without a declaration: array and
// string literals, concatenations with a string, a fallback's left
// operand, a regular expression.
func TestLiteralValuesBindTheirPlatformMembers(t *testing.T) {
	f := newFixture(t)
	f.add("build", "web/src/build.ts", literalSource, true, true)
	f.resolve()
	for _, c := range []struct{ text, name string }{
		{`args.push("--rm")`, "Array.push"},
		{`args.join(" ")`, "Array.join"},
		{"label.trim()", "string.trim"},
		{"parts.push(label.trim())", "Array.push"},
		{"pattern.test(label)", "RegExp.test"},
	} {
		expectNamed(t, f, f.lookup("build", c.text, 0), semantic.LookupCall, c.name)
	}
}

const arrowSource = `export class Client {
  send(): void {}
}
const make = () => new Client();
const names = () => ["a", "b"];
function build() {
  const nested = () => {
    return "not the outer result";
  };
  return new Client();
}
export function run(): void {
  make().send();
  names().map((n) => n);
  build().send();
}
`

// A call of an arrow function with an expression body has that
// expression's type.
func TestArrowFunctionResultsAreTyped(t *testing.T) {
	f := newFixture(t)
	f.add("arrow", "web/src/arrow.ts", arrowSource, true, true)
	f.resolve()
	expectResolved(t, f.lookup("arrow", "make().send()", 0), semantic.LookupCall, f.symbol("arrow", ir.DeclarationMethod, "send"))
	expectNamed(t, f, f.lookup("arrow", "names().map((n) => n)", 0), semantic.LookupCall, "Array.map")
	// A block-bodied function is typed by its own return statement, not a
	// nested function's.
	expectResolved(t, f.lookup("arrow", "build().send()", 0), semantic.LookupCall, f.symbol("arrow", ir.DeclarationMethod, "send"))
}

func TestOnlyPackageNamesBecomePackageSymbols(t *testing.T) {
	for spec, want := range map[string]bool{
		"react": true, "@tanstack/react-query": true, "@tanstack/react-query/devtools": true, "lodash/debounce": true, "node:fs": true, "fs": true, "socket.io-client": true,
		"@/components/Button": false, "~/lib/api": false, "#internal": false, "@scope": false, "React": false, "": false,
	} {
		if got := packageSpecifier(spec); got != want {
			t.Fatalf("%q: %v", spec, got)
		}
	}
}
