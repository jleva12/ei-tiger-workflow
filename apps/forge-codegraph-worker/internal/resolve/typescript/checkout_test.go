package typescript_test

// A checkout-driven resolution run: set CODEGRAPH_TEST_TS_CHECKOUT to a
// repository directory to parse every TypeScript/JavaScript file with the
// real adapter, discover its project configuration, resolve it with the
// syntax-tier resolver against an in-memory workspace, and print the
// resolution statistics. It skips without the variable.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/languages/typescript/packages"
	"ei-aitiger-codegraph/worker/internal/languages/typescript/project"
	tsparser "ei-aitiger-codegraph/worker/internal/parser/typescript"
	tsresolve "ei-aitiger-codegraph/worker/internal/resolve/typescript"
)

type memoryWorkspace struct {
	mu       sync.Mutex
	build    bc.BuildContext
	files    []semantic.SourceInput
	syntax   map[ir.FileID]ir.SourceFile
	bytes    map[ir.FileID][]byte
	symbols  map[string]semantic.Symbol
	lookups  map[ir.FileID][]semantic.Lookup
	previous map[string]semantic.FileIdentities
}

func (w *memoryWorkspace) Build() bc.BuildContext { return w.build }
func (w *memoryWorkspace) Files(context.Context) ([]semantic.SourceInput, error) {
	out := append([]semantic.SourceInput{}, w.files...)
	sort.Slice(out, func(i, j int) bool { return out[i].Source.Path < out[j].Source.Path })
	return out, nil
}
func (w *memoryWorkspace) File(_ context.Context, id ir.FileID) (semantic.SourceInput, error) {
	for _, f := range w.files {
		if f.Source.FileID == id {
			return f, nil
		}
	}
	return semantic.SourceInput{}, semantic.ErrNotFound
}
func (w *memoryWorkspace) FileByPath(_ context.Context, set bc.SourceSetID, path string) (semantic.SourceInput, error) {
	for _, f := range w.files {
		if f.Source.SourceSetID == string(set) && f.Source.Path == path {
			return f, nil
		}
	}
	return semantic.SourceInput{}, semantic.ErrNotFound
}
func (w *memoryWorkspace) Syntax(_ context.Context, in semantic.SourceInput) (ir.SourceFile, error) {
	f, ok := w.syntax[in.Source.FileID]
	if !ok {
		return ir.SourceFile{}, semantic.ErrNotFound
	}
	return f, nil
}
func (w *memoryWorkspace) SourceBytes(_ context.Context, in semantic.SourceInput) ([]byte, error) {
	b, ok := w.bytes[in.Source.FileID]
	if !ok {
		return nil, semantic.ErrNotFound
	}
	return b, nil
}
func (w *memoryWorkspace) PutSymbols(_ context.Context, symbols []semantic.Symbol) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, s := range symbols {
		w.symbols[s.ID] = s
	}
	return nil
}
func (w *memoryWorkspace) Symbol(_ context.Context, id string) (semantic.Symbol, error) {
	s, ok := w.symbols[id]
	if !ok {
		return semantic.Symbol{}, semantic.ErrNotFound
	}
	return s, nil
}
func (w *memoryWorkspace) Symbols(_ context.Context, ids []string) (map[string]semantic.Symbol, error) {
	out := map[string]semantic.Symbol{}
	for _, id := range ids {
		if s, ok := w.symbols[id]; ok {
			out[id] = s
		}
	}
	return out, nil
}
func (w *memoryWorkspace) SymbolsByFile(_ context.Context, id ir.FileID) ([]semantic.Symbol, error) {
	var out []semantic.Symbol
	for _, s := range w.symbols {
		if s.Source != nil && s.Source.FileID == id {
			out = append(out, s)
		}
	}
	return out, nil
}
func (w *memoryWorkspace) EachSymbol(_ context.Context, fn func(semantic.Symbol) error) error {
	for _, s := range w.symbols {
		if err := fn(s); err != nil {
			return err
		}
	}
	return nil
}
func (w *memoryWorkspace) PutLookups(_ context.Context, id ir.FileID, lookups []semantic.Lookup) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.lookups[id] = append([]semantic.Lookup{}, lookups...)
	return nil
}
func (w *memoryWorkspace) Lookups(_ context.Context, id ir.FileID) ([]semantic.Lookup, error) {
	return w.lookups[id], nil
}
func (w *memoryWorkspace) PutIdentities(context.Context, semantic.FileIdentities) error { return nil }
func (w *memoryWorkspace) Identities(context.Context, ir.FileID) (semantic.FileIdentities, error) {
	return semantic.FileIdentities{}, semantic.ErrNotFound
}
func (w *memoryWorkspace) PreviousIdentities(_ context.Context, lineage string) (semantic.FileIdentities, error) {
	f, ok := w.previous[lineage]
	if !ok {
		return semantic.FileIdentities{}, semantic.ErrNotFound
	}
	return f, nil
}
func (w *memoryWorkspace) Entity(context.Context, ir.FileID, ir.DeclarationID) (string, error) {
	return "", semantic.ErrNotFound
}

func TestResolveCheckout(t *testing.T) {
	checkout := os.Getenv("CODEGRAPH_TEST_TS_CHECKOUT")
	if checkout == "" {
		t.Skip("set CODEGRAPH_TEST_TS_CHECKOUT to a repository directory")
	}
	ctx := context.Background()
	cfg := project.Config{Version: "5", DefaultExcludes: true}
	// CODEGRAPH_TEST_TS_INSTALL_CACHE installs the checkout's npm projects
	// into that cache, as the worker does.
	if cache := os.Getenv("CODEGRAPH_TEST_TS_INSTALL_CACHE"); cache != "" {
		cfg.Install = &packages.Config{NPM: "npm", NPMVersion: "checkout-test", CacheDir: cache, MaxBytes: 4 << 30, Timeout: 15 * time.Minute}
		cfg.Compiler = "checkout-test"
	}
	provider, err := project.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	commit := strings.Repeat("0", 40)
	build, err := provider.Build(ctx, bc.Request{Checkout: bc.Checkout{Path: checkout, RepositoryID: "repo", SnapshotID: commit}, Limits: bc.DefaultLimits()})
	if err != nil {
		t.Fatal(err)
	}
	// CODEGRAPH_TEST_TS_INSTALLS=dir=/abs/installation,... hands the
	// compiler packages installed for projects of the checkout.
	if installs := os.Getenv("CODEGRAPH_TEST_TS_INSTALLS"); installs != "" {
		settings, err := tsresolve.DecodeSettings(build.Inventory.SourceSets[0].LanguageOptions)
		if err != nil {
			t.Fatal(err)
		}
		for _, entry := range strings.Split(installs, ",") {
			dir, path, _ := strings.Cut(entry, "=")
			settings.Installs = append(settings.Installs, tsresolve.Install{Dir: dir, Path: path, SHA256: strings.Repeat("0", 64)})
		}
		options, err := tsresolve.EncodeSettings(settings)
		if err != nil {
			t.Fatal(err)
		}
		build.Inventory.SourceSets[0].LanguageOptions = options
	}
	for _, gap := range build.Inventory.MissingInputs {
		fmt.Printf("gap %s: %s\n", gap.Requested, gap.Reason)
	}
	set := build.Inventory.SourceSets[0]
	registration := tsparser.Registration()
	p, err := tsparser.New()
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close(ctx)
	w := &memoryWorkspace{build: build, syntax: map[ir.FileID]ir.SourceFile{}, bytes: map[ir.FileID][]byte{}, symbols: map[string]semantic.Symbol{}, lookups: map[ir.FileID][]semantic.Lookup{}, previous: map[string]semantic.FileIdentities{}}
	coverage := map[ir.ExtractionStatus]int{}
	unknownExpressions, unknownTypes := map[string]int{}, map[string]int{}
	partial := []string{}
	limits := parser.DefaultLimits()
	err = filepath.WalkDir(checkout, func(full string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(checkout, full)
		rel = filepath.ToSlash(rel)
		if d.IsDir() {
			if d.Name() == ".git" || !set.SelectsSource(rel+"/x") && strings.Contains(rel, "node_modules") {
				return filepath.SkipDir
			}
			for _, pattern := range set.ExcludePatterns {
				if strings.HasSuffix(pattern, "/**") && bc.MatchSourcePattern(strings.TrimSuffix(pattern, "/**"), rel) {
					return filepath.SkipDir
				}
			}
			return nil
		}
		supported := false
		for _, ext := range registration.Descriptor.Extensions {
			if strings.HasSuffix(rel, ext) {
				supported = true
			}
		}
		if !supported || !set.SelectsSource(rel) {
			return nil
		}
		content, err := os.ReadFile(full)
		if err != nil {
			return err
		}
		if uint64(len(content)) > limits.MaxSourceBytes {
			return nil
		}
		sum := sha256.Sum256(content)
		src := ir.Source{FileID: ir.FileID("file:" + hex.EncodeToString(sum[:8]) + ":" + rel), RepositoryID: "repo", SnapshotID: commit, Path: rel, ContentSHA256: hex.EncodeToString(sum[:]), SizeBytes: uint64(len(content)), Language: "typescript", LanguageVersion: "5", ModuleID: string(set.ModuleID), SourceSetID: string(set.ID), BuildContextID: string(build.ID)}
		file, err := p.Parse(ctx, parser.Input{Source: src, Content: content, Limits: limits, Options: parser.Options{Settings: set.LanguageOptions}})
		if err != nil {
			t.Logf("parse %s: %v", rel, err)
			return nil
		}
		coverage[file.Coverage.Status]++
		for _, x := range file.Expressions {
			if x.Kind == ir.ExpressionUnknown {
				unknownExpressions[x.SyntaxKind]++
			}
		}
		for _, tr := range file.Types {
			if tr.Kind == ir.TypeUnknown {
				shape := tr.Spelling
				if len(shape) > 40 {
					shape = shape[:40]
				}
				unknownTypes[shape]++
			}
		}
		if file.Coverage.Status != ir.ExtractionComplete && len(partial) < 30 {
			issues := map[string]int{}
			for _, issue := range file.Coverage.Issues {
				issues[issue.Feature]++
			}
			partial = append(partial, fmt.Sprintf("%s %v", rel, issues))
		}
		w.files = append(w.files, semantic.SourceInput{Source: src, Lineage: graph.Lineage("repo", string(set.ModuleID), string(set.ID), rel), Affected: true})
		w.syntax[src.FileID] = file
		w.bytes[src.FileID] = content
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	resolver := tsresolve.New()
	if os.Getenv("CODEGRAPH_TEST_TS_COMPILER") != "" {
		resolver = tsresolve.NewWithCompiler(tsresolve.Compiler{})
	}
	result, err := resolver.Resolve(ctx, semantic.ResolveRequest{Run: deployment.RunKey{RepositoryID: "repo", RunID: "run"}, CommitSHA: commit, CheckoutPath: checkout, SyntaxLimits: limits, BuildLimits: bc.DefaultLimits()}, w)
	if err != nil {
		t.Fatal(err)
	}
	byKind := map[string]map[string]int{}
	reasons := map[string]int{}
	details := map[string]map[string]int{}
	provenance := map[string]int{}
	for _, lookups := range w.lookups {
		for _, l := range lookups {
			provenance[l.Provenance+"/"+string(l.Status)]++
			if byKind[string(l.Kind)] == nil {
				byKind[string(l.Kind)] = map[string]int{}
			}
			byKind[string(l.Kind)][string(l.Status)]++
			if l.Status != semantic.LookupResolved {
				reason, detail, _ := strings.Cut(l.Reason, ":")
				reasons[string(l.Cause)+"/"+reason]++
				if details[reason] == nil {
					details[reason] = map[string]int{}
				}
				detail = strings.TrimSpace(detail)
				if i := strings.Index(detail, " is not "); i > 0 && (reason == "symbol_not_found" || reason == "member_not_found") {
					detail = detail[:i]
				}
				if i := strings.Index(detail, " declares no module-level "); i > 0 {
					detail = detail[i+len(" declares no module-level "):]
				}
				if reason == "receiver_type_unknown" {
					detail = strings.TrimPrefix(detail, "the type of the receiver of ")
					if i := strings.Index(detail, " is not "); i > 0 {
						detail = detail[:i]
					}
				}
				details[reason][detail]++
			}
		}
	}
	top := func(title string, counts map[string]int, n int) {
		type kv struct {
			k string
			v int
		}
		var all []kv
		for k, v := range counts {
			all = append(all, kv{k, v})
		}
		sort.Slice(all, func(i, j int) bool { return all[i].v > all[j].v || all[i].v == all[j].v && all[i].k < all[j].k })
		if len(all) > n {
			all = all[:n]
		}
		fmt.Printf("== %s\n", title)
		for _, e := range all {
			fmt.Printf("%6d %s\n", e.v, e.k)
		}
	}
	top("unknown expression kinds", unknownExpressions, 15)
	top("unknown type shapes", unknownTypes, 15)
	for _, reason := range []string{"symbol_not_found", "member_not_found", "receiver_type_unknown", "export_not_found", "module_not_found", "analysis_limitation"} {
		top(reason, details[reason], 25)
	}
	fmt.Printf("files=%d coverage=%v\n", len(w.files), coverage)
	for _, line := range partial {
		fmt.Println("partial:", line)
	}
	fmt.Printf("result=%+v\n", result)
	fmt.Printf("provenance=%v\n", provenance)
	if os.Getenv("CODEGRAPH_TEST_TS_FILES") != "" {
		paths := map[ir.FileID]string{}
		for _, f := range w.files {
			paths[f.Source.FileID] = f.Source.Path
		}
		byFile := map[string]int{}
		for id, lookups := range w.lookups {
			for _, l := range lookups {
				if l.Status != semantic.LookupResolved {
					byFile[paths[id]]++
				}
			}
		}
		top("unresolved by file", byFile, 15)
		// Resolution of the checkout's own code, without hidden tool
		// directories (.agents, .claude, .github skills).
		var own, ownResolved int
		for id, lookups := range w.lookups {
			hidden := strings.HasPrefix(paths[id], ".") || strings.Contains(paths[id], "/.")
			for _, l := range lookups {
				if hidden {
					continue
				}
				own++
				if l.Status == semantic.LookupResolved {
					ownResolved++
				}
			}
		}
		fmt.Printf("outside hidden directories: %d/%d = %.1f%%\n", ownResolved, own, 100*float64(ownResolved)/float64(max(own, 1)))
	}
	if only := os.Getenv("CODEGRAPH_TEST_TS_UNRESOLVED_IN"); only != "" {
		for _, f := range w.files {
			if f.Source.Path != only {
				continue
			}
			raw := w.bytes[f.Source.FileID]
			shown := 0
			for _, l := range w.lookups[f.Source.FileID] {
				if l.Status == semantic.LookupResolved || shown >= 30 {
					continue
				}
				shown++
				span := l.Evidence.Span
				fmt.Printf("unresolved %d %q %s\n", span.Start.Line+1, string(raw[span.Start.ByteOffset:span.End.ByteOffset]), l.Reason)
			}
		}
	}
	if os.Getenv("CODEGRAPH_TEST_TS_SAMPLES") != "" {
		paths := map[ir.FileID]string{}
		for _, f := range w.files {
			paths[f.Source.FileID] = f.Source.Path
		}
		shown := 0
		for id, lookups := range w.lookups {
			for _, l := range lookups {
				if l.Status != semantic.LookupAmbiguous || shown >= 25 {
					continue
				}
				shown++
				var names []string
				for _, c := range l.CandidateIDs {
					sym := w.symbols[c]
					name := sym.Name
					if sym.Key != nil {
						name = sym.Key.CanonicalSignature
					}
					names = append(names, name)
				}
				fmt.Printf("ambiguous %s:%d %v\n", paths[id], l.Evidence.Span.Start.Line+1, names)
			}
		}
	}
	kinds := make([]string, 0, len(byKind))
	for k := range byKind {
		kinds = append(kinds, k)
	}
	sort.Strings(kinds)
	for _, k := range kinds {
		fmt.Printf("kind %-12s %v\n", k, byKind[k])
	}
	type rc struct {
		reason string
		n      int
	}
	var sorted []rc
	for r, n := range reasons {
		sorted = append(sorted, rc{r, n})
	}
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].n > sorted[j].n })
	for _, r := range sorted {
		fmt.Printf("unresolved %6d %s\n", r.n, r.reason)
	}
}
