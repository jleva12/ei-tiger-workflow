package graphanalysis

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
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
	java "ei-aitiger-codegraph/worker/internal/parser/java"
)

func must(t testing.TB, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

// fakeWorkspace is an in-memory semantic.Workspace for one run.
type fakeWorkspace struct {
	mu         sync.Mutex
	inputs     []semantic.SourceInput
	syntax     map[ir.FileID]ir.SourceFile
	content    map[ir.FileID][]byte
	symbols    map[string]semantic.Symbol
	lookups    map[ir.FileID][]semantic.Lookup
	identities map[ir.FileID]semantic.FileIdentities
	previous   map[string]semantic.FileIdentities
}

var _ semantic.Workspace = (*fakeWorkspace)(nil)

func newFakeWorkspace() *fakeWorkspace {
	return &fakeWorkspace{
		syntax:     map[ir.FileID]ir.SourceFile{},
		content:    map[ir.FileID][]byte{},
		symbols:    map[string]semantic.Symbol{},
		lookups:    map[ir.FileID][]semantic.Lookup{},
		identities: map[ir.FileID]semantic.FileIdentities{},
		previous:   map[string]semantic.FileIdentities{},
	}
}

func (w *fakeWorkspace) Build() bc.BuildContext { return bc.BuildContext{} }

func (w *fakeWorkspace) Files(context.Context) ([]semantic.SourceInput, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]semantic.SourceInput(nil), w.inputs...), nil
}

func (w *fakeWorkspace) File(_ context.Context, id ir.FileID) (semantic.SourceInput, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, in := range w.inputs {
		if in.Source.FileID == id {
			return in, nil
		}
	}
	return semantic.SourceInput{}, semantic.ErrNotFound
}

func (w *fakeWorkspace) FileByPath(_ context.Context, sourceSet bc.SourceSetID, path string) (semantic.SourceInput, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, in := range w.inputs {
		if in.Source.Path == path && in.Source.SourceSetID == string(sourceSet) {
			return in, nil
		}
	}
	return semantic.SourceInput{}, semantic.ErrNotFound
}

func (w *fakeWorkspace) Syntax(_ context.Context, in semantic.SourceInput) (ir.SourceFile, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	file, ok := w.syntax[in.Source.FileID]
	if !ok {
		return ir.SourceFile{}, semantic.ErrNotFound
	}
	return file, nil
}

func (w *fakeWorkspace) SourceBytes(_ context.Context, in semantic.SourceInput) ([]byte, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	b, ok := w.content[in.Source.FileID]
	if !ok {
		return nil, semantic.ErrNotFound
	}
	return append([]byte(nil), b...), nil
}

func (w *fakeWorkspace) PutSymbols(_ context.Context, symbols []semantic.Symbol) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, s := range symbols {
		w.symbols[s.ID] = s
	}
	return nil
}

func (w *fakeWorkspace) Symbol(_ context.Context, id string) (semantic.Symbol, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	s, ok := w.symbols[id]
	if !ok {
		return semantic.Symbol{}, fmt.Errorf("%w: symbol %s", semantic.ErrNotFound, id)
	}
	return s, nil
}

func (w *fakeWorkspace) Symbols(_ context.Context, ids []string) (map[string]semantic.Symbol, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	out := map[string]semantic.Symbol{}
	for _, id := range ids {
		if s, ok := w.symbols[id]; ok {
			out[id] = s
		}
	}
	return out, nil
}

func (w *fakeWorkspace) sortedSymbols() []semantic.Symbol {
	out := make([]semantic.Symbol, 0, len(w.symbols))
	for _, s := range w.symbols {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func (w *fakeWorkspace) SymbolsByFile(_ context.Context, id ir.FileID) ([]semantic.Symbol, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	var out []semantic.Symbol
	for _, s := range w.sortedSymbols() {
		if s.Source != nil && s.Source.FileID == id {
			out = append(out, s)
		}
	}
	return out, nil
}

func (w *fakeWorkspace) EachSymbol(_ context.Context, fn func(semantic.Symbol) error) error {
	w.mu.Lock()
	symbols := w.sortedSymbols()
	w.mu.Unlock()
	for _, s := range symbols {
		if err := fn(s); err != nil {
			return err
		}
	}
	return nil
}

func (w *fakeWorkspace) PutLookups(_ context.Context, id ir.FileID, lookups []semantic.Lookup) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.lookups[id] = append([]semantic.Lookup(nil), lookups...)
	return nil
}

func (w *fakeWorkspace) Lookups(_ context.Context, id ir.FileID) ([]semantic.Lookup, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]semantic.Lookup(nil), w.lookups[id]...), nil
}

func (w *fakeWorkspace) PutIdentities(_ context.Context, f semantic.FileIdentities) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.identities[f.FileID] = f
	return nil
}

func (w *fakeWorkspace) Identities(_ context.Context, id ir.FileID) (semantic.FileIdentities, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	f, ok := w.identities[id]
	if !ok {
		return semantic.FileIdentities{}, semantic.ErrNotFound
	}
	return f, nil
}

func (w *fakeWorkspace) PreviousIdentities(_ context.Context, lineage string) (semantic.FileIdentities, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	f, ok := w.previous[lineage]
	if !ok {
		return semantic.FileIdentities{}, semantic.ErrNotFound
	}
	return f, nil
}

func (w *fakeWorkspace) Entity(ctx context.Context, id ir.FileID, declaration ir.DeclarationID) (string, error) {
	w.mu.Lock()
	f, ok := w.identities[id]
	w.mu.Unlock()
	if !ok {
		in, err := w.File(ctx, id)
		if err != nil {
			return "", err
		}
		if f, err = w.PreviousIdentities(ctx, in.Lineage); err != nil {
			return "", err
		}
	}
	d, ok := f.Declaration(declaration)
	if !ok {
		return "", fmt.Errorf("%w: declaration %s of %s", semantic.ErrNotFound, declaration, id)
	}
	return d.EntityID, nil
}

// fixture parses Java fixtures into a fake workspace and gives every
// declaration a hand-built source symbol with a canonical key.
type fixture struct {
	t   testing.TB
	ws  *fakeWorkspace
	run deployment.RunKey
}

func newFixture(t testing.TB, runID string) *fixture {
	t.Helper()
	return &fixture{t: t, ws: newFakeWorkspace(), run: deployment.RunKey{RepositoryID: "repo", RunID: runID}}
}

var parserOnce struct {
	sync.Mutex
	engine *java.Parser
}

func parseJava(t testing.TB, source ir.Source, content []byte) ir.SourceFile {
	t.Helper()
	parserOnce.Lock()
	defer parserOnce.Unlock()
	if parserOnce.engine == nil {
		engine, err := java.New()
		must(t, err)
		parserOnce.engine = engine
	}
	file, err := parserOnce.engine.Parse(context.Background(), parser.Input{Source: source, Content: content, Limits: parser.DefaultLimits()})
	must(t, err)
	must(t, file.Validate())
	return file
}

// addFile parses one Java file, registers it as affected and returns its input.
func (f *fixture) addFile(path, content string) semantic.SourceInput {
	f.t.Helper()
	sum := sha256.Sum256([]byte(content))
	source := ir.Source{FileID: ir.FileID(f.run.RunID + ":" + path), RepositoryID: "repo", SnapshotID: "snapshot-" + f.run.RunID, Path: path, ContentSHA256: hex.EncodeToString(sum[:]), SizeBytes: uint64(len(content)), Language: "java", LanguageVersion: "21", ModuleID: "app", SourceSetID: "main"}
	file := parseJava(f.t, source, []byte(content))
	in := semantic.SourceInput{Source: source, Lineage: graph.Lineage("repo", "app", "main", path), Affected: true}
	f.ws.inputs = append(f.ws.inputs, in)
	f.ws.syntax[source.FileID] = file
	f.ws.content[source.FileID] = []byte(content)
	must(f.t, f.ws.PutSymbols(context.Background(), sourceSymbols(in, file)))
	return in
}

func (f *fixture) file(in semantic.SourceInput) ir.SourceFile { return f.ws.syntax[in.Source.FileID] }

func (f *fixture) symbolID(in semantic.SourceInput, d ir.DeclarationID) string {
	return "sym:" + string(in.Source.FileID) + ":" + string(d)
}

// declaration finds a declaration by kind and name; the test fails on
// anything but exactly one match.
func (f *fixture) declaration(in semantic.SourceInput, kind ir.DeclarationKind, name string) ir.Declaration {
	f.t.Helper()
	var found []ir.Declaration
	for _, d := range f.file(in).Declarations {
		if d.Kind == kind && d.Name == name {
			found = append(found, d)
		}
	}
	if len(found) != 1 {
		f.t.Fatalf("%s %s: %d declarations", kind, name, len(found))
	}
	return found[0]
}

func (f *fixture) identities(in semantic.SourceInput) semantic.FileIdentities {
	f.t.Helper()
	ids, err := f.ws.Identities(context.Background(), in.Source.FileID)
	must(f.t, err)
	return ids
}

func (f *fixture) entity(in semantic.SourceInput, kind ir.DeclarationKind, name string) string {
	f.t.Helper()
	d, ok := f.identities(in).Declaration(f.declaration(in, kind, name).ID)
	if !ok {
		f.t.Fatalf("%s %s has no identity", kind, name)
	}
	return d.EntityID
}

func (f *fixture) match(files ...semantic.SourceInput) semantic.MatchResult {
	f.t.Helper()
	out, err := (Matcher{}).Match(context.Background(), semantic.MatchRequest{Run: f.run, Files: files}, f.ws)
	must(f.t, err)
	return out
}

// sourceSymbols builds one source symbol per declaration. Types and members
// get canonical keys; parameters, locals and type parameters do not, like a
// resolver that only canonicalizes named members.
func sourceSymbols(in semantic.SourceInput, file ir.SourceFile) []semantic.Symbol {
	byID := map[ir.DeclarationID]ir.Declaration{}
	for _, d := range file.Declarations {
		byID[d.ID] = d
	}
	types := map[ir.TypeRefID]ir.TypeRef{}
	for _, t := range file.Types {
		types[t.ID] = t
	}
	pkg := ""
	if file.Package != nil {
		var parts []string
		for _, s := range file.Package.Name.Segments {
			parts = append(parts, s.Text)
		}
		pkg = strings.Join(parts, ".")
	}
	var qualified func(d ir.Declaration) string
	qualified = func(d ir.Declaration) string {
		owner := pkg
		if d.OwnerID != "" {
			owner = qualified(byID[d.OwnerID])
		}
		name := owner + "." + d.Name
		if d.Callable != nil {
			var params []string
			for _, pid := range d.Callable.ParameterIDs {
				p := byID[pid]
				spelling := "?"
				if p.Variable != nil {
					spelling = types[p.Variable.DeclaredTypeID].Spelling
				}
				params = append(params, spelling)
			}
			name += "(" + strings.Join(params, ",") + ")"
		}
		return name
	}
	var out []semantic.Symbol
	for _, d := range file.Declarations {
		s := semantic.Symbol{ID: "sym:" + string(in.Source.FileID) + ":" + string(d.ID), Name: d.Name, Source: &semantic.SourceSymbol{FileID: in.Source.FileID, DeclarationID: d.ID, Evidence: anchor(in, d.Span)}}
		if d.OwnerID != "" {
			s.OwnerSymbolID = "sym:" + string(in.Source.FileID) + ":" + string(d.OwnerID)
		}
		switch d.Kind {
		case ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationRecord, ir.DeclarationAnnotationType, ir.DeclarationMethod, ir.DeclarationConstructor, ir.DeclarationField, ir.DeclarationEnumConstant:
			owner := pkg
			if d.OwnerID != "" {
				owner = qualified(byID[d.OwnerID])
			}
			s.Key = &semantic.DeclarationKey{OwnerKey: owner, Kind: d.Kind, Name: d.Name, CanonicalSignature: qualified(d)}
		}
		out = append(out, s)
	}
	return out
}
