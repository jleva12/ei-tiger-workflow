package python

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/discovery"
	"ei-aitiger-codegraph/worker/internal/graphanalysis"
	"ei-aitiger-codegraph/worker/internal/languages/python/project"
	pyparser "ei-aitiger-codegraph/worker/internal/parser/python"
)

// TestResolveCheckout exercises discovery, parsing, analyzer binding, identity
// matching and graph projection on a real checkout without importing its code.
func TestResolveCheckout(t *testing.T) {
	checkout := os.Getenv("CODEGRAPH_TEST_PYTHON_CHECKOUT")
	if checkout == "" {
		t.Skip("set CODEGRAPH_TEST_PYTHON_CHECKOUT")
	}
	requireAnalyzer(t)
	ctx := context.Background()
	started := time.Now()
	cfg := project.Config{Version: "3.12", Platform: "Linux"}
	if include := os.Getenv("CODEGRAPH_TEST_PYTHON_INCLUDE"); include != "" {
		cfg.Includes = []string{include}
	}
	if deps := os.Getenv("CODEGRAPH_TEST_PYTHON_DEPENDENCIES"); deps != "" {
		cfg.Dependencies = filepath.SplitList(deps)
	}
	co := bc.Checkout{Path: checkout, RepositoryID: "python-checkout", SnapshotID: strings.Repeat("a", 40)}
	build, err := (project.Provider{Config: cfg}).Build(ctx, bc.Request{Checkout: co, Limits: bc.DefaultLimits()})
	must(t, err)
	w := newFakeWorkspace(build)
	engine, err := pyparser.New()
	must(t, err)
	defer engine.Close(ctx)
	registry, err := parser.NewRegistry(pyparser.Registration())
	must(t, err)
	coverage := map[string]int{}
	issues := map[string]int{}
	skipped := 0
	report, err := discovery.WalkWithClassifier(ctx, co, build, discovery.DefaultLimits(), registry, func(src ir.Source) error {
		content, err := os.ReadFile(filepath.Join(checkout, filepath.FromSlash(src.Path)))
		if err != nil {
			return err
		}
		f, err := engine.Parse(ctx, parser.Input{Source: src, Content: content, Limits: parser.DefaultLimits(), Options: parser.Options{Settings: build.Inventory.SourceSets[0].LanguageOptions}})
		if errors.Is(err, parser.ErrLimitExceeded) {
			skipped++
			t.Logf("skipped %s: %v", src.Path, err)
			return nil
		}
		if err != nil {
			return err
		}
		coverage[string(f.Coverage.Status)]++
		for _, issue := range f.Coverage.Issues {
			issues[issue.Message]++
		}
		in := semantic.SourceInput{Source: src, Lineage: graph.Lineage(co.RepositoryID, src.ModuleID, src.SourceSetID, src.Path), Affected: true}
		w.files = append(w.files, in)
		w.syntax[src.FileID] = f
		w.bytes[src.FileID] = content
		return nil
	})
	must(t, err)
	parsed := time.Now()
	t.Logf("parse coverage: %v; syntax issues: %v", coverage, issues)
	req := semantic.ResolveRequest{Run: deployment.RunKey{RepositoryID: co.RepositoryID, RunID: "checkout"}, CommitSHA: co.SnapshotID, CheckoutPath: checkout, SyntaxLimits: parser.DefaultLimits(), BuildLimits: bc.DefaultLimits()}
	result, err := New(Config{MaxHeapMiB: 2048, Timeout: 10 * time.Minute}).Resolve(ctx, req, w)
	must(t, err)
	resolved := time.Now()
	_, err = (graphanalysis.Matcher{}).Match(ctx, semantic.MatchRequest{Run: req.Run, Files: w.files}, w)
	must(t, err)
	edges := map[string]int{}
	var nodes int
	projection, err := (graphanalysis.Projector{}).Project(ctx, semantic.ProjectRequest{Run: req.Run, Files: w.files, SyntaxLimits: req.SyntaxLimits}, w, func(_ context.Context, f graph.Fact) error {
		if err := f.Validate(); err != nil {
			return err
		}
		if f.Node != nil {
			nodes++
		} else {
			edges[f.Edge.Kind]++
		}
		return nil
	})
	must(t, err)
	causes := map[string]int{}
	for _, ls := range w.lookups {
		for _, l := range ls {
			if l.Status != semantic.LookupResolved {
				causes[string(l.Status)+":"+string(l.Cause)+":"+l.Reason]++
			}
		}
	}
	stats := map[string]any{"files": report.Files, "skipped": skipped, "coverage": coverage, "syntax_issues": issues, "resolution": result, "projection": projection, "edge_kinds": edges, "parse_seconds": parsed.Sub(started).Seconds(), "resolve_seconds": resolved.Sub(parsed).Seconds(), "total_seconds": time.Since(started).Seconds()}
	data, _ := json.MarshalIndent(stats, "", "  ")
	t.Log(string(data))
	type count struct {
		Reason string
		Count  int
	}
	var counts []count
	for reason, n := range causes {
		counts = append(counts, count{reason, n})
	}
	sort.Slice(counts, func(i, j int) bool { return counts[i].Count > counts[j].Count })
	for _, c := range counts[:min(12, len(counts))] {
		t.Logf("%d %s", c.Count, c.Reason)
	}
	if nodes == 0 || edges[graph.EdgeCalls] == 0 {
		t.Fatal("checkout produced no callable graph")
	}
}
