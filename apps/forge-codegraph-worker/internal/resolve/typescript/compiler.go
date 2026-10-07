package typescript

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
	"io/fs"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
)

// The compiler tier. After the syntax tier has bound a context's sites, the
// TypeScript compiler, run through the bridge in typescript-analyzer,
// analyses the context as the projects it belongs to (each file with the
// options of the tsconfig that governs it, packages from node_modules when
// they are installed) and binds the same sites with the type checker. A
// site the compiler binds takes the compiler's targets and compiler
// provenance; a site it cannot bind (an any-typed receiver, a keyword type)
// keeps its syntax-tier binding. A compiler that fails part way leaves the
// sites it had not reached to the syntax tier, and the context is resolved
// again on the next run.

// DefaultCompilerHeapMiB is the heap the compiler runs with by default: the
// checker keeps a whole program, with the declarations of its packages, in
// memory.
const DefaultCompilerHeapMiB = 4096

// CompilerVersion changes whenever the compiler tier binds differently: the
// bridge's rules, its pinned TypeScript, or how targets become symbols.
const CompilerVersion = "typescript-compiler-binding-v2/typescript-6.0.3"

// CompilerPolicy summarizes what compiler-tier bindings mean.
const CompilerPolicy = "Compiler-tier binding with TypeScript 6.0.3 over each file's governing tsconfig or jsconfig (the nearest above it that includes it, or a project it references) and installed packages: calls bind to the declaration of the signature the checker resolves (an overload's implementation in the checkout), references and decorators to the declaration of the symbol behind the name with imports followed, types to the type declaration, overrides to the base class's member, and a member of a union to the member a common base declares or else to each constituent's member as binding alternatives; declarations in the checkout bind to their source symbols, a member of an object or type literal to a member derived from the innermost enclosing declaration with a portable key and named by the path below it, those of packages to external symbols named by package and qualified name with a module's export = entity omitted (react#useState), those of the platform library to intrinsics named by their qualified name (Console.log); a workspace package whose entries are uncommitted build output resolves to the sources it is built from; sites the compiler cannot bind keep their syntax-tier binding."

// Compiler runs the TypeScript compiler bridge.
type Compiler struct {
	NodePath     string
	AnalyzerPath string
	MaxHeapMiB   int
	Timeout      time.Duration
}

// AnalyzerPath finds the bridge: the configured path, then
// CODEGRAPH_TYPESCRIPT_ANALYZER, the image's copy, and the source tree's
// build.
func AnalyzerPath(configured string) string {
	if configured != "" {
		return configured
	}
	if p := os.Getenv("CODEGRAPH_TYPESCRIPT_ANALYZER"); p != "" {
		return p
	}
	if _, err := os.Stat("/opt/codegraph/typescript/bridge.cjs"); err == nil {
		return "/opt/codegraph/typescript/bridge.cjs"
	}
	cwd, _ := os.Getwd()
	for p := cwd; p != ""; p = filepath.Dir(p) {
		candidate := filepath.Join(p, "apps/forge-codegraph-worker/typescript-analyzer/dist/bridge.cjs")
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
		if candidate := filepath.Join(p, "typescript-analyzer/dist/bridge.cjs"); fileExists(candidate) {
			return candidate
		}
		if filepath.Dir(p) == p {
			break
		}
	}
	return filepath.Join(cwd, "apps/forge-codegraph-worker/typescript-analyzer/dist/bridge.cjs")
}

func fileExists(p string) bool {
	info, err := os.Stat(p)
	return err == nil && info.Mode().IsRegular()
}

// site is what the compiler is asked about a lookup: the kind of syntax it
// stands for and its span.
type site struct {
	kind string // call, name, member, type, decorator or override
	span ir.Span
}

type compilerSite struct {
	ID    string `json:"id"`
	Kind  string `json:"kind"`
	Start uint64 `json:"start"`
	End   uint64 `json:"end"`
}

type compilerFile struct {
	ID    string         `json:"id"`
	Path  string         `json:"path"`
	Sites []compilerSite `json:"sites,omitempty"`
}

type compilerRequest struct {
	Protocol int               `json:"protocol"`
	Context  string            `json:"context"`
	Root     string            `json:"root"`
	Files    []compilerFile    `json:"files"`
	Packages []compilerPackage `json:"packages,omitempty"`
}

// compilerPackage is a workspace package of the checkout, importable by
// name even when its entries are build output that is not committed.
type compilerPackage struct {
	Name string `json:"name"`
	Dir  string `json:"dir"`
}

type compilerTarget struct {
	Module    string          `json:"module"`
	Name      string          `json:"name"`
	Path      string          `json:"path,omitempty"`
	Start     uint64          `json:"start,omitempty"`
	End       uint64          `json:"end,omitempty"`
	Owners    []compilerOwner `json:"owners,omitempty"`
	Pkg       string          `json:"pkg,omitempty"`
	Qualified string          `json:"qualified,omitempty"`
}

// compilerOwner is a named declaration around a member of an object or
// type literal, and the member's path below it.
type compilerOwner struct {
	Start  uint64 `json:"start"`
	End    uint64 `json:"end"`
	Member string `json:"member"`
}

type compilerEvent struct {
	Event      string           `json:"event"`
	Protocol   int              `json:"protocol,omitempty"`
	TypeScript string           `json:"typescript,omitempty"`
	Context    string           `json:"context,omitempty"`
	File       string           `json:"file,omitempty"`
	Site       string           `json:"site,omitempty"`
	Targets    []compilerTarget `json:"targets,omitempty"`
	Programs   int              `json:"programs,omitempty"`
	Sites      int              `json:"sites,omitempty"`
	Answered   int              `json:"answered,omitempty"`
	Failures   int              `json:"failures,omitempty"`
	Seconds    float64          `json:"seconds,omitempty"`
}

// maxConfigFiles and maxConfigBytes bound the project files copied beside
// the sources.
const (
	maxConfigFiles = 20000
	maxConfigBytes = 4 << 20
)

// compile binds the pending lookups of the affected files with the
// compiler. It changes lookups in place and never fails the resolution: a
// compiler that cannot run or stops part way is a warning, and the context
// is resolved again on the next run.
func (s *run) compile(pending map[*module][]semantic.Lookup, sites map[string]site) {
	c := s.compiler
	if c == nil || len(pending) == 0 {
		return
	}
	var contexts []string
	for id := range s.settings {
		contexts = append(contexts, string(id))
	}
	sort.Strings(contexts)
	degrade := func(reason string) {
		s.result.Warnings = append(s.result.Warnings, fmt.Sprintf("The TypeScript compiler did not bind every reference (%s); the rest keep their syntax-tier bindings and are resolved again on the next run.", reason))
		s.result.Degraded = append(s.result.Degraded, contexts...)
	}
	if !fileExists(c.AnalyzerPath) {
		degrade("its bridge is not built: run npm ci && npm run build in typescript-analyzer")
		return
	}
	scratch, err := os.MkdirTemp("", "codegraph-typescript-")
	if err != nil {
		degrade(err.Error())
		return
	}
	defer os.RemoveAll(scratch)
	root, err := filepath.EvalSymlinks(scratch)
	if err != nil {
		degrade(err.Error())
		return
	}
	request, byLookup, err := s.compilerRequest(root, pending, sites)
	if err != nil {
		if s.ctx.Err() != nil {
			return
		}
		degrade(err.Error())
		return
	}
	started := time.Now()
	stats := compilerEvent{}
	answered := 0
	err = c.run(s.ctx, request, func(e compilerEvent) error {
		switch e.Event {
		case "hello":
			if e.Protocol != 1 || e.Context != request.Context {
				return errors.New("the bridge speaks another protocol")
			}
		case "lookup":
			l := byLookup[e.Site]
			if l == nil || string(l.FileID) != e.File {
				return fmt.Errorf("the bridge answered an unknown site %q", e.Site)
			}
			ids, unknown, err := s.compilerTargets(root, e.Targets)
			if err != nil {
				return err
			}
			if applyCompiler(l, ids, unknown) {
				answered++
			}
		case "done":
			stats = e
		case "file_done":
		default:
			return fmt.Errorf("unknown bridge event %q", e.Event)
		}
		return nil
	})
	slog.Info("TypeScript compiler bound sites", "files", len(request.Files), "sites", len(byLookup), "bound", answered, "programs", stats.Programs, "failures", stats.Failures, "seconds", time.Since(started).Seconds())
	if err != nil && s.ctx.Err() == nil {
		degrade(err.Error())
	}
}

// compilerRequest writes the context's files under root, as the parser read
// them, with the checkout's project files beside them, and lists the sites
// of the affected files.
func (s *run) compilerRequest(root string, pending map[*module][]semantic.Lookup, sites map[string]site) (compilerRequest, map[string]*semantic.Lookup, error) {
	var contexts []string
	for id := range s.settings {
		contexts = append(contexts, string(id))
	}
	sort.Strings(contexts)
	rq := compilerRequest{Protocol: 1, Context: strings.Join(contexts, ","), Root: root}
	byLookup := map[string]*semantic.Lookup{}
	written := map[string]bool{}
	for _, m := range s.modules {
		if err := s.ctx.Err(); err != nil {
			return rq, nil, err
		}
		if m.skipped {
			continue
		}
		content, err := s.w.SourceBytes(s.ctx, m.in)
		if err != nil {
			if s.ctx.Err() != nil {
				return rq, nil, err
			}
			continue // a file the compiler cannot see keeps its syntax-tier bindings
		}
		sum := sha256.Sum256(content)
		if hex.EncodeToString(sum[:]) != m.in.Source.ContentSHA256 {
			continue
		}
		p := filepath.Join(root, filepath.FromSlash(m.in.Source.Path))
		if !within(root, p) {
			continue
		}
		if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
			return rq, nil, err
		}
		if err := os.WriteFile(p, parser.Input{Content: content}.Text(), 0o600); err != nil {
			return rq, nil, err
		}
		written[m.in.Source.Path] = true
		f := compilerFile{ID: string(m.in.Source.FileID), Path: p}
		lookups := pending[m]
		for i := range lookups {
			l := &lookups[i]
			st, ok := sites[l.ID]
			if !ok {
				continue
			}
			f.Sites = append(f.Sites, compilerSite{ID: l.ID, Kind: st.kind, Start: st.span.Start.ByteOffset, End: st.span.End.ByteOffset})
			byLookup[l.ID] = l
		}
		rq.Files = append(rq.Files, f)
	}
	seen := map[string]bool{}
	for _, id := range contexts {
		for _, pkg := range s.settings[bc.SourceSetID(id)].Packages {
			dir := filepath.Join(root, filepath.FromSlash(pkg.Dir))
			if !seen[pkg.Name] && within(root, dir) {
				seen[pkg.Name] = true
				rq.Packages = append(rq.Packages, compilerPackage{Name: pkg.Name, Dir: dir})
			}
		}
	}
	if err := copyProjectFiles(s.ctx, s.req.CheckoutPath, root, written, s.req.BuildLimits); err != nil {
		return rq, nil, err
	}
	if err := s.linkPackages(root); err != nil {
		return rq, nil, err
	}
	return rq, byLookup, nil
}

// copyProjectFiles copies the checkout's tsconfig, jsconfig and package.json
// files under root, so the compiler reads each file's project as written.
func copyProjectFiles(ctx context.Context, checkout, root string, written map[string]bool, limits bc.Limits) error {
	if checkout == "" {
		return nil
	}
	dir, err := os.OpenRoot(checkout)
	if err != nil {
		return nil // no checkout to read: the compiler uses its defaults
	}
	defer dir.Close()
	copied, visited := 0, uint64(0)
	return fs.WalkDir(dir.FS(), ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if visited++; limits.MaxFiles > 0 && visited > limits.MaxFiles {
			return fs.SkipAll
		}
		if d.IsDir() {
			if p != "." && (d.Name() == "node_modules" || d.Name() == ".git") {
				return fs.SkipDir
			}
			return nil
		}
		name := d.Name()
		project := name == "package.json" || (strings.HasSuffix(name, ".json") && (strings.HasPrefix(name, "tsconfig") || strings.HasPrefix(name, "jsconfig")))
		if !project || written[p] || !d.Type().IsRegular() || copied >= maxConfigFiles {
			return nil
		}
		info, err := d.Info()
		if err != nil || info.Size() > maxConfigBytes {
			return nil
		}
		data, err := fs.ReadFile(dir.FS(), p)
		if err != nil {
			return nil
		}
		target := filepath.Join(root, filepath.FromSlash(p))
		if !within(root, target) {
			return nil
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
			return err
		}
		copied++
		return os.WriteFile(target, data, 0o600)
	})
}

func within(root, p string) bool {
	rel, err := filepath.Rel(root, p)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && !filepath.IsAbs(rel)
}

// compilerTargets turns the compiler's targets into symbols. unknown is set
// when a target has no symbol: a declaration of the checkout the IR does
// not declare (a member of an object literal), or a name that is no
// package's (a wildcard module declaration).
func (s *run) compilerTargets(root string, targets []compilerTarget) ([]string, bool, error) {
	var ids []string
	unknown := false
	for _, t := range targets {
		switch t.Module {
		case "source":
			rel, ok := strings.CutPrefix(t.Path, root+string(filepath.Separator))
			m := s.byPath[filepath.ToSlash(rel)]
			if !ok || m == nil || m.skipped {
				unknown = true
				continue
			}
			if d := m.declNamedAt(t.Start, t.End); d != nil {
				ids = append(ids, symbolID(m.in.Source.FileID, d.ID))
				continue
			}
			sym, ok := s.literalMember(m, t.Owners)
			if !ok {
				unknown = true
				continue
			}
			if err := s.symbols.addUnique(sym); err != nil {
				return nil, false, err
			}
			ids = append(ids, sym.ID)
		case "package":
			if t.Pkg == "" || strings.ContainsAny(t.Pkg, "*\n\x00") || !validText(t.Pkg, 512) || (t.Qualified != "" && !validText(t.Qualified, 2048)) {
				unknown = true
				continue
			}
			sym := externalSymbol(canonicalSpecifier(t.Pkg), t.Qualified)
			if err := s.symbols.addUnique(sym); err != nil {
				return nil, false, err
			}
			ids = append(ids, sym.ID)
		case "lib":
			if !validText(t.Qualified, 2048) {
				unknown = true
				continue
			}
			sym := intrinsic(t.Qualified)
			if err := s.symbols.addUnique(sym); err != nil {
				return nil, false, err
			}
			ids = append(ids, sym.ID)
		default:
			unknown = true
		}
	}
	return ids, unknown, nil
}

// literalMember is a member of an object or type literal (a zod schema's
// field, an inline props type, a returned object's property), which the
// graph has no declaration for: a member derived from the innermost
// declaration around it that has a portable key, named by the path below
// it (web/src/schemas#TaskRunSchema.runId).
func (s *run) literalMember(m *module, owners []compilerOwner) (semantic.Symbol, bool) {
	for _, o := range owners {
		d := m.declNamedAt(o.Start, o.End)
		if d == nil {
			continue
		}
		key := s.key(m, d)
		if key == nil {
			continue // a local or parameter: the next declaration out
		}
		signature := key.CanonicalSignature + "." + o.Member
		name := o.Member[strings.LastIndex(o.Member, ".")+1:]
		if !validText(signature, 4096) || !validText(name, 512) {
			return semantic.Symbol{}, false
		}
		owner := symbolID(m.in.Source.FileID, d.ID)
		return semantic.Symbol{
			ID: graph.ID("sym", "typescript-derived", owner, o.Member), Name: name, OwnerSymbolID: owner,
			Key:     &semantic.DeclarationKey{OwnerKey: key.CanonicalSignature, Kind: ir.DeclarationField, Name: name, CanonicalSignature: signature},
			Derived: &semantic.DerivedSymbol{Language: Language, Rule: "literal_member", SourceSymbolIDs: []string{owner}, DefinitionDigest: graph.Digest([]string{CompilerVersion, "literal_member"})},
		}, true
	}
	return semantic.Symbol{}, false
}

// applyCompiler gives a lookup the compiler's targets. With none that are
// symbols it keeps its syntax-tier binding and reports false.
func applyCompiler(l *semantic.Lookup, ids []string, unknown bool) bool {
	sort.Strings(ids)
	ids = uniqueStrings(ids)
	if len(ids) == 0 || (len(ids) == 1 && unknown) {
		return false
	}
	l.Provenance = "compiler"
	l.HasUnknownCandidates = unknown
	if len(ids) == 1 {
		l.Status, l.SelectedSymbolID, l.CandidateIDs = semantic.LookupResolved, ids[0], []string{ids[0]}
		l.Cause, l.Reason, l.CandidateRole = "", "", ""
		return true
	}
	l.Status, l.SelectedSymbolID, l.CandidateIDs = semantic.LookupAmbiguous, "", ids
	l.CandidateRole = "binding_alternative"
	l.Cause = semantic.CauseAmbiguousBinding
	l.Reason = "the compiler binds the member of each constituent of a union"
	return true
}

func uniqueStrings(sorted []string) []string {
	out := sorted[:0]
	for i, id := range sorted {
		if i == 0 || id != sorted[i-1] {
			out = append(out, id)
		}
	}
	return out
}

// declNamedAt is the declaration whose name spans exactly those bytes.
func (m *module) declNamedAt(start, end uint64) *decl {
	if m.byName == nil {
		m.byName = map[[2]uint64]int{}
		for i := range m.decls {
			if span := m.decls[i].NameSpan; span != nil {
				m.byName[[2]uint64{span.Start.ByteOffset, span.End.ByteOffset}] = i
			}
		}
	}
	i, ok := m.byName[[2]uint64{start, end}]
	if !ok {
		return nil
	}
	return &m.decls[i]
}

// run starts the bridge and hands each event it writes to accept, stopping
// at the first error.
func (c *Compiler) run(ctx context.Context, rq compilerRequest, accept func(compilerEvent) error) error {
	data, err := json.Marshal(rq)
	if err != nil {
		return err
	}
	timeout := c.Timeout
	if timeout <= 0 {
		timeout = 15 * time.Minute
	}
	heap := c.MaxHeapMiB
	if heap <= 0 {
		heap = DefaultCompilerHeapMiB
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	node := c.NodePath
	if node == "" {
		node = "node"
	}
	cmd := exec.CommandContext(ctx, node, "--max-old-space-size="+strconv.Itoa(heap), "--stack-size=4000", c.AnalyzerPath)
	cmd.Stdin = bytes.NewReader(data)
	cmd.Dir = rq.Root
	stderr := &cappedBuffer{}
	cmd.Stderr = stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("node could not start: %w", err)
	}
	scanner := bufio.NewScanner(&io.LimitedReader{R: stdout, N: 2 << 30})
	scanner.Buffer(make([]byte, 64<<10), 16<<20)
	var streamErr error
	done := false
	for scanner.Scan() {
		var e compilerEvent
		if err := json.Unmarshal(scanner.Bytes(), &e); err != nil {
			streamErr = fmt.Errorf("the bridge wrote something other than JSON: %w", err)
			break
		}
		if err := accept(e); err != nil {
			streamErr = err
			break
		}
		done = done || e.Event == "done"
	}
	if streamErr == nil {
		streamErr = scanner.Err()
	}
	if streamErr != nil {
		cancel()
	}
	_, _ = io.Copy(io.Discard, stdout)
	waitErr := cmd.Wait()
	switch {
	case ctx.Err() == context.DeadlineExceeded:
		return fmt.Errorf("it ran longer than %s", timeout)
	case heapExhausted(stderr.String()):
		return fmt.Errorf("it ran out of its %d MiB heap with %d files", heap, len(rq.Files))
	case streamErr != nil:
		return streamErr
	case waitErr != nil:
		return fmt.Errorf("the bridge failed: %v: %s", waitErr, lastLine(stderr.String()))
	case !done:
		return errors.New("the bridge stopped before it finished")
	}
	return nil
}

type cappedBuffer struct{ bytes.Buffer }

func (b *cappedBuffer) Write(p []byte) (int, error) {
	if b.Len() < 64<<10 {
		_, _ = b.Buffer.Write(p[:min(len(p), (64<<10)-b.Len())])
	}
	return len(p), nil
}

func heapExhausted(stderr string) bool {
	return strings.Contains(stderr, "JavaScript heap out of memory") || strings.Contains(stderr, "Reached heap limit")
}

func lastLine(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	line := strings.TrimSpace(lines[len(lines)-1])
	if len(line) > 300 {
		line = line[:300] + "…"
	}
	return line
}

// BridgeDigest fingerprints the bridge's directory: the bundle, its
// version and the platform library it reads.
func BridgeDigest(analyzerPath string) (string, error) {
	root := filepath.Dir(analyzerPath)
	h := sha256.New()
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		rel, _ := filepath.Rel(root, p)
		data, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		fmt.Fprintf(h, "%d:%s:%d:", len(rel), filepath.ToSlash(rel), len(data))
		h.Write(data)
		return nil
	})
	return hex.EncodeToString(h.Sum(nil)), err
}
