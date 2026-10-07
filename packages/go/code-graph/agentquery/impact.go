package agentquery

import (
	"context"
	"fmt"
	"path"
	"sort"
	"strconv"
	"strings"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

const (
	defaultImpactDepth = 2
	maxImpactDepth     = 6
	defaultImpactLimit = 200
	maxImpactLimit     = 2000
	// impactEdgeBudget bounds the edges read per depth level; a level that
	// exceeds it is reported as truncated rather than read to the end.
	impactEdgeBudget = 20000
)

// impactKinds are the dependency edges walked backwards: whoever calls,
// references, uses, extends, overrides, implements or is bound to a
// declaration depends on it.
var impactKinds = []string{graph.EdgeCalls, graph.EdgeReferences, graph.EdgeUsesType, graph.EdgeInherits, graph.EdgeOverrides, graph.EdgeImplements, graph.EdgeFrameworkBinding}

// invocationKinds are the edges through which a declaration's behavior is
// exercised: the ones a body change propagates along.
var invocationKinds = []string{graph.EdgeCalls, graph.EdgeReferences, graph.EdgeFrameworkBinding}

// dispatchKinds relate a declaration to the declarations it can be called
// through: a method to what it overrides, a lambda or method reference to
// the abstract method it implements.
var dispatchKinds = []string{graph.EdgeOverrides, graph.EdgeImplements}

// Change kinds: what is going to happen to the declaration decides which
// dependants matter.
const (
	// ChangeAny walks every dependant of the declaration and of what it
	// overrides or implements. It is the default.
	ChangeAny = "any"
	// ChangeBody is a behavior change with the same signature: callers and
	// references, transitively, including callers of what the declaration
	// overrides or implements since they may dispatch here.
	ChangeBody = "body"
	// ChangeSignature changes the name, parameters, return type or the type
	// of a field: every direct user must change, callers, references, type
	// uses, subtypes, overriders and implementors. No dispatch roots: a
	// changed override no longer overrides.
	ChangeSignature = "signature"
	// ChangeRemove deletes the declaration: everything that touches it.
	ChangeRemove = "remove"
	// ChangeContract changes what an abstract declaration promises: its
	// implementors and overriders must be reviewed, and so must the callers
	// of the declaration and of each of them.
	ChangeContract = "contract"
)

// changeProfile is what a change kind selects.
type changeProfile struct {
	kinds    []string
	upward   bool // add what the root overrides or implements as roots
	downward bool // add what overrides or implements the root as roots
	note     string
}

func profileFor(change string) (changeProfile, error) {
	switch change {
	case "", ChangeAny:
		return changeProfile{kinds: impactKinds, upward: true, note: "every dependant of the declaration and of what it overrides or implements"}, nil
	case ChangeBody:
		return changeProfile{kinds: invocationKinds, upward: true, note: "callers and references, including callers of what the declaration overrides or implements"}, nil
	case ChangeSignature:
		return changeProfile{kinds: impactKinds, note: "every direct user must change: callers, references, type uses, subtypes, overriders and implementors"}, nil
	case ChangeRemove:
		return changeProfile{kinds: impactKinds, note: "everything that touches the declaration"}, nil
	case ChangeContract:
		return changeProfile{kinds: impactKinds, downward: true, note: "implementors and overriders, and the callers of the declaration and of each of them"}, nil
	}
	return changeProfile{}, fmt.Errorf("%w: change must be any, body, signature, remove or contract", deployment.ErrInvalidRequest)
}

// ImpactRequest asks what depends on a node, transitively up to Depth hops,
// at Generation (zero means live), for a Change of the given kind. Local
// variables, parameters and pattern variables that use a type are folded
// away unless IncludeLocals is set: the method that declares them is the
// dependant an agent acts on, and it is reached through its own edges.
type ImpactRequest struct {
	RepositoryID  string
	NodeID        string
	Generation    uint64
	Change        string
	Depth         int
	Limit         int
	IncludeLocals bool
	// Across, when set, continues the walk over cross-repository links into
	// the repositories it says the caller may read.
	Across Readable
}

// localKinds are declarations scoped to one body; they never have
// dependants of their own beyond that body.
var localKinds = map[string]bool{"local_variable": true, "parameter": true, "pattern_variable": true, "type_parameter": true}

// ImpactHit is one node reached by walking dependency edges backwards from
// a root: Via is the kind of the first edge that reached it, From the node
// that edge leads to, Depth the hop count and Edges how many dependency
// edges from that depth's frontier reach the node. Module and Root locate
// it in the build: the module directory and the source root (main, test).
type ImpactHit struct {
	Node   graph.Version `json:"node"`
	Via    string        `json:"via"`
	From   string        `json:"from"`
	Depth  int           `json:"depth"`
	Edges  int           `json:"edges"`
	Module string        `json:"module,omitempty"`
	Root   string        `json:"root,omitempty"`
}

// ImpactResult lists the dependants of the roots. Roots holds the changed
// declaration first, then the declarations the change reaches through
// dispatch. Edges is the number of dependency edges the walk followed; Hits
// holds each impacted node once; Assessment groups the hits by module and
// root, names the entry points and selects the tests to run.
type ImpactResult struct {
	Root       string      `json:"root,omitempty"`
	Roots      []string    `json:"roots"`
	Change     string      `json:"change"`
	Note       string      `json:"note"`
	Hits       []ImpactHit `json:"hits"`
	Edges      int         `json:"edges"`
	Skipped    int         `json:"skipped_locals,omitempty"` // local declarations folded away
	Truncated  bool        `json:"truncated"`
	Assessment *Assessment `json:"assessment,omitempty"`
	// Across is where the change reaches other repositories through
	// cross-repository links, when the request asked to follow them.
	Across []ImpactAcross `json:"across,omitempty"`
}

// Impact walks incoming dependency edges breadth-first from the root and its
// dispatch roots, one depth level per query over the edge indexes rather
// than one query per node. It stops at Depth hops, at Limit hits or at the
// per-level edge budget, and reports truncation instead of failing.
func Impact(ctx context.Context, st Store, req ImpactRequest) (ImpactResult, error) {
	if req.Depth < 0 || req.Depth > maxImpactDepth || req.Limit < 0 || req.Limit > maxImpactLimit {
		return ImpactResult{}, fmt.Errorf("%w: impact depth 0-%d and limit 0-%d", deployment.ErrInvalidRequest, maxImpactDepth, maxImpactLimit)
	}
	profile, err := profileFor(req.Change)
	if err != nil {
		return ImpactResult{}, err
	}
	if req.Depth == 0 {
		req.Depth = defaultImpactDepth
	}
	if req.Limit == 0 {
		req.Limit = defaultImpactLimit
	}
	if _, err := st.GetNode(ctx, req.RepositoryID, req.NodeID, req.Generation); err != nil {
		return ImpactResult{}, err
	}
	// With what the declaration overrides or implements (upward), or what
	// overrides or implements it (downward), as the change kind says.
	roots, err := withDispatch(ctx, st, req.RepositoryID, req.Generation, []string{req.NodeID}, profile)
	if err != nil {
		return ImpactResult{}, err
	}
	result, err := impactFrom(ctx, st, req.RepositoryID, req.Generation, roots, profile, req.Depth, req.Limit, req.IncludeLocals)
	if err != nil {
		return ImpactResult{}, err
	}
	result.Root = req.NodeID
	if err := assess(ctx, st, req.RepositoryID, req.Generation, &result); err != nil {
		return ImpactResult{}, err
	}
	if req.Across != nil {
		across, cut, err := impactAcross(ctx, st, req, result)
		if err != nil {
			return ImpactResult{}, err
		}
		result.Across = across
		result.Truncated = result.Truncated || cut
	}
	return result, nil
}

// impactFrom is the walk shared by Impact and ChangeImpact: every root is
// visited at depth zero, then each depth level is one batched read.
func impactFrom(ctx context.Context, st Store, repo string, generation uint64, roots []string, profile changeProfile, depth, limit int, includeLocals bool) (ImpactResult, error) {
	result := ImpactResult{Roots: roots, Change: changeName(profile), Note: profile.note, Hits: []ImpactHit{}}
	visited := make(map[string]bool, len(roots))
	for _, id := range roots {
		visited[id] = true
	}
	type reached struct {
		via, from string
		edges     int
	}
	frontier := append([]string(nil), roots...)
	for d := 1; d <= depth && len(frontier) > 0 && !result.Truncated; d++ {
		refs, cut, err := st.DependencyEdges(ctx, repo, frontier, graph.Incoming, profile.kinds, generation, impactEdgeBudget)
		if err != nil {
			return ImpactResult{}, err
		}
		if cut {
			result.Truncated = true
		}
		result.Edges += len(refs)
		level := map[string]*reached{}
		var order []string
		for _, e := range refs {
			far := e.SourceID
			if far == "" {
				continue
			}
			if r, ok := level[far]; ok {
				r.edges++
				continue
			}
			if visited[far] {
				continue
			}
			level[far] = &reached{via: e.Kind, from: e.TargetID, edges: 1}
			order = append(order, far)
		}
		nodes, err := st.GetNodes(ctx, repo, order, generation)
		if err != nil {
			return ImpactResult{}, err
		}
		var next []string
		for _, id := range order {
			v, ok := nodes[id]
			if !ok {
				visited[id] = true
				continue
			}
			if !includeLocals && v.Fact.Node != nil && localKinds[v.Fact.Node.Kind] {
				visited[id] = true
				result.Skipped++
				continue
			}
			if len(result.Hits) >= limit {
				result.Truncated = true
				break
			}
			visited[id] = true
			r := level[id]
			result.Hits = append(result.Hits, ImpactHit{Node: v, Via: r.via, From: r.from, Depth: d, Edges: r.edges})
			next = append(next, id)
		}
		frontier = next
	}
	return result, nil
}

func changeName(p changeProfile) string {
	for _, name := range []string{ChangeAny, ChangeBody, ChangeSignature, ChangeRemove, ChangeContract} {
		if q, _ := profileFor(name); q.note == p.note {
			return name
		}
	}
	return ChangeAny
}

// --- assessment ---------------------------------------------------------------

// Assessment turns a list of dependants into what a reviewer asks for: how
// the impact spreads over modules and source roots, how deep it goes, which
// of the impacted declarations nothing else depends on (the entry points a
// change surfaces through), and which tests exercise the impacted code.
type Assessment struct {
	Groups              []ImpactGroup  `json:"groups"`
	ByDepth             map[string]int `json:"by_depth"`
	EntryPoints         []Brief        `json:"entry_points"`
	EntryPointsComplete bool           `json:"entry_points_complete"`
	Tests               []TestClass    `json:"tests"`
	Commands            []string       `json:"commands"`
}

// ImpactGroup counts impacted declarations in one module and source root.
type ImpactGroup struct {
	Module string `json:"module"`
	Root   string `json:"root"`
	Nodes  int    `json:"nodes"`
	Edges  int    `json:"edges"`
}

// TestClass is a test source file with impacted declarations, named the way
// the build's test runner selects it.
type TestClass struct {
	Module   string `json:"module"`
	Class    string `json:"class"`
	File     string `json:"file"`
	Language string `json:"language,omitempty"`
	Nodes    int    `json:"nodes"`
}

// fileInfo is what a source_file node says about where a hit lives.
type fileInfo struct {
	path, module, root, language string
}

// assess fills the hits' module and root and builds the Assessment.
func assess(ctx context.Context, st Store, repo string, generation uint64, result *ImpactResult) error {
	var lineages []string
	for _, h := range result.Hits {
		if h.Node.Lineage != "" {
			lineages = append(lineages, h.Node.Lineage)
		}
	}
	files, err := st.GetNodes(ctx, repo, lineages, generation)
	if err != nil {
		return err
	}
	infos := make(map[string]fileInfo, len(files))
	for id, v := range files {
		if v.Fact.Node == nil {
			continue
		}
		infos[id] = locate(v.Fact.Node.Properties)
	}
	a := &Assessment{Groups: []ImpactGroup{}, ByDepth: map[string]int{}, EntryPoints: []Brief{}, EntryPointsComplete: true, Tests: []TestClass{}, Commands: []string{}}
	groups := map[string]*ImpactGroup{}
	tests := map[string]*TestClass{}
	var mainIDs []string
	for i := range result.Hits {
		h := &result.Hits[i]
		info, ok := infos[h.Node.Lineage]
		if !ok && h.Node.Fact.Node != nil {
			info = locate(h.Node.Fact.Node.Properties)
		}
		h.Module, h.Root = info.module, info.root
		a.ByDepth[strconv.Itoa(h.Depth)]++
		key := info.module + "\x00" + info.root
		g := groups[key]
		if g == nil {
			g = &ImpactGroup{Module: info.module, Root: info.root}
			groups[key] = g
		}
		g.Nodes++
		g.Edges += h.Edges
		if info.root == "test" {
			if tc := testClassOf(info); tc != nil {
				k := info.module + "\x00" + tc.Class
				if tests[k] == nil {
					tests[k] = tc
				}
				tests[k].Nodes++
			}
			continue
		}
		if h.Node.Fact.Node != nil {
			mainIDs = append(mainIDs, h.Node.Fact.Node.ID)
		}
	}
	for _, g := range groups {
		a.Groups = append(a.Groups, *g)
	}
	sort.Slice(a.Groups, func(i, j int) bool {
		if a.Groups[i].Nodes != a.Groups[j].Nodes {
			return a.Groups[i].Nodes > a.Groups[j].Nodes
		}
		return a.Groups[i].Module+a.Groups[i].Root < a.Groups[j].Module+a.Groups[j].Root
	})
	for _, tc := range tests {
		a.Tests = append(a.Tests, *tc)
	}
	sort.Slice(a.Tests, func(i, j int) bool {
		if a.Tests[i].Module != a.Tests[j].Module {
			return a.Tests[i].Module < a.Tests[j].Module
		}
		return a.Tests[i].Class < a.Tests[j].Class
	})
	a.Commands = testCommands(a.Tests)
	// Entry points: impacted declarations outside tests that nothing depends
	// on; whatever surfaces the change to callers outside the graph.
	if len(mainIDs) > 0 {
		refs, cut, err := st.DependencyEdges(ctx, repo, mainIDs, graph.Incoming, spannerstore.SemanticEdgeKinds(), generation, impactEdgeBudget)
		if err != nil {
			return err
		}
		a.EntryPointsComplete = !cut
		depended := make(map[string]bool, len(refs))
		for _, e := range refs {
			depended[e.TargetID] = true
		}
		if !cut {
			for _, h := range result.Hits {
				if h.Root != "test" && h.Node.Fact.Node != nil && !depended[h.Node.Fact.Node.ID] {
					a.EntryPoints = append(a.EntryPoints, Briefly(*h.Node.Fact.Node))
				}
			}
		}
	}
	result.Assessment = a
	return nil
}

// locate derives module and source root from a file node's properties: the
// source set tells main from test; the path gives the module directory in a
// Maven or Gradle layout (<module>/src/<root>/...), "." for the root module.
func locate(props map[string]graph.PropertyValue) fileInfo {
	info := fileInfo{path: graph.Text(props, "file_path"), language: graph.Text(props, "language")}
	if i := strings.LastIndex(graph.Text(props, "source_set_id"), ":"); i >= 0 {
		info.root = graph.Text(props, "source_set_id")[i+1:]
	}
	p := info.path
	rest := ""
	switch i := strings.Index(p, "/src/"); {
	case strings.HasPrefix(p, "src/"):
		info.module, rest = ".", p[len("src/"):]
	case i >= 0:
		info.module, rest = p[:i], p[i+len("/src/"):]
	case strings.Contains(p, "/"):
		info.module = p[:strings.Index(p, "/")]
	default:
		info.module = "."
	}
	if info.root == "" {
		switch segment := rest[:max(0, strings.IndexByte(rest+"/", '/'))]; {
		case segment == "test", strings.HasSuffix(segment, "Test"), strings.HasSuffix(segment, "test"):
			info.root = "test"
		case strings.Contains(p, "/test/") || strings.HasPrefix(p, "test/"):
			info.root = "test"
		default:
			info.root = "main"
		}
	}
	return info
}

// testClassOf names the test class the build's runner selects for a test
// source file: the file's base name, qualified by the package the path
// implies for Java and Kotlin sources.
func testClassOf(info fileInfo) *TestClass {
	base := path.Base(info.path)
	ext := path.Ext(base)
	if ext == "" {
		return nil
	}
	class := strings.TrimSuffix(base, ext)
	for _, marker := range []string{"/java/", "/kotlin/"} {
		if i := strings.Index(info.path, marker); i >= 0 {
			pkg := strings.TrimSuffix(info.path[i+len(marker):], base)
			pkg = strings.Trim(strings.ReplaceAll(pkg, "/", "."), ".")
			if pkg != "" {
				class = pkg + "." + class
			}
			break
		}
	}
	return &TestClass{Module: info.module, Class: class, File: info.path, Language: info.language}
}

// testCommands renders one Maven selection per module for Java and Kotlin
// tests; other languages list their files and get no command.
func testCommands(tests []TestClass) []string {
	byModule := map[string][]string{}
	var modules []string
	for _, tc := range tests {
		if tc.Language != "java" && tc.Language != "kotlin" {
			continue
		}
		if _, ok := byModule[tc.Module]; !ok {
			modules = append(modules, tc.Module)
		}
		byModule[tc.Module] = append(byModule[tc.Module], tc.Class)
	}
	sort.Strings(modules)
	var out []string
	for _, m := range modules {
		classes := byModule[m]
		sort.Strings(classes)
		cmd := "mvn"
		if m != "." {
			cmd += " -pl " + m + " -am"
		}
		out = append(out, cmd+" -Dtest="+strings.Join(classes, ",")+" -Dsurefire.failIfNoSpecifiedTests=false test")
	}
	return out
}

// --- compact form ----------------------------------------------------------------

// ImpactBrief is an impacted node in compact form.
type ImpactBrief struct {
	Brief
	Via    string `json:"via"`
	From   string `json:"from"`
	Depth  int    `json:"depth"`
	Edges  int    `json:"edges"`
	Module string `json:"module,omitempty"`
	Root   string `json:"root,omitempty"`
}

// CompactImpactResult is ImpactResult without records: one brief per
// impacted node with the edge count that reached it, the totals, a
// breakdown of impacted nodes by kind and the assessment.
type CompactImpactResult struct {
	Root       string                `json:"root,omitempty"`
	Roots      []string              `json:"roots"`
	Change     string                `json:"change"`
	Note       string                `json:"note"`
	Nodes      []ImpactBrief         `json:"nodes"`
	Edges      int                   `json:"edges"`
	Skipped    int                   `json:"skipped_locals,omitempty"`
	ByKind     map[string]int        `json:"by_kind"`
	Truncated  bool                  `json:"truncated"`
	Assessment *Assessment           `json:"assessment,omitempty"`
	Across     []CompactImpactAcross `json:"across,omitempty"`
}

// CompactImpact projects an impact result.
func CompactImpact(r ImpactResult) CompactImpactResult {
	out := CompactImpactResult{Root: r.Root, Roots: r.Roots, Change: r.Change, Note: r.Note, Nodes: make([]ImpactBrief, 0, len(r.Hits)), Edges: r.Edges, Skipped: r.Skipped, ByKind: map[string]int{}, Truncated: r.Truncated, Assessment: r.Assessment, Across: compactAcross(r.Across)}
	for _, h := range r.Hits {
		b := ImpactBrief{Via: h.Via, From: h.From, Depth: h.Depth, Edges: h.Edges, Module: h.Module, Root: h.Root}
		if h.Node.Fact.Node != nil {
			b.Brief = Briefly(*h.Node.Fact.Node)
			out.ByKind[h.Node.Fact.Node.Kind]++
		}
		out.Nodes = append(out.Nodes, b)
	}
	return out
}
