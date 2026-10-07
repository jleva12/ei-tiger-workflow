package java

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
	parserjava "ei-aitiger-codegraph/worker/internal/parser/java"
)

// fakeWorkspace is an in-memory semantic.Workspace for tests.
type fakeWorkspace struct {
	mu       sync.Mutex
	build    bc.BuildContext
	files    []semantic.SourceInput
	syntax   map[ir.FileID]ir.SourceFile
	bytes    map[ir.FileID][]byte
	symbols  map[string]semantic.Symbol
	puts     int
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
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i].Source, out[j].Source
		if a.ModuleID != b.ModuleID {
			return a.ModuleID < b.ModuleID
		}
		if a.SourceSetID != b.SourceSetID {
			return a.SourceSetID < b.SourceSetID
		}
		return a.Path < b.Path
	})
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
	w.puts++
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

// fixture is a build inventory with one module, main and test source sets,
// and the pinned JDK when one is supplied.
type fixture struct {
	t        *testing.T
	w        *fakeWorkspace
	build    bc.BuildContext
	files    map[string]ir.SourceFile
	checkout string
	req      semantic.ResolveRequest
}

func newFixture(t *testing.T, jdkHome string, setup func(*bc.Inventory, *[]bc.InputCheck)) *fixture {
	t.Helper()
	ctx := context.Background()
	in := bc.Inventory{
		Inputs:     []bc.Input{{ID: "src", Kind: bc.InputSourceRoot, Location: &bc.Location{Root: "checkout", Path: "src"}}, {ID: "jdk", Kind: bc.InputJDK, UnavailableReason: "test has no platform"}},
		JDKs:       []bc.JDK{{ID: "jdk21", HomeInputID: "jdk", Vendor: "fixture", Version: "21", Major: 21}},
		Modules:    []bc.Module{{ID: "app", Name: "app", Directory: "."}},
		SourceSets: []bc.SourceSet{{ID: "main", ModuleID: "app", Name: "main", Kind: bc.SourceSetMain, JDKID: "jdk21", TargetRelease: 21, SourceRootIDs: []bc.InputID{"src"}}, {ID: "test", ModuleID: "app", Name: "test", Kind: bc.SourceSetTest, JDKID: "jdk21", TargetRelease: 21, SourceRootIDs: []bc.InputID{"src"}, Classpath: []bc.PathEntry{{Kind: bc.EntrySourceSet, RefID: "main"}}}},
	}
	checks := []bc.InputCheck{{InputID: "src", Status: bc.Available}, {InputID: "jdk", Status: bc.Missing}}
	if jdkHome != "" {
		fp, err := manifest.Fingerprint(ctx, jdkHome, ".", bc.InputJDK, bc.DefaultLimits())
		must(t, err)
		in.Inputs[1] = bc.Input{ID: "jdk", Kind: bc.InputJDK, Location: &bc.Location{Root: "java-jdk", Path: "."}, SHA256: fp}
		checks[1] = bc.InputCheck{InputID: "jdk", Status: bc.Available, ObservedSHA256: fp}
	}
	if setup != nil {
		setup(&in, &checks)
	}
	inputDigest, err := in.Digest()
	must(t, err)
	commit := "0123456789abcdef0123456789abcdef01234567"
	build, err := bc.Seal(bc.BuildContext{RepositoryID: "repo", SnapshotID: commit, Producer: bc.Producer{Name: "fixture", Version: "1", InputSHA256: inputDigest}, Inventory: in, Checks: checks})
	must(t, err)
	f := &fixture{t: t, w: newFakeWorkspace(build), build: build, files: map[string]ir.SourceFile{}, checkout: t.TempDir()}
	f.req = semantic.ResolveRequest{Run: deployment.RunKey{RepositoryID: "repo", RunID: "run"}, CommitSHA: commit, CheckoutPath: f.checkout, SyntaxLimits: parser.DefaultLimits(), BuildLimits: bc.DefaultLimits()}
	return f
}

// addParsed parses a Java file with the Tree-sitter adapter and registers it
// as an affected file of a source set.
func (f *fixture) addParsed(id, set, path string, content []byte, affected bool) semantic.SourceInput {
	f.t.Helper()
	ctx := context.Background()
	p, err := parserjava.New()
	must(f.t, err)
	defer p.Close(ctx)
	src := ir.Source{FileID: ir.FileID(id), RepositoryID: "repo", SnapshotID: f.build.SnapshotID, Path: path, ContentSHA256: rawSum(content), SizeBytes: uint64(len(content)), Language: "java", LanguageVersion: "21", ModuleID: "app", SourceSetID: set, BuildContextID: string(f.build.ID)}
	file, err := p.Parse(ctx, parser.Input{Source: src, Content: content, Limits: parser.DefaultLimits()})
	must(f.t, err)
	if file.Coverage.Status != ir.ExtractionComplete {
		f.t.Fatalf("fixture parse partial: %+v", file.Coverage)
	}
	// The parser's LambdaSite table may not be emitted yet; synthesize sites
	// from lambda expressions so implements lookups have an occurrence.
	if len(file.Lambdas) == 0 {
		for _, x := range file.Expressions {
			if x.Kind == ir.ExpressionLambda {
				file.Lambdas = append(file.Lambdas, ir.LambdaSite{Occurrence: ir.Occurrence{ID: ir.OccurrenceID("lambda:" + string(x.ID)), Span: x.Span, ScopeID: x.ScopeID}, ExpressionID: x.ID})
			}
		}
	}
	f.files[id] = file
	in := semantic.SourceInput{Source: src, Lineage: graph.Lineage("repo", "app", set, path), Affected: affected}
	f.w.files = append(f.w.files, in)
	f.w.syntax[src.FileID] = file
	f.w.bytes[src.FileID] = content
	full := filepath.Join(f.checkout, filepath.FromSlash(path))
	must(f.t, os.MkdirAll(filepath.Dir(full), 0o700))
	must(f.t, os.WriteFile(full, content, 0o600))
	return in
}

func (f *fixture) lookups(id string) []semantic.Lookup { return f.w.lookups[ir.FileID(id)] }

// occurrenceLookup finds the lookup of the occurrence whose written text is exactly text.
func (f *fixture) occurrenceLookup(id string, kind semantic.LookupKind, text string, content []byte) semantic.Lookup {
	f.t.Helper()
	for _, l := range f.lookups(id) {
		if l.Kind != kind || l.OccurrenceID == "" {
			continue
		}
		s := l.Evidence.Span
		if string(content[s.Start.ByteOffset:s.End.ByteOffset]) == text {
			return l
		}
	}
	f.t.Fatalf("no %s lookup for %q in %s", kind, text, id)
	return semantic.Lookup{}
}

func (f *fixture) declaration(id string, kind ir.DeclarationKind, name string) ir.Declaration {
	f.t.Helper()
	for _, d := range f.files[id].Declarations {
		if d.Kind == kind && d.Name == name {
			return d
		}
	}
	f.t.Fatalf("no %s %s in %s", kind, name, id)
	return ir.Declaration{}
}
