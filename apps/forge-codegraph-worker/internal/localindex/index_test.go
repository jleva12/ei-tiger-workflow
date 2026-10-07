package localindex

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"sync"
	"testing"

	"golang.org/x/sync/errgroup"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// fakePrevious is a baseline reader that counts calls per lineage and can be
// switched into a failing mode to model a transient backend error.
type fakePrevious struct {
	mu    sync.Mutex
	maps  map[string]semantic.FileIdentities
	calls map[string]int
	fail  error
}

func newFakePrevious() *fakePrevious {
	return &fakePrevious{maps: map[string]semantic.FileIdentities{}, calls: map[string]int{}}
}

func (p *fakePrevious) PreviousIdentities(_ context.Context, lineage string) (semantic.FileIdentities, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.calls[lineage]++
	if p.fail != nil {
		return semantic.FileIdentities{}, p.fail
	}
	if m, ok := p.maps[lineage]; ok {
		return m, nil
	}
	return semantic.FileIdentities{}, semantic.ErrNotFound
}

func (p *fakePrevious) count(lineage string) int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.calls[lineage]
}

func (p *fakePrevious) setFail(err error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.fail = err
}

type fixture struct {
	ctx      context.Context
	dir      string
	checkout string
	cache    *SyntaxCache
	prev     *fakePrevious
	x        *Index
}

const testDigest = "descriptor-digest-1"

func newFixture(t *testing.T, o Options) *fixture {
	t.Helper()
	dir := t.TempDir()
	checkout := filepath.Join(dir, "checkout")
	if err := os.MkdirAll(checkout, 0o755); err != nil {
		t.Fatal(err)
	}
	cache, err := OpenSyntaxCache(filepath.Join(dir, "cache"), 8<<20)
	if err != nil {
		t.Fatal(err)
	}
	prev := newFakePrevious()
	build := bc.BuildContext{SchemaVersion: bc.SchemaVersion, ID: "bc-1", RepositoryID: "repo", SnapshotID: "snap"}
	x, err := Open(context.Background(), filepath.Join(dir, "run"), build, checkout, cache, testDigest, prev, o)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = x.Close()
		_ = cache.Close()
	})
	return &fixture{ctx: context.Background(), dir: dir, checkout: checkout, cache: cache, prev: prev, x: x}
}

func shaOf(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func symID(i int) string { return shaOf([]byte(strconv.Itoa(i)))[:24] }

func input(id, module, set, p string) semantic.SourceInput {
	return semantic.SourceInput{
		Source: ir.Source{
			FileID: ir.FileID(id), RepositoryID: "repo", SnapshotID: "snap", Path: p,
			ContentSHA256: shaOf([]byte(id)), SizeBytes: 10, Language: "java", LanguageVersion: "21",
			BuildContextID: "bc-1", ModuleID: module, SourceSetID: set,
		},
		Lineage: "lineage:" + id,
	}
}

func symbol(i int, fileID string) semantic.Symbol {
	id := symID(i)
	return semantic.Symbol{
		ID:   id,
		Name: "sym" + strconv.Itoa(i),
		Key:  &semantic.DeclarationKey{OwnerKey: "com.example.A", Kind: ir.DeclarationMethod, Name: "m" + strconv.Itoa(i), CanonicalSignature: "(I)V"},
		Source: &semantic.SourceSymbol{
			FileID: ir.FileID(fileID), DeclarationID: ir.DeclarationID("d" + strconv.Itoa(i)),
			Evidence: graph.SourceAnchor{Lineage: "lineage:" + fileID, ContentSHA256: shaOf([]byte(fileID)), Span: ir.Span{Start: ir.Position{ByteOffset: uint64(i), Line: 1}, End: ir.Position{ByteOffset: uint64(i + 10), Line: 1, Column: 10}}},
		},
	}
}

func sampleIR(fileID, sha string, name string) ir.SourceFile {
	return ir.SourceFile{
		SchemaVersion: ir.SchemaVersion,
		Source:        ir.Source{FileID: ir.FileID(fileID), Path: "A.java", ContentSHA256: sha, Language: "java"},
		Producer:      ir.Producer{Name: "test", Version: "1"},
		RootScopeID:   "s0",
		Scopes:        []ir.Scope{{ID: "s0", Kind: ir.ScopeFile, Span: ir.Span{End: ir.Position{ByteOffset: 100, Line: 5}}}},
		Declarations:  []ir.Declaration{{ID: "d1", Kind: ir.DeclarationClass, Name: name, DeclaringScopeID: "s0", Span: ir.Span{End: ir.Position{ByteOffset: 50, Line: 3}}}},
		Coverage:      ir.ExtractionCoverage{Status: ir.ExtractionComplete, FeatureSet: "java"},
	}
}

func TestFilesInventory(t *testing.T) {
	f := newFixture(t, Options{})
	x, ctx := f.x, f.ctx
	if got := x.Build().ID; got != "bc-1" {
		t.Fatalf("Build().ID = %q", got)
	}
	f3 := input("f3", "m2", "s2", "b.java")
	f1 := input("f1", "m1", "s1", "z.java")
	f2 := input("f2", "m1", "s1", "a.java")
	f4 := input("f4", "m1", "s2", "a.java")
	if err := x.PutFiles(ctx, []semantic.SourceInput{f3, f1, f2, f4}); err != nil {
		t.Fatal(err)
	}
	files, err := x.Files(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if want := []semantic.SourceInput{f2, f1, f4, f3}; !reflect.DeepEqual(files, want) {
		t.Fatalf("Files order/content mismatch:\n got %+v\nwant %+v", files, want)
	}
	got, err := x.File(ctx, "f1")
	if err != nil || !reflect.DeepEqual(got, f1) {
		t.Fatalf("File(f1) = %+v, %v", got, err)
	}
	got, err = x.FileByPath(ctx, "s1", "z.java")
	if err != nil || !reflect.DeepEqual(got, f1) {
		t.Fatalf("FileByPath(s1, z.java) = %+v, %v", got, err)
	}
	if _, err := x.File(ctx, "nope"); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("File(nope) err = %v", err)
	}
	if _, err := x.FileByPath(ctx, "s1", "nope.java"); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("FileByPath(nope) err = %v", err)
	}

	if err := x.SetAffected(ctx, []ir.FileID{"f1", "f4"}); err != nil {
		t.Fatal(err)
	}
	files, err = x.Files(ctx)
	if err != nil {
		t.Fatal(err)
	}
	for _, in := range files {
		want := in.Source.FileID == "f1" || in.Source.FileID == "f4"
		if in.Affected != want {
			t.Fatalf("%s affected=%v want %v", in.Source.FileID, in.Affected, want)
		}
	}
	if err := x.SetAffected(ctx, []ir.FileID{"f2", "missing"}); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("SetAffected(missing) err = %v", err)
	}
	if got, _ := x.File(ctx, "f2"); got.Affected {
		t.Fatal("SetAffected with an unknown ID must roll back entirely")
	}

	// Replace by file ID: new hash, affected flag taken from the input.
	f1b := f1
	f1b.Source.ContentSHA256 = shaOf([]byte("changed"))
	if err := x.PutFiles(ctx, []semantic.SourceInput{f1b}); err != nil {
		t.Fatal(err)
	}
	got, err = x.File(ctx, "f1")
	if err != nil || got.Source.ContentSHA256 != f1b.Source.ContentSHA256 || got.Affected {
		t.Fatalf("replaced File(f1) = %+v, %v", got, err)
	}
	// Replace by (source set, path): the old file ID disappears.
	f5 := input("f5", "m1", "s1", "a.java")
	if err := x.PutFiles(ctx, []semantic.SourceInput{f5}); err != nil {
		t.Fatal(err)
	}
	if got, err := x.FileByPath(ctx, "s1", "a.java"); err != nil || got.Source.FileID != "f5" {
		t.Fatalf("FileByPath after replace = %+v, %v", got, err)
	}
	if _, err := x.File(ctx, "f2"); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("File(f2) after path replace err = %v", err)
	}
	if files, _ = x.Files(ctx); len(files) != 4 {
		t.Fatalf("Files len = %d, want 4", len(files))
	}
	if err := x.PutFiles(ctx, []semantic.SourceInput{{Lineage: "l"}}); !errors.Is(err, semantic.ErrInvalid) {
		t.Fatalf("PutFiles(empty id) err = %v", err)
	}
	if err := x.PutFiles(ctx, []semantic.SourceInput{{Source: ir.Source{FileID: "f9"}}}); !errors.Is(err, semantic.ErrInvalid) {
		t.Fatalf("PutFiles(empty lineage) err = %v", err)
	}
}

func TestSymbols10k(t *testing.T) {
	f := newFixture(t, Options{})
	x, ctx := f.x, f.ctx
	const n = 10_000
	syms := make([]semantic.Symbol, n)
	for i := range syms {
		syms[i] = symbol(i, "file-"+strconv.Itoa(i%10))
	}
	rng := rand.New(rand.NewSource(7))
	rng.Shuffle(n, func(i, j int) { syms[i], syms[j] = syms[j], syms[i] })
	for i := 0; i < n; i += 1000 {
		if err := x.PutSymbols(ctx, syms[i:i+1000]); err != nil {
			t.Fatal(err)
		}
	}
	for i := range n {
		want := symbol(i, "file-"+strconv.Itoa(i%10))
		got, err := x.Symbol(ctx, want.ID)
		if err != nil {
			t.Fatalf("Symbol(%d): %v", i, err)
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("Symbol(%d) = %+v want %+v", i, got, want)
		}
	}
	if _, err := x.Symbol(ctx, "missing"); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("Symbol(missing) err = %v", err)
	}

	ids := make([]string, 0, 1240)
	for i := 0; i < 1234; i++ {
		ids = append(ids, symID(i*7%n))
	}
	ids = append(ids, symID(0), "missing-1", "missing-2", symID(0), symID(3), symID(3))
	got, err := x.Symbols(ctx, ids)
	if err != nil {
		t.Fatal(err)
	}
	unique := map[string]bool{}
	for _, id := range ids {
		if id[:8] != "missing-" {
			unique[id] = true
		}
	}
	if len(got) != len(unique) {
		t.Fatalf("Symbols returned %d, want %d", len(got), len(unique))
	}
	for id := range unique {
		if got[id].ID != id {
			t.Fatalf("Symbols missing %s", id)
		}
	}
	if _, ok := got["missing-1"]; ok {
		t.Fatal("Symbols must not invent rows")
	}
	if empty, err := x.Symbols(ctx, nil); err != nil || len(empty) != 0 {
		t.Fatalf("Symbols(nil) = %v, %v", empty, err)
	}

	byFile, err := x.SymbolsByFile(ctx, "file-7")
	if err != nil {
		t.Fatal(err)
	}
	if len(byFile) != n/10 {
		t.Fatalf("SymbolsByFile = %d rows, want %d", len(byFile), n/10)
	}
	for i := range byFile {
		if byFile[i].Source.FileID != "file-7" {
			t.Fatalf("SymbolsByFile row %d belongs to %s", i, byFile[i].Source.FileID)
		}
		if i > 0 && byFile[i-1].ID >= byFile[i].ID {
			t.Fatalf("SymbolsByFile not ordered at %d", i)
		}
	}
	if none, err := x.SymbolsByFile(ctx, "file-none"); err != nil || len(none) != 0 {
		t.Fatalf("SymbolsByFile(none) = %v, %v", none, err)
	}

	var seen []string
	if err := x.EachSymbol(ctx, func(s semantic.Symbol) error { seen = append(seen, s.ID); return nil }); err != nil {
		t.Fatal(err)
	}
	if len(seen) != n {
		t.Fatalf("EachSymbol visited %d, want %d", len(seen), n)
	}
	for i := 1; i < len(seen); i++ {
		if seen[i-1] >= seen[i] {
			t.Fatalf("EachSymbol out of order at %d: %s >= %s", i, seen[i-1], seen[i])
		}
	}
	stop := errors.New("stop")
	calls := 0
	if err := x.EachSymbol(ctx, func(semantic.Symbol) error {
		calls++
		if calls == 5 {
			return stop
		}
		return nil
	}); !errors.Is(err, stop) || calls != 5 {
		t.Fatalf("EachSymbol early stop: err=%v calls=%d", err, calls)
	}

	// Replace keeps the row count and updates the payload.
	replaced := symbol(42, "file-2")
	replaced.Name = "renamed"
	if err := x.PutSymbols(ctx, []semantic.Symbol{replaced}); err != nil {
		t.Fatal(err)
	}
	if got, err := x.Symbol(ctx, replaced.ID); err != nil || got.Name != "renamed" {
		t.Fatalf("replaced Symbol = %+v, %v", got, err)
	}
	count := 0
	_ = x.EachSymbol(ctx, func(semantic.Symbol) error { count++; return nil })
	if count != n {
		t.Fatalf("count after replace = %d", count)
	}
	if err := x.PutSymbols(ctx, []semantic.Symbol{{Name: "no id"}}); !errors.Is(err, semantic.ErrInvalid) {
		t.Fatalf("PutSymbols(no id) err = %v", err)
	}
	// Non-source symbols carry an empty file ID and still round-trip.
	ext := semantic.Symbol{ID: "ext-1", External: &semantic.ExternalSymbol{ArtifactID: "a", ArtifactFingerprint: "fp"}}
	if err := x.PutSymbols(ctx, []semantic.Symbol{ext}); err != nil {
		t.Fatal(err)
	}
	if got, err := x.Symbol(ctx, "ext-1"); err != nil || !reflect.DeepEqual(got, ext) {
		t.Fatalf("external Symbol = %+v, %v", got, err)
	}
}

func lookup(fileID string, i int) semantic.Lookup {
	return semantic.Lookup{
		ID: fmt.Sprintf("%s#%d", fileID, i), FileID: ir.FileID(fileID), OccurrenceID: ir.OccurrenceID("o" + strconv.Itoa(i)),
		Kind: semantic.LookupCall, Status: semantic.LookupResolved, SelectedSymbolID: symID(i),
		CandidateIDs: []string{symID(i), symID(i + 1)},
		Evidence:     graph.SourceAnchor{Lineage: "lineage:" + fileID, ContentSHA256: shaOf([]byte(fileID))},
	}
}

func TestLookupsReplace(t *testing.T) {
	f := newFixture(t, Options{})
	x, ctx := f.x, f.ctx
	first := []semantic.Lookup{lookup("fa", 0), lookup("fa", 1), lookup("fa", 2)}
	if err := x.PutLookups(ctx, "fa", first); err != nil {
		t.Fatal(err)
	}
	other := []semantic.Lookup{lookup("fb", 9)}
	if err := x.PutLookups(ctx, "fb", other); err != nil {
		t.Fatal(err)
	}
	got, err := x.Lookups(ctx, "fa")
	if err != nil || !reflect.DeepEqual(got, first) {
		t.Fatalf("Lookups(fa) = %+v, %v", got, err)
	}
	second := []semantic.Lookup{lookup("fa", 5), lookup("fa", 4)}
	second[1].Status = semantic.LookupUnresolved
	second[1].Cause = semantic.CauseExternalDependency
	if err := x.PutLookups(ctx, "fa", second); err != nil {
		t.Fatal(err)
	}
	got, err = x.Lookups(ctx, "fa")
	if err != nil || !reflect.DeepEqual(got, second) {
		t.Fatalf("Lookups(fa) after replace = %+v, %v", got, err)
	}
	if got, err := x.Lookups(ctx, "fb"); err != nil || !reflect.DeepEqual(got, other) {
		t.Fatalf("Lookups(fb) disturbed = %+v, %v", got, err)
	}
	if err := x.PutLookups(ctx, "fa", nil); err != nil {
		t.Fatal(err)
	}
	if got, err := x.Lookups(ctx, "fa"); err != nil || len(got) != 0 {
		t.Fatalf("Lookups(fa) after clear = %+v, %v", got, err)
	}
	if got, err := x.Lookups(ctx, "unknown"); err != nil || len(got) != 0 {
		t.Fatalf("Lookups(unknown) = %+v, %v", got, err)
	}
}

func identities(fileID, lineage string, decls map[string]string) semantic.FileIdentities {
	f := semantic.FileIdentities{Lineage: lineage, FileID: ir.FileID(fileID), Path: fileID + ".java", ContentSHA256: shaOf([]byte(fileID))}
	for d, e := range decls {
		f.Declarations = append(f.Declarations, semantic.DeclarationIdentity{DeclarationID: ir.DeclarationID(d), EntityID: e, Kind: ir.DeclarationMethod, Name: d})
	}
	return f
}

func TestIdentitiesAndEntity(t *testing.T) {
	f := newFixture(t, Options{PreviousCacheEntries: 2})
	x, ctx, prev := f.x, f.ctx, f.prev
	fA := input("fA", "m", "s", "A.java")
	fA.Affected = true
	fB := input("fB", "m", "s", "B.java")
	fC := input("fC", "m", "s", "C.java") // renamed from lineage "old:C"
	fD := input("fD", "m", "s", "D.java") // new file, no baseline
	if err := x.PutFiles(ctx, []semantic.SourceInput{fA, fB, fC, fD}); err != nil {
		t.Fatal(err)
	}
	prev.maps[fB.Lineage] = identities("fB-prev", fB.Lineage, map[string]string{"d1": "E-B1"})
	prev.maps["old:C"] = identities("fC-prev", "old:C", map[string]string{"d1": "E-C1"})

	idsA := identities("fA", fA.Lineage, map[string]string{"d1": "E-A1"})
	idsA.Declarations = append(idsA.Declarations, semantic.DeclarationIdentity{DeclarationID: "d2", EntityID: "E-A2", Kind: ir.DeclarationField, Name: "x", Span: ir.Span{Start: ir.Position{ByteOffset: 5, Line: 2, Column: 1}, End: ir.Position{ByteOffset: 9, Line: 2, Column: 5}}, OwnerID: "d1", Key: &semantic.DeclarationKey{OwnerKey: "A", Kind: ir.DeclarationField, Name: "x"}})
	idsA.Occurrences = []semantic.OccurrenceIdentity{{OccurrenceID: "o1", PersistentID: "P-1", EnclosingDeclarationID: "d1", EnclosingEntityID: "E-A1"}}
	if err := x.PutIdentities(ctx, idsA); err != nil {
		t.Fatal(err)
	}
	got, err := x.Identities(ctx, "fA")
	if err != nil || !reflect.DeepEqual(got, idsA) {
		t.Fatalf("Identities(fA) = %+v, %v", got, err)
	}
	if _, err := x.Identities(ctx, "fB"); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("Identities(fB) err = %v", err)
	}
	if err := x.PutIdentities(ctx, semantic.FileIdentities{Lineage: "l"}); !errors.Is(err, semantic.ErrInvalid) {
		t.Fatalf("PutIdentities(empty) err = %v", err)
	}

	// Current-run entities.
	for decl, want := range map[ir.DeclarationID]string{"d1": "E-A1", "d2": "E-A2"} {
		if got, err := x.Entity(ctx, "fA", decl); err != nil || got != want {
			t.Fatalf("Entity(fA,%s) = %q, %v", decl, got, err)
		}
	}
	// Affected file, unknown declaration: falls through to a baseline that
	// does not exist for its lineage.
	if _, err := x.Entity(ctx, "fA", "d9"); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("Entity(fA,d9) err = %v", err)
	}
	if prev.count(fA.Lineage) != 1 {
		t.Fatalf("baseline for fA fetched %d times", prev.count(fA.Lineage))
	}
	// Unchanged file through the previous map, fetched exactly once.
	for range 3 {
		if got, err := x.Entity(ctx, "fB", "d1"); err != nil || got != "E-B1" {
			t.Fatalf("Entity(fB,d1) = %q, %v", got, err)
		}
	}
	if _, err := x.Entity(ctx, "fB", "d2"); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("Entity(fB,d2) err = %v", err)
	}
	if prev.count(fB.Lineage) != 1 {
		t.Fatalf("baseline for fB fetched %d times", prev.count(fB.Lineage))
	}
	if _, err := x.Entity(ctx, "unknown-file", "d1"); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("Entity(unknown) err = %v", err)
	}
	// Rename alias.
	x.AliasPrevious(fC.Lineage, "old:C")
	p, err := x.PreviousIdentities(ctx, fC.Lineage)
	if err != nil || p.Lineage != "old:C" || p.FileID != "fC-prev" {
		t.Fatalf("PreviousIdentities(alias) = %+v, %v", p, err)
	}
	if got, err := x.Entity(ctx, "fC", "d1"); err != nil || got != "E-C1" {
		t.Fatalf("Entity(fC,d1) = %q, %v", got, err)
	}
	if prev.count(fC.Lineage) != 0 || prev.count("old:C") != 1 {
		t.Fatalf("alias fetch counts: new=%d old=%d", prev.count(fC.Lineage), prev.count("old:C"))
	}
	// Missing baseline propagates ErrNotFound and is negatively cached.
	for range 2 {
		if _, err := x.PreviousIdentities(ctx, fD.Lineage); !errors.Is(err, semantic.ErrNotFound) {
			t.Fatalf("PreviousIdentities(fD) err = %v", err)
		}
	}
	if prev.count(fD.Lineage) != 1 {
		t.Fatalf("missing baseline fetched %d times", prev.count(fD.Lineage))
	}
	// The LRU holds 2 entries; fB was evicted from memory but stays in the
	// previous table, so the reader is still not consulted again.
	if x.prevLRU.len() != 2 {
		t.Fatalf("previous LRU len = %d, want 2", x.prevLRU.len())
	}
	if got, err := x.Entity(ctx, "fB", "d1"); err != nil || got != "E-B1" {
		t.Fatalf("Entity(fB,d1) after eviction = %q, %v", got, err)
	}
	if prev.count(fB.Lineage) != 1 {
		t.Fatalf("baseline for fB refetched: %d", prev.count(fB.Lineage))
	}
	// Transient backend errors propagate and are not cached.
	boom := errors.New("spanner unavailable")
	prev.setFail(boom)
	if _, err := x.PreviousIdentities(ctx, "lineage:fE"); !errors.Is(err, boom) {
		t.Fatalf("transient err = %v", err)
	}
	prev.setFail(nil)
	prev.maps["lineage:fE"] = identities("fE-prev", "lineage:fE", map[string]string{"d1": "E-E1"})
	if p, err := x.PreviousIdentities(ctx, "lineage:fE"); err != nil || p.FileID != "fE-prev" {
		t.Fatalf("after transient = %+v, %v", p, err)
	}
	if prev.count("lineage:fE") != 2 {
		t.Fatalf("transient error was cached: %d calls", prev.count("lineage:fE"))
	}
	// Re-putting identities rebuilds the entity rows.
	if err := x.PutIdentities(ctx, identities("fA", fA.Lineage, map[string]string{"d1": "E-A1b"})); err != nil {
		t.Fatal(err)
	}
	if got, err := x.Entity(ctx, "fA", "d1"); err != nil || got != "E-A1b" {
		t.Fatalf("Entity(fA,d1) after re-put = %q, %v", got, err)
	}
	if _, err := x.Entity(ctx, "fA", "d2"); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("Entity(fA,d2) after re-put err = %v", err)
	}
}

func TestSourceBytes(t *testing.T) {
	f := newFixture(t, Options{})
	x, ctx := f.x, f.ctx
	content := []byte("package a;\nclass Hello {}\n")
	rel := filepath.Join("src", "a", "Hello.java")
	if err := os.MkdirAll(filepath.Dir(filepath.Join(f.checkout, rel)), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(f.checkout, rel), content, 0o644); err != nil {
		t.Fatal(err)
	}
	in := semantic.SourceInput{Source: ir.Source{FileID: "h", Path: "src/a/Hello.java", ContentSHA256: shaOf(content), SizeBytes: uint64(len(content))}, Lineage: "l"}
	got, err := x.SourceBytes(ctx, in)
	if err != nil || string(got) != string(content) {
		t.Fatalf("SourceBytes = %q, %v", got, err)
	}
	// Tampered content of the same size.
	tampered := append([]byte(nil), content...)
	tampered[0] = 'P'
	if err := os.WriteFile(filepath.Join(f.checkout, rel), tampered, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := x.SourceBytes(ctx, in); !errors.Is(err, semantic.ErrIntegrity) {
		t.Fatalf("tampered err = %v", err)
	}
	// Size mismatch.
	if err := os.WriteFile(filepath.Join(f.checkout, rel), content, 0o644); err != nil {
		t.Fatal(err)
	}
	short := in
	short.Source.SizeBytes--
	if _, err := x.SourceBytes(ctx, short); !errors.Is(err, semantic.ErrIntegrity) {
		t.Fatalf("size mismatch err = %v", err)
	}
	// Missing file.
	missing := in
	missing.Source.Path = "src/a/Nope.java"
	if _, err := x.SourceBytes(ctx, missing); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("missing err = %v", err)
	}
	// Symlink escaping the checkout is refused even when the hash matches.
	outside := filepath.Join(t.TempDir(), "secret.java")
	if err := os.WriteFile(outside, content, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(f.checkout, "escape.java")); err != nil {
		t.Fatal(err)
	}
	link := in
	link.Source.Path = "escape.java"
	if _, err := x.SourceBytes(ctx, link); !errors.Is(err, semantic.ErrIntegrity) {
		t.Fatalf("symlink escape err = %v", err)
	}
	// Symlink inside the checkout is followed.
	if err := os.Symlink(filepath.Join("src", "a", "Hello.java"), filepath.Join(f.checkout, "inside.java")); err != nil {
		t.Fatal(err)
	}
	link.Source.Path = "inside.java"
	if got, err := x.SourceBytes(ctx, link); err != nil || string(got) != string(content) {
		t.Fatalf("in-root symlink = %q, %v", got, err)
	}
	// Directory traversal and absolute paths are invalid requests.
	for _, bad := range []string{"../x.java", "src/../../x.java", "/etc/passwd", "", ".", "src/a/", "./src/a/Hello.java"} {
		b := in
		b.Source.Path = bad
		if _, err := x.SourceBytes(ctx, b); !errors.Is(err, semantic.ErrInvalid) {
			t.Fatalf("path %q err = %v", bad, err)
		}
	}
	dir := in
	dir.Source.Path = "src/a"
	if _, err := x.SourceBytes(ctx, dir); !errors.Is(err, semantic.ErrInvalid) {
		t.Fatalf("directory err = %v", err)
	}
}

func TestSyntaxThroughCache(t *testing.T) {
	f := newFixture(t, Options{})
	x, ctx := f.x, f.ctx
	sha := shaOf([]byte("A.java v1"))
	in := semantic.SourceInput{Source: ir.Source{FileID: "fa", Path: "A.java", ContentSHA256: sha}, Lineage: "l", Affected: true}
	if _, err := x.Syntax(ctx, in); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("Syntax miss err = %v", err)
	}
	want := sampleIR("fa", sha, "A")
	key := SyntaxKey(testDigest, bc.SourceSet{}, in)
	if err := f.cache.Put(ctx, sha, key, want); err != nil {
		t.Fatal(err)
	}
	// Cached IR is rebound to the identity of the variant being read.
	want.Source = in.Source
	got, err := x.Syntax(ctx, in)
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("Syntax = %+v, %v", got, err)
	}
	// The in-memory LRU serves repeat reads: replacing the cache row under the
	// same key is not visible until the LRU entry is evicted.
	if err := f.cache.Put(ctx, sha, key, sampleIR("fa", sha, "Changed")); err != nil {
		t.Fatal(err)
	}
	if got, err := x.Syntax(ctx, in); err != nil || got.Declarations[0].Name != "A" {
		t.Fatalf("LRU bypassed: %+v, %v", got, err)
	}
	for i := range syntaxLRUSize {
		other := shaOf([]byte("other" + strconv.Itoa(i)))
		otherIn := semantic.SourceInput{Source: ir.Source{ContentSHA256: other}}
		if err := f.cache.Put(ctx, other, SyntaxKey(testDigest, bc.SourceSet{}, otherIn), sampleIR("o", other, "O")); err != nil {
			t.Fatal(err)
		}
		if _, err := x.Syntax(ctx, otherIn); err != nil {
			t.Fatal(err)
		}
	}
	if got, err := x.Syntax(ctx, in); err != nil || got.Declarations[0].Name != "Changed" {
		t.Fatalf("after LRU eviction: %+v, %v", got, err)
	}
	// A different descriptor digest is a different cache key.
	other := shaOf([]byte("B.java"))
	if err := f.cache.Put(ctx, other, "another-digest", sampleIR("fb", other, "B")); err != nil {
		t.Fatal(err)
	}
	if _, err := x.Syntax(ctx, semantic.SourceInput{Source: ir.Source{ContentSHA256: other}}); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("digest mismatch err = %v", err)
	}
	if _, err := x.Syntax(ctx, semantic.SourceInput{}); !errors.Is(err, semantic.ErrInvalid) {
		t.Fatalf("empty hash err = %v", err)
	}
	// No cache attached at all.
	y, err := Open(ctx, filepath.Join(f.dir, "run2"), x.Build(), f.checkout, nil, testDigest, nil, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer y.Close()
	if _, err := y.Syntax(ctx, in); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("nil cache err = %v", err)
	}
	if _, err := y.PreviousIdentities(ctx, "any"); !errors.Is(err, semantic.ErrNotFound) {
		t.Fatalf("nil previous err = %v", err)
	}
}

func TestCloseDeletesFiles(t *testing.T) {
	f := newFixture(t, Options{})
	x, ctx := f.x, f.ctx
	if err := x.PutSymbols(ctx, []semantic.Symbol{symbol(1, "f")}); err != nil {
		t.Fatal(err)
	}
	runDir := filepath.Join(f.dir, "run")
	if _, err := os.Stat(filepath.Join(runDir, indexFile)); err != nil {
		t.Fatalf("index file missing before close: %v", err)
	}
	if err := x.Close(); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(runDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("run dir not emptied: %v", entries)
	}
	if err := x.Close(); err != nil {
		t.Fatalf("second Close = %v", err)
	}
	if _, err := x.Symbol(ctx, "x"); err == nil {
		t.Fatal("Symbol after Close must fail")
	}
	if err := x.PutSymbols(ctx, nil); err == nil {
		t.Fatal("PutSymbols after Close must fail")
	}
	// Open replaces a stale index file.
	if err := os.WriteFile(filepath.Join(runDir, indexFile), []byte("garbage"), 0o600); err != nil {
		t.Fatal(err)
	}
	y, err := Open(ctx, runDir, x.Build(), f.checkout, nil, testDigest, nil, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if err := y.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestConcurrentReadersDuringWrites(t *testing.T) {
	f := newFixture(t, Options{PreviousCacheEntries: 16})
	x, ctx, prev := f.x, f.ctx, f.prev
	const n, batch, readers = 8000, 250, 8
	files := make([]semantic.SourceInput, 20)
	for i := range files {
		files[i] = input("cf"+strconv.Itoa(i), "m", "s", "F"+strconv.Itoa(i)+".java")
		prev.maps[files[i].Lineage] = identities("prev"+strconv.Itoa(i), files[i].Lineage, map[string]string{"d1": "E" + strconv.Itoa(i)})
	}
	if err := x.PutFiles(ctx, files); err != nil {
		t.Fatal(err)
	}
	syms := make([]semantic.Symbol, n)
	ids := make([]string, n)
	for i := range syms {
		syms[i] = symbol(i, "cf"+strconv.Itoa(i%20))
		ids[i] = syms[i].ID
	}
	done := make(chan struct{})
	g, gctx := errgroup.WithContext(ctx)
	// Symbol writer.
	g.Go(func() error {
		defer close(done)
		for i := 0; i < n; i += batch {
			if err := x.PutSymbols(gctx, syms[i:i+batch]); err != nil {
				return err
			}
		}
		return nil
	})
	// Lookup and identity writer competing for the write slot.
	g.Go(func() error {
		for i := 0; i < 200; i++ {
			fid := "cf" + strconv.Itoa(i%20)
			if err := x.PutLookups(gctx, ir.FileID(fid), []semantic.Lookup{lookup(fid, i), lookup(fid, i+1)}); err != nil {
				return err
			}
			if err := x.PutIdentities(gctx, identities(fid, "lineage:"+fid, map[string]string{"d2": "cur" + strconv.Itoa(i)})); err != nil {
				return err
			}
		}
		return nil
	})
	for r := range readers {
		g.Go(func() error {
			rng := rand.New(rand.NewSource(int64(r)))
			for iter := 0; ; iter++ {
				select {
				case <-done:
					if iter > 0 {
						return nil
					}
				default:
				}
				id := ids[rng.Intn(n)]
				if s, err := x.Symbol(gctx, id); err != nil {
					if !errors.Is(err, semantic.ErrNotFound) {
						return err
					}
				} else if s.ID != id {
					return fmt.Errorf("Symbol(%s) returned %s", id, s.ID)
				}
				start := rng.Intn(n - 600)
				if m, err := x.Symbols(gctx, ids[start:start+600]); err != nil {
					return err
				} else {
					for k, v := range m {
						if k != v.ID {
							return fmt.Errorf("Symbols mismatch %s/%s", k, v.ID)
						}
					}
				}
				last := ""
				if err := x.EachSymbol(gctx, func(s semantic.Symbol) error {
					if s.ID <= last {
						return fmt.Errorf("EachSymbol order %s <= %s", s.ID, last)
					}
					last = s.ID
					return nil
				}); err != nil {
					return err
				}
				fid := ir.FileID("cf" + strconv.Itoa(rng.Intn(20)))
				if _, err := x.SymbolsByFile(gctx, fid); err != nil {
					return err
				}
				if _, err := x.Lookups(gctx, fid); err != nil {
					return err
				}
				if e, err := x.Entity(gctx, fid, "d1"); err != nil || e != "E"+string(fid[2:]) {
					return fmt.Errorf("Entity(%s,d1) = %q, %v", fid, e, err)
				}
				if _, err := x.Entity(gctx, fid, "d2"); err != nil && !errors.Is(err, semantic.ErrNotFound) {
					return err
				}
				if _, err := x.PreviousIdentities(gctx, "lineage:"+string(fid)); err != nil {
					return err
				}
				if _, err := x.Files(gctx); err != nil {
					return err
				}
			}
		})
	}
	if err := g.Wait(); err != nil {
		t.Fatal(err)
	}
	count := 0
	if err := x.EachSymbol(ctx, func(semantic.Symbol) error { count++; return nil }); err != nil {
		t.Fatal(err)
	}
	if count != n {
		t.Fatalf("final symbol count = %d, want %d", count, n)
	}
	// Every baseline was fetched at most once despite concurrent misses, and
	// touching each file now completes the set without new fetches.
	for _, in := range files {
		if e, err := x.Entity(ctx, in.Source.FileID, "d1"); err != nil || e != "E"+string(in.Source.FileID[2:]) {
			t.Fatalf("Entity(%s,d1) = %q, %v", in.Source.FileID, e, err)
		}
		if prev.count(in.Lineage) != 1 {
			t.Fatalf("baseline %s fetched %d times", in.Lineage, prev.count(in.Lineage))
		}
	}
}
