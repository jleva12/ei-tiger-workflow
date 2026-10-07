// Package python binds Python with the pinned Pyright analyzer. Go owns source
// identities and graph facts; no Python name/type heuristics run in the worker.
package python

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"time"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/languages/python/environment"
	pyparser "ei-aitiger-codegraph/worker/internal/parser/python"
	"ei-aitiger-codegraph/worker/internal/scratch"
)

const Version = "python-pyright-binding-v4"

type Config struct {
	NodePath, AnalyzerPath string
	MaxHeapMiB             int
	Timeout                time.Duration
}
type Resolver struct{ config Config }

// DefaultMaxHeapMiB is the analyzer heap when the configuration sets none.
// Pyright holds the parse trees, bindings and checker results of one whole
// program at once and only sheds its type cache under pressure, so the heap
// scales with the context: 1,932 files of google/adk-python peak near 3 GiB
// and die at 1 GiB, 733 files fit in 2 GiB.
const DefaultMaxHeapMiB = 4096

// ErrAnalyzerHeap reports that the analyzer process exhausted the heap it was
// given. The same context fails the same way on every attempt until
// max_heap_mib is raised or the context is narrowed.
var ErrAnalyzerHeap = errors.New("Python analyzer ran out of heap")

func New(c Config) *Resolver {
	scratch.Sweep(os.TempDir(), "codegraph-python-", scratch.Stale) // analyses a killed process left
	if c.NodePath == "" {
		c.NodePath = "node"
	}
	if c.MaxHeapMiB == 0 {
		c.MaxHeapMiB = DefaultMaxHeapMiB
	}
	if c.Timeout == 0 {
		c.Timeout = 5 * time.Minute
	}
	return &Resolver{config: c}
}
func (*Resolver) Version() string { return Version }
func (r *Resolver) PolicyDigest() string {
	return graph.Digest(Version + ":pyright-1.1.414:static-contracts:unknown-alternatives:no-python-execution:errors-explain-misses:one-target-per-definition:uninstalled-packages-named")
}

type site struct {
	ID     string `json:"id"`
	Kind   string `json:"kind"`
	Start  uint64 `json:"start"`
	End    uint64 `json:"end"`
	occ    ir.Occurrence
	decl   ir.DeclarationID
	lookup semantic.LookupKind
}
type fileRequest struct {
	ID           ir.FileID `json:"id"`
	Path         string    `json:"path"`
	SHA256       string    `json:"sha256"`
	Sites        []site    `json:"sites"`
	in           semantic.SourceInput
	declarations []ir.Declaration
	bySite       map[string]site
}
type request struct {
	Protocol     int               `json:"protocol"`
	Context      string            `json:"context"`
	Root         string            `json:"root"`
	Version      string            `json:"version"`
	Platform     string            `json:"platform"`
	Roots        []string          `json:"roots"`
	Environments []execEnvironment `json:"environments"`
	Dependencies []string          `json:"dependencies"`
	Files        []*fileRequest    `json:"files"`
}

// execEnvironment is a Pyright execution environment: the files under Root
// resolve imports from it, then from ExtraPaths in order.
type execEnvironment struct {
	Root       string   `json:"root"`
	ExtraPaths []string `json:"extraPaths"`
}
type target struct {
	// External names what an uninstalled package provides, by its dotted
	// path from the module (fastapi.FastAPI().get); it has no declaration.
	External     bool               `json:"external,omitempty"`
	DerivedOwner *target            `json:"derivedOwner,omitempty"`
	Path         string             `json:"path"`
	Start        uint64             `json:"start"`
	End          uint64             `json:"end"`
	Name         string             `json:"name"`
	Kind         ir.DeclarationKind `json:"kind"`
	Qualified    string             `json:"qualified"`
	SHA256       string             `json:"sha256"`
	Role         string             `json:"role"`
}
type event struct {
	Event    string                `json:"event"`
	Protocol int                   `json:"protocol"`
	Pyright  string                `json:"pyright"`
	Context  string                `json:"context"`
	File     ir.FileID             `json:"file"`
	Site     string                `json:"site"`
	Status   semantic.LookupStatus `json:"status"`
	Targets  []target              `json:"targets"`
	Unknown  bool                  `json:"unknown"`
	Reason   string                `json:"reason"`
	Code     string                `json:"code"`
	Cause    semantic.LookupCause  `json:"cause"`
}

func (r *Resolver) Resolve(ctx context.Context, req semantic.ResolveRequest, w semantic.Workspace) (semantic.ResolutionResult, error) {
	var result semantic.ResolutionResult
	if w == nil {
		return result, semantic.ErrInvalid
	}
	build := w.Build()
	if build.RepositoryID != req.Run.RepositoryID || build.SnapshotID != req.CommitSHA {
		return result, fmt.Errorf("%w: Python build identity mismatch", semantic.ErrInvalid)
	}
	if err := req.SyntaxLimits.Validate(); err != nil {
		return result, err
	}
	if err := req.BuildLimits.Validate(); err != nil {
		return result, err
	}
	sets := map[bc.SourceSetID]bc.SourceSet{}
	for _, s := range build.Inventory.SourceSets {
		if s.Language == "python" {
			sets[s.ID] = s
		}
	}
	selected := append([]bc.SourceSetID{}, req.Contexts...)
	if len(selected) == 0 {
		for id := range sets {
			selected = append(selected, id)
		}
	}
	sort.Slice(selected, func(i, j int) bool { return selected[i] < selected[j] })
	files, err := w.Files(ctx)
	if err != nil {
		return result, err
	}
	engine, err := pyparser.New()
	if err != nil {
		return result, err
	}
	defer engine.Close(context.Background())
	for _, id := range selected {
		set, ok := sets[id]
		if !ok {
			return result, fmt.Errorf("%w: not a Python context: %s", semantic.ErrInvalid, id)
		}
		var inputs []semantic.SourceInput
		for _, f := range files {
			if f.Source.SourceSetID == string(id) {
				inputs = append(inputs, f)
			}
		}
		if len(inputs) == 0 {
			continue
		}
		if err := r.resolveContext(ctx, req, w, engine, set, inputs, &result); err != nil {
			return result, fmt.Errorf("Python context %s: %w", id, err)
		}
	}
	return result, nil
}

func symbolID(file ir.FileID, decl ir.DeclarationID) string {
	return graph.ID("python-symbol", string(file), string(decl))
}
func anchor(in semantic.SourceInput, s ir.Span) graph.SourceAnchor {
	return graph.SourceAnchor{Lineage: in.Lineage, ContentSHA256: in.Source.ContentSHA256, Span: s}
}
func sourceSymbol(f *fileRequest, d ir.Declaration) semantic.Symbol {
	s := semantic.Symbol{ID: symbolID(f.ID, d.ID), Name: d.Name, Source: &semantic.SourceSymbol{FileID: f.ID, DeclarationID: d.ID, Evidence: anchor(f.in, d.Span)}}
	if d.OwnerID != "" {
		s.OwnerSymbolID = symbolID(f.ID, d.OwnerID)
	}
	return s
}

// externalName is the symbol of what an uninstalled package provides, named
// by its dotted path: it belongs to the artifact of the package's top-level
// module and pins no version, as nothing installed says which.
func externalName(qualified, name string) semantic.Symbol {
	top, _, _ := strings.Cut(qualified, ".")
	owner := "python"
	if i := strings.LastIndex(qualified, "."); i >= 0 {
		owner = qualified[:i]
	}
	artifact := "python:" + top
	k := &semantic.DeclarationKey{OwnerKey: owner, Kind: ir.DeclarationVariable, Name: name, CanonicalSignature: qualified}
	return semantic.Symbol{ID: graph.ID("python-external-name", artifact, qualified), Name: name, Key: k, External: &semantic.ExternalSymbol{ArtifactID: artifact, ArtifactFingerprint: "unversioned"}}
}

// environments lays out import resolution for a checkout of several
// projects. Files outside every project resolve from the checkout's roots,
// then the shared projects. A project's files resolve from its directory
// and package directories first, so its own app or tests package wins over
// a sibling's; the innermost project holding a file is the one it runs in.
func environments(scratch string, settings environment.Settings) ([]string, []execEnvironment) {
	abs := func(dirs ...string) []string {
		out := make([]string, 0, len(dirs))
		for _, d := range dirs {
			out = append(out, filepath.Join(scratch, filepath.FromSlash(d)))
		}
		return out
	}
	roots := abs(settings.Roots...)
	var shared []string
	for _, p := range settings.Projects {
		if p.Shared {
			shared = append(shared, abs(append(append([]string{}, p.Paths...), p.Root)...)...)
		}
	}
	projects := append([]environment.Project{}, settings.Projects...)
	sort.SliceStable(projects, func(i, j int) bool {
		return strings.Count(projects[i].Root, "/") > strings.Count(projects[j].Root, "/")
	})
	envs := make([]execEnvironment, 0, len(projects))
	for _, p := range projects {
		own := abs(append(append([]string{}, p.Paths...), p.Root)...)
		extra := append(abs(p.Paths...), roots...)
		for _, dir := range shared {
			if !slices.Contains(own, dir) {
				extra = append(extra, dir)
			}
		}
		envs = append(envs, execEnvironment{Root: filepath.Join(scratch, filepath.FromSlash(p.Root)), ExtraPaths: extra})
	}
	return append(roots, shared...), envs
}

func key(t target) *semantic.DeclarationKey {
	owner, _, _ := strings.Cut(t.Qualified, "#")
	if i := strings.LastIndex(t.Qualified, "."); i >= 0 {
		owner = t.Qualified[:i]
	} else {
		owner = "python"
	}
	return &semantic.DeclarationKey{OwnerKey: owner, Kind: t.Kind, Name: t.Name, CanonicalSignature: t.Qualified}
}

func (r *Resolver) resolveContext(parent context.Context, req semantic.ResolveRequest, w semantic.Workspace, engine *pyparser.Parser, set bc.SourceSet, inputs []semantic.SourceInput, result *semantic.ResolutionResult) error {
	ctx, cancel := context.WithTimeout(parent, r.config.Timeout)
	defer cancel()
	settings, err := environment.Decode(set.LanguageOptions, set.LanguageVersion)
	if err != nil {
		return err
	}
	bridge := environment.AnalyzerPath(r.config.AnalyzerPath)
	bridge, err = filepath.Abs(bridge)
	if err != nil {
		return err
	}
	if _, err := os.Stat(bridge); err != nil {
		return fmt.Errorf("Python analyzer missing; run npm ci && npm run build in apps/forge-codegraph-worker/python-analyzer, or set CODEGRAPH_PYTHON_ANALYZER: %w", err)
	}
	budget := environment.TreeBudget{Bytes: req.BuildLimits.MaxHashBytes, Files: req.BuildLimits.MaxFiles, Depth: req.BuildLimits.MaxDepth}
	bridgeDigest, err := environment.DigestTreeWithBudget(ctx, filepath.Dir(bridge), &budget)
	if err != nil {
		return err
	}
	if settings.AnalyzerDigest != "" && settings.AnalyzerDigest != bridgeDigest {
		return fmt.Errorf("%w: Python analyzer fingerprint changed", semantic.ErrIntegrity)
	}
	for _, dep := range settings.Dependencies {
		digest, err := environment.DigestTreeWithBudget(ctx, dep.Path, &budget)
		if err != nil {
			return err
		}
		if digest != dep.SHA256 {
			return fmt.Errorf("%w: Python dependency fingerprint changed: %s", semantic.ErrIntegrity, dep.Path)
		}
	}
	scratch, err := os.MkdirTemp("", "codegraph-python-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(scratch)
	rq := request{Protocol: 1, Context: string(set.ID), Root: scratch, Version: settings.Version, Platform: settings.Platform, Dependencies: []string{}}
	rq.Roots, rq.Environments = environments(scratch, settings)
	for _, dep := range settings.Dependencies {
		rq.Dependencies = append(rq.Dependencies, dep.Path)
	}
	byPath := map[string]*fileRequest{}
	byID := map[ir.FileID]*fileRequest{}
	for _, in := range inputs {
		if err := ctx.Err(); err != nil {
			return err
		}
		content, err := w.SourceBytes(ctx, in)
		if err != nil {
			return err
		}
		file, err := w.Syntax(ctx, in)
		if errors.Is(err, semantic.ErrNotFound) {
			file, err = engine.Parse(ctx, parser.Input{Source: in.Source, Content: content, Options: parser.Options{Settings: set.LanguageOptions}, Limits: req.SyntaxLimits})
		}
		if err != nil && ctx.Err() == nil && !errors.Is(err, semantic.ErrIntegrity) {
			// A file the parser cannot handle (beyond its limits, not
			// UTF-8, invalid syntax it rejects) is left out.
			if in.Affected {
				result.Skipped++
			}
			continue
		}
		if err != nil {
			return err
		}
		if file.Source != in.Source {
			return fmt.Errorf("%w: Python syntax identity mismatch", semantic.ErrIntegrity)
		}
		sum := sha256.Sum256(content)
		if hex.EncodeToString(sum[:]) != in.Source.ContentSHA256 {
			return semantic.ErrIntegrity
		}
		p := filepath.Join(scratch, filepath.FromSlash(in.Source.Path))
		if !within(scratch, p) {
			return semantic.ErrInvalid
		}
		if err := os.MkdirAll(filepath.Dir(p), 0700); err != nil {
			return err
		}
		if err := os.WriteFile(p, content, 0600); err != nil {
			return err
		}
		f := &fileRequest{ID: in.Source.FileID, Path: p, SHA256: in.Source.ContentSHA256, in: in, declarations: file.Declarations, bySite: map[string]site{}}
		add := func(s site) { f.Sites = append(f.Sites, s); f.bySite[s.ID] = s }
		for _, d := range file.Declarations {
			if d.NameSpan != nil {
				add(site{ID: "d:" + string(d.ID), Kind: "declaration", Start: d.NameSpan.Start.ByteOffset, End: d.NameSpan.End.ByteOffset, decl: d.ID})
				if in.Affected && d.Kind == ir.DeclarationMethod {
					add(site{ID: "o:" + string(d.ID), Kind: "override", Start: d.NameSpan.Start.ByteOffset, End: d.NameSpan.End.ByteOffset, decl: d.ID, lookup: semantic.LookupOverride, occ: ir.Occurrence{Span: d.Span}})
				}
			}
		}
		if in.Affected {
			for _, c := range file.Calls {
				add(site{ID: "c:" + string(c.Occurrence.ID), Kind: "call", Start: c.Occurrence.Span.Start.ByteOffset, End: c.Occurrence.Span.End.ByteOffset, occ: c.Occurrence, lookup: semantic.LookupCall})
			}
			for _, ref := range file.References {
				add(site{ID: "r:" + string(ref.Occurrence.ID), Kind: "reference", Start: ref.Occurrence.Span.Start.ByteOffset, End: ref.Occurrence.Span.End.ByteOffset, occ: ref.Occurrence, lookup: semantic.LookupMember})
			}
			for _, u := range file.TypeUses {
				kind := semantic.LookupType
				if u.Role == ir.TypeUseHeritage {
					kind = semantic.LookupInheritance
				}
				add(site{ID: "t:" + string(u.Occurrence.ID), Kind: "type", Start: u.Occurrence.Span.Start.ByteOffset, End: u.Occurrence.Span.End.ByteOffset, occ: u.Occurrence, lookup: kind})
			}
			for _, im := range file.Imports {
				add(site{ID: "i:" + string(im.Occurrence.ID), Kind: "import", Start: im.Occurrence.Span.Start.ByteOffset, End: im.Occurrence.Span.End.ByteOffset, occ: im.Occurrence, lookup: semantic.LookupMember})
			}
		}
		rq.Files = append(rq.Files, f)
		byPath[p] = f
		byID[f.ID] = f
	}
	if len(rq.Files) == 0 {
		return nil
	}
	sort.Slice(rq.Files, func(i, j int) bool { return rq.Files[i].Path < rq.Files[j].Path })
	symbols := map[string]semantic.Symbol{}
	for _, f := range rq.Files {
		for _, d := range f.declarations {
			s := sourceSymbol(f, d)
			symbols[s.ID] = s
		}
	}
	var resolveTarget func(target) (string, bool, error)
	resolveTarget = func(t target) (string, bool, error) {
		if t.External {
			if t.Qualified == "" || t.Name == "" {
				return "", false, nil
			}
			sym := externalName(t.Qualified, t.Name)
			symbols[sym.ID] = sym
			return sym.ID, true, nil
		}
		if t.DerivedOwner != nil {
			owner, mapped, err := resolveTarget(*t.DerivedOwner)
			if err != nil {
				return "", false, err
			}
			if mapped && symbols[owner].Source != nil {
				id := graph.ID("python-derived", owner, t.Name, t.Role)
				symbols[id] = semantic.Symbol{ID: id, Name: t.Name, Key: key(t), OwnerSymbolID: owner, Derived: &semantic.DerivedSymbol{Language: "python", Rule: t.Role, SourceSymbolIDs: []string{owner}, DefinitionDigest: graph.Digest(Version + ":" + t.Role)}}
				return id, true, nil
			}
		}
		if f := byPath[t.Path]; f != nil {
			if t.SHA256 != f.SHA256 {
				return "", false, semantic.ErrIntegrity
			}
			if t.Role == "module" {
				id := graph.ID("python-module", string(f.ID))
				symbols[id] = semantic.Symbol{ID: id, Name: t.Name, Key: key(t), Module: &semantic.ModuleSymbol{FileID: f.ID}}
				return id, true, nil
			}
			for _, d := range f.declarations {
				if d.NameSpan != nil && d.Name == t.Name && d.NameSpan.Start.ByteOffset == t.Start && d.NameSpan.End.ByteOffset == t.End {
					s := symbols[symbolID(f.ID, d.ID)]
					if t.Qualified != "" && d.Kind != ir.DeclarationLocal && d.Kind != ir.DeclarationParameter && d.Kind != ir.DeclarationTypeParameter {
						s.Key = key(t)
						s.Key.Kind = d.Kind
					}
					symbols[s.ID] = s
					return s.ID, true, nil
				}
			}
			return "", false, nil
		}
		artifact, digest, artifactRoot := "", "", ""
		if root := filepath.Join(filepath.Dir(bridge), "typeshed-fallback"); within(root, t.Path) {
			artifact = "python-typeshed:pyright-1.1.414"
			digest = bridgeDigest
			artifactRoot = root
		}
		for i, dep := range settings.Dependencies {
			if within(dep.Path, t.Path) {
				artifact = fmt.Sprintf("python-dependencies:%d", i)
				digest = dep.SHA256
				artifactRoot = dep.Path
				break
			}
		}
		if artifact == "" || t.Name == "" || t.Qualified == "" {
			return "", false, nil
		}
		// An immutable artifact may contain several overload declarations with
		// the same qualified name. Use its relative declaration location in
		// both identities, so projection cannot collapse distinct overloads.
		rel, err := filepath.Rel(artifactRoot, t.Path)
		if err != nil {
			return "", false, err
		}
		k := key(t)
		k.CanonicalSignature += fmt.Sprintf("@%s:%d:%d", filepath.ToSlash(rel), t.Start, t.End)
		id := graph.ID("python-external", artifact, digest, k.CanonicalSignature, string(t.Kind))
		symbols[id] = semantic.Symbol{ID: id, Name: t.Name, Key: k, External: &semantic.ExternalSymbol{ArtifactID: artifact, ArtifactFingerprint: digest}}
		return id, true, nil
	}
	lookups := map[ir.FileID][]semantic.Lookup{}
	seen := map[string]bool{}
	completed := map[ir.FileID]bool{}
	hello, done := false, false
	err = r.runBridge(ctx, bridge, rq, func(e event) error {
		switch e.Event {
		case "hello":
			if hello || e.Protocol != 1 || e.Pyright != "1.1.414" || e.Context != rq.Context {
				return semantic.ErrIntegrity
			}
			hello = true
		case "lookup":
			if !hello || done {
				return semantic.ErrIntegrity
			}
			f := byID[e.File]
			if f == nil || completed[e.File] {
				return semantic.ErrIntegrity
			}
			s, ok := f.bySite[e.Site]
			if !ok {
				return semantic.ErrIntegrity
			}
			seenKey := string(e.File) + "\x00" + e.Site
			if seen[seenKey] {
				return semantic.ErrIntegrity
			}
			seen[seenKey] = true
			var ids []string
			unknown := e.Unknown
			for _, t := range e.Targets {
				id, mapped, err := resolveTarget(t)
				if err != nil {
					return err
				}
				if mapped {
					ids = append(ids, id)
				} else {
					unknown = true
				}
			}
			sort.Strings(ids)
			ids = unique(ids)
			if s.Kind == "declaration" {
				return nil
			}
			if s.Kind == "override" {
				for _, id := range ids {
					lookups[f.ID] = append(lookups[f.ID], semantic.Lookup{ID: graph.ID("python-override", string(f.ID), s.ID, id), FileID: f.ID, DeclarationID: s.decl, Kind: semantic.LookupOverride, Status: semantic.LookupResolved, SelectedSymbolID: id, Provenance: "type_analyzer", Evidence: anchor(f.in, s.occ.Span)})
				}
				return nil
			}
			l := semantic.Lookup{ID: graph.ID("python-lookup", string(f.ID), s.ID), FileID: f.ID, OccurrenceID: s.occ.ID, Kind: s.lookup, Status: e.Status, Reason: e.Reason, Cause: e.Cause, DiagnosticCode: e.Code, Provenance: "type_analyzer", Evidence: anchor(f.in, s.occ.Span), CandidateIDs: ids, CandidateRole: "binding_alternative", HasUnknownCandidates: unknown}
			if e.Status == semantic.LookupResolved && len(ids) == 1 && !unknown {
				l.SelectedSymbolID = ids[0]
				l.Cause = ""
				l.Reason = ""
				l.CandidateRole = ""
				l.CandidateIDs = nil
			} else {
				if len(ids) > 1 {
					l.Status = semantic.LookupAmbiguous
					l.Cause = semantic.CauseAmbiguousBinding
				} else {
					l.Status = semantic.LookupUnresolved
				}
				if unknown && e.Code == "" {
					// A diagnostic explains the miss better than the
					// analyzer's incomplete type.
					l.Reason = "target_mapping_or_type_incomplete"
					l.Cause = semantic.CauseAnalysisLimitation
				}
			}
			lookups[f.ID] = append(lookups[f.ID], l)
		case "file_done":
			if byID[e.File] == nil || completed[e.File] {
				return semantic.ErrIntegrity
			}
			completed[e.File] = true
		case "done":
			if !hello || done || e.Context != rq.Context {
				return semantic.ErrIntegrity
			}
			done = true
		default:
			return fmt.Errorf("unknown Python analyzer event %q", e.Event)
		}
		return nil
	})
	analysisErr := err
	if analysisErr == nil && !done {
		analysisErr = errors.New("the analyzer did not complete")
	}
	for _, f := range rq.Files {
		if analysisErr != nil {
			break
		}
		if !completed[f.ID] {
			analysisErr = fmt.Errorf("the analyzer did not finish %s", f.in.Source.Path)
			break
		}
		for _, s := range f.Sites {
			if !seen[string(f.ID)+"\x00"+s.ID] {
				analysisErr = fmt.Errorf("the analyzer omitted a site of %s", f.in.Source.Path)
				break
			}
		}
	}
	if analysisErr != nil {
		if err := parent.Err(); err != nil {
			return err
		}
		// The analyzer failed on this context (its time or heap budget, a
		// crash, a protocol error): its declarations are still symbols
		// other contexts can bind to, and its references stay unresolved.
		slog.WarnContext(parent, "the Python analyzer failed on a context; its references stay unresolved", "context", set.ID, "files", len(rq.Files), "error", analysisErr)
		result.Warnings = append(result.Warnings, fmt.Sprintf("The Python analyzer could not analyse %s (%v), so its references are unresolved.", set.Name, analysisErr))
		result.Degraded = append(result.Degraded, string(set.ID))
		for id, s := range symbols {
			if s.Source == nil {
				delete(symbols, id)
			}
		}
		lookups = map[ir.FileID][]semantic.Lookup{}
		for _, f := range rq.Files {
			if f.in.Affected {
				result.Skipped++
			}
		}
	}
	// Detect analysis input edits before retaining any facts. A second pass
	// has the same bounded budget as the initial fingerprint pass.
	budget = environment.TreeBudget{Bytes: req.BuildLimits.MaxHashBytes, Files: req.BuildLimits.MaxFiles, Depth: req.BuildLimits.MaxDepth}
	if after, err := environment.DigestTreeWithBudget(ctx, filepath.Dir(bridge), &budget); err != nil {
		return err
	} else if after != bridgeDigest {
		return semantic.ErrIntegrity
	}
	for _, dep := range settings.Dependencies {
		digest, err := environment.DigestTreeWithBudget(ctx, dep.Path, &budget)
		if err != nil {
			return err
		}
		if digest != dep.SHA256 {
			return semantic.ErrIntegrity
		}
	}
	var batch []semantic.Symbol
	var symbolIDs []string
	for id := range symbols {
		symbolIDs = append(symbolIDs, id)
	}
	sort.Strings(symbolIDs)
	for _, id := range symbolIDs {
		batch = append(batch, symbols[id])
		if len(batch) == 1000 {
			if err := w.PutSymbols(ctx, batch); err != nil {
				return err
			}
			batch = nil
		}
	}
	if len(batch) > 0 {
		if err := w.PutSymbols(ctx, batch); err != nil {
			return err
		}
	}
	result.Symbols += uint64(len(symbols))
	for _, f := range rq.Files {
		if !f.in.Affected {
			continue
		}
		ls := lookups[f.ID]
		if err := w.PutLookups(ctx, f.ID, ls); err != nil {
			return err
		}
		for _, l := range ls {
			switch l.Status {
			case semantic.LookupResolved:
				result.Resolved++
			case semantic.LookupAmbiguous:
				result.Ambiguous++
			case semantic.LookupUnsupported:
				result.Unsupported++
			default:
				result.Unresolved++
			}
		}
	}
	return nil
}
func unique(values []string) []string {
	var out []string
	for _, s := range values {
		if len(out) == 0 || out[len(out)-1] != s {
			out = append(out, s)
		}
	}
	return out
}
func within(root, p string) bool {
	rel, err := filepath.Rel(root, p)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && !filepath.IsAbs(rel)
}

type cappedBuffer struct{ bytes.Buffer }

func (b *cappedBuffer) Write(p []byte) (int, error) {
	n := len(p)
	if b.Len() < 64<<10 {
		keep := min(n, (64<<10)-b.Len())
		_, _ = b.Buffer.Write(p[:keep])
	}
	return n, nil
}
func (r *Resolver) runBridge(ctx context.Context, bridge string, rq request, accept func(event) error) error {
	data, err := json.Marshal(rq)
	if err != nil {
		return err
	}
	cmd := exec.CommandContext(ctx, r.config.NodePath, fmt.Sprintf("--max-old-space-size=%d", r.config.MaxHeapMiB), bridge)
	cmd.Stdin = bytes.NewReader(data)
	cmd.Dir = rq.Root
	cmd.WaitDelay = 3 * time.Second
	// Prevent inherited Node startup hooks from changing analyzer semantics.
	for _, item := range os.Environ() {
		if !strings.HasPrefix(item, "NODE_OPTIONS=") && !strings.HasPrefix(item, "NODE_PATH=") {
			cmd.Env = append(cmd.Env, item)
		}
	}
	stderr := &cappedBuffer{}
	cmd.Stderr = stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	if err := cmd.Start(); err != nil {
		return err
	}
	output := &io.LimitedReader{R: stdout, N: 512 << 20}
	scanner := bufio.NewScanner(output)
	scanner.Buffer(make([]byte, 64<<10), 4<<20)
	var streamErr error
	for scanner.Scan() {
		var e event
		if err := json.Unmarshal(scanner.Bytes(), &e); err != nil {
			streamErr = err
			break
		}
		if err := accept(e); err != nil {
			streamErr = err
			break
		}
	}
	if streamErr == nil {
		streamErr = scanner.Err()
	}
	if streamErr == nil && output.N == 0 {
		streamErr = fmt.Errorf("Python analyzer output exceeds 512 MiB")
	}
	if streamErr != nil {
		_ = cmd.Process.Kill()
	}
	err = cmd.Wait()
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if streamErr != nil {
		return fmt.Errorf("Python analyzer protocol: %w", streamErr)
	}
	if err != nil {
		if heapExhausted(stderr.String()) {
			return fmt.Errorf("%w: max_heap_mib=%d is too small for the %d files of context %s; raise python.max_heap_mib or narrow the context with python.excludes", ErrAnalyzerHeap, r.config.MaxHeapMiB, len(rq.Files), rq.Context)
		}
		return fmt.Errorf("Python analyzer: %w: %s", err, stderr.String())
	}
	return nil
}

// heapExhausted recognizes V8's fatal heap-limit report on stderr.
func heapExhausted(stderr string) bool {
	return strings.Contains(stderr, "JavaScript heap out of memory") || strings.Contains(stderr, "Reached heap limit")
}
