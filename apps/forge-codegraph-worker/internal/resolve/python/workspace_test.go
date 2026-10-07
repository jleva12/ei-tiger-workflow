package python

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"sync"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	pyparser "ei-aitiger-codegraph/worker/internal/parser/python"
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

const setID = "syntax-python"

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
	return newFixtureWithOptions(t, nil)
}

// newFixtureWithOptions analyses the source set with these language options
// (a python.environment of projects, roots or dependencies).
func newFixtureWithOptions(t *testing.T, options map[string]string) *fixture {
	t.Helper()
	in := bc.Inventory{
		Inputs:        []bc.Input{{ID: "checkout-sources", Kind: bc.InputSourceRoot, Location: &bc.Location{Root: "checkout", Path: "."}}},
		Modules:       []bc.Module{{ID: "syntax", Name: "explicit syntax scan", Directory: "."}},
		SourceSets:    []bc.SourceSet{{ID: setID, ModuleID: "syntax", Name: "python sources", Kind: bc.SourceSetCustom, Language: "python", LanguageVersion: "3.12", LanguageOptions: options, SourceRootIDs: []bc.InputID{"checkout-sources"}}},
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

// add parses a Python file and registers it. When syntax is false the
// file is unchanged and known only through its previous identities.
func (f *fixture) add(id, path, content string, affected, syntax bool) semantic.SourceInput {
	f.t.Helper()
	ctx := context.Background()
	p, err := pyparser.New()
	must(f.t, err)
	defer p.Close(ctx)
	raw := []byte(content)
	src := ir.Source{FileID: ir.FileID(id), RepositoryID: "repo", SnapshotID: f.build.SnapshotID, Path: path, ContentSHA256: rawSum(raw), SizeBytes: uint64(len(raw)), Language: "python", LanguageVersion: "3.12", ModuleID: "syntax", SourceSetID: setID, BuildContextID: string(f.build.ID)}
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
