// Package discovery streams source identities from explicit BuildContext
// roots. It never retains a repository-sized file list or follows symlinks.
package discovery

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"sort"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/ir"
)

const Version = "1.3.0"

var (
	ErrLimit       = errors.New("discovery: limit exceeded")
	ErrChanged     = errors.New("discovery: source changed")
	ErrUnsupported = errors.New("discovery: unsupported filesystem entry")
)

type Limits struct {
	MaxEntries   uint64 `json:"max_entries"`
	MaxFiles     uint64 `json:"max_files"`
	MaxHashBytes uint64 `json:"max_hash_bytes"`
	MaxDepth     int    `json:"max_depth"`
	MaxRoots     int    `json:"max_roots"`
	MaxIssues    int    `json:"max_issues"`
	// IncludeHidden and IncludeTests admit what discovery leaves out by
	// default: every path with a segment that starts with a dot (.github,
	// .agents, .eslintrc.js), and test code: test source sets, and the
	// directories and files that are tests by convention (testDirectory,
	// testFile).
	IncludeHidden bool `json:"include_hidden"`
	IncludeTests  bool `json:"include_tests"`
}

func DefaultLimits() Limits {
	return Limits{MaxEntries: 1_000_000, MaxFiles: 100_000, MaxHashBytes: 8 << 30, MaxDepth: 128, MaxRoots: 1024, MaxIssues: 128}
}
func (l Limits) Validate() error {
	if l.MaxEntries == 0 || l.MaxFiles == 0 || l.MaxHashBytes == 0 || l.MaxDepth < 1 || l.MaxDepth > 256 || l.MaxRoots < 1 || l.MaxRoots > 10_000 || l.MaxIssues < 1 || l.MaxIssues > 1024 {
		return fmt.Errorf("%w: explicit positive supported limits required", ErrLimit)
	}
	return nil
}

type Issue struct {
	Code        string `json:"code"`
	Path        string `json:"path"`
	SourceSetID string `json:"source_set_id"`
}
type Report struct {
	Entries             uint64  `json:"entries"`
	Files               uint64  `json:"files"` // compilation-context variants, not unique physical paths
	HashBytes           uint64  `json:"hash_bytes"`
	ExcludedFiles       uint64  `json:"excluded_files"`
	ExcludedDirectories uint64  `json:"excluded_directories"` // subtrees skipped by a whole-tree exclude pattern
	UnsupportedFiles    uint64  `json:"unsupported_files"`    // regular files without a registered language
	OtherLanguageFiles  uint64  `json:"other_language_files"` // registered files outside this source set's language
	IgnoredGitEntries   uint64  `json:"ignored_git_entries"`
	HiddenEntries       uint64  `json:"hidden_entries"`   // dot files, directories and source roots left out
	TestEntries         uint64  `json:"test_entries"`     // test files and directories left out
	TestSourceSets      uint64  `json:"test_source_sets"` // test source sets left out whole
	Omissions           uint64  `json:"omissions"`
	Issues              []Issue `json:"issues,omitempty"`
}

type walker struct {
	ctx        context.Context
	root       *os.Root
	build      bc.BuildContext
	limits     Limits
	report     Report
	buffer     []byte
	emit       func(ir.Source) error
	classifier LanguageClassifier
}

// LanguageClassifier identifies syntax adapters by exact filename extension.
// parser.Registry implements this interface.
type LanguageClassifier interface {
	LanguageForPath(string) (string, bool)
}

// WalkWithClassifier emits one exact source identity at a time. Available checkout roots are
// processed; missing/mismatched/external roots and symlinks become omissions.
// I/O, changed inputs, invalid paths and budgets are fatal; callers must not
// seal discovery on error. Duplicate/overlapping roots within a set collapse,
// but the same file in two source sets intentionally has two identities.
func WalkWithClassifier(ctx context.Context, checkout bc.Checkout, build bc.BuildContext, limits Limits, classifier LanguageClassifier, emit func(ir.Source) error) (report Report, err error) {
	defer func() { report.Issues = boundIssues(report.Issues, limits.MaxIssues) }()
	if classifier == nil {
		return Report{}, fmt.Errorf("discovery: language classifier required")
	}
	if err := limits.Validate(); err != nil {
		return Report{}, err
	}
	if emit == nil {
		return Report{}, fmt.Errorf("discovery: callback required")
	}
	if err := build.Validate(); err != nil {
		return Report{}, err
	}
	if build.RepositoryID != checkout.RepositoryID || build.SnapshotID != checkout.SnapshotID {
		return Report{}, bc.ErrIdentityMismatch
	}
	if err := ctx.Err(); err != nil {
		return Report{}, err
	}
	root, err := os.OpenRoot(checkout.Path)
	if err != nil {
		return Report{}, err
	}
	defer root.Close()
	w := walker{ctx: ctx, root: root, build: build, limits: limits, buffer: make([]byte, 32<<10), emit: emit, classifier: classifier}
	inputs := map[bc.InputID]bc.Input{}
	checks := map[bc.InputID]bc.CheckStatus{}
	for _, in := range build.Inventory.Inputs {
		inputs[in.ID] = in
	}
	for _, c := range build.Checks {
		checks[c.InputID] = c.Status
	}
	sets := append([]bc.SourceSet(nil), build.Inventory.SourceSets...)
	sort.Slice(sets, func(i, j int) bool { return sets[i].ID < sets[j].ID })
	count := 0
	for _, set := range sets {
		if set.Kind == bc.SourceSetTest && !limits.IncludeTests {
			w.report.TestSourceSets++
			continue
		}
		var roots []string
		ids := append(append([]bc.InputID(nil), set.SourceRootIDs...), set.GeneratedRootIDs...)
		for _, id := range ids {
			count++
			if count > limits.MaxRoots {
				return w.report, fmt.Errorf("%w: more than %d source roots", ErrLimit, limits.MaxRoots)
			}
			in := inputs[id]
			if checks[id] != bc.Available {
				w.issue("unavailable_source_root", string(id), set)
				continue
			}
			if in.Location == nil || in.Location.Root != "checkout" {
				w.issue("external_source_root", string(id), set)
				continue
			}
			if !limits.IncludeHidden && hiddenPath(in.Location.Path) {
				w.report.HiddenEntries++
				continue
			}
			roots = append(roots, in.Location.Path)
		}
		sort.Strings(roots)
		selected := map[string]bool{}
		for _, p := range roots {
			duplicate := false
			for ancestor := p; ; ancestor = path.Dir(ancestor) {
				if selected[ancestor] {
					duplicate = true
					break
				}
				if ancestor == "." {
					break
				}
			}
			if duplicate {
				continue
			}
			selected[p] = true
			info, err := inspect(root, p)
			if errors.Is(err, ErrUnsupported) {
				w.issue("unsupported_source_root", p, set)
				continue
			}
			if err != nil {
				return w.report, err
			}
			if !info.IsDir() {
				return w.report, fmt.Errorf("%w: source root is not a directory", ErrChanged)
			}
			if err := w.directory(p, p, set, 0); err != nil {
				return w.report, err
			}
		}
	}
	return w.report, ctx.Err()
}

// issue records an omission. The report keeps the MaxIssues smallest
// issues in (source set, path, code) order; the list is sorted and cut when
// it doubles and once at the end of the walk, not on every omission, so a
// tree of many thousands of symlinks stays linear.
func (w *walker) issue(code, p string, set bc.SourceSet) {
	w.report.Omissions++
	w.report.Issues = append(w.report.Issues, Issue{Code: code, Path: p, SourceSetID: string(set.ID)})
	if len(w.report.Issues) >= 2*max(w.limits.MaxIssues, 1) {
		w.report.Issues = boundIssues(w.report.Issues, w.limits.MaxIssues)
	}
}

func boundIssues(issues []Issue, limit int) []Issue {
	sort.Slice(issues, func(i, j int) bool {
		a, b := issues[i], issues[j]
		if a.SourceSetID != b.SourceSetID {
			return a.SourceSetID < b.SourceSetID
		}
		if a.Path != b.Path {
			return a.Path < b.Path
		}
		return a.Code < b.Code
	})
	if len(issues) > limit {
		issues = issues[:limit]
	}
	return issues
}
func (w *walker) directory(p, sourceRoot string, set bc.SourceSet, depth int) error {
	if err := w.ctx.Err(); err != nil {
		return err
	}
	if depth > w.limits.MaxDepth {
		// Nested beyond any source tree (a vendored or generated
		// hierarchy): its files are left out, not the run.
		w.issue("too_deep", p, set)
		return nil
	}
	f, err := w.root.Open(p)
	if err != nil {
		return err
	}
	defer f.Close()
	for {
		entries, readErr := f.ReadDir(256)
		for _, entry := range entries {
			if err := w.ctx.Err(); err != nil {
				return err
			}
			w.report.Entries++
			if w.report.Entries > w.limits.MaxEntries {
				return fmt.Errorf("%w: more than %d directory entries", ErrLimit, w.limits.MaxEntries)
			}
			if entry.Name() == ".git" {
				w.report.IgnoredGitEntries++
				continue
			}
			if !w.limits.IncludeHidden && strings.HasPrefix(entry.Name(), ".") {
				w.report.HiddenEntries++
				continue
			}
			name := path.Join(p, entry.Name())
			if !validPath(name) {
				// A name the IR cannot carry (a control character, as in
				// macOS's Icon\r, invalid UTF-8, a colon or backslash): the
				// entry is left out, not the run.
				w.issue("unrepresentable_path", strconv.QuoteToASCII(name), set)
				continue
			}
			info, err := w.root.Lstat(name)
			if err != nil {
				return err
			}
			switch {
			case info.Mode()&os.ModeSymlink != 0:
				w.issue("symlink", name, set)
			case info.IsDir():
				if excludesTree(set, relativeTo(name, sourceRoot)) {
					// Every descendant is excluded by a whole-tree pattern
					// (node_modules, a build output): skip the walk itself.
					w.report.ExcludedDirectories++
					continue
				}
				if !w.limits.IncludeTests && testDirectory(set, p, entry.Name()) {
					w.report.TestEntries++
					continue
				}
				if err := w.directory(name, sourceRoot, set, depth+1); err != nil {
					return err
				}
			case !info.Mode().IsRegular():
				w.issue("special_file", name, set)
			default:
				language, supported := w.classifier.LanguageForPath(name)
				if !supported {
					w.report.UnsupportedFiles++
					continue
				}
				setLanguage, version := set.SyntaxLanguage()
				if language != setLanguage {
					w.report.OtherLanguageFiles++
					continue
				}
				if !w.limits.IncludeTests && testFile(entry.Name()) {
					w.report.TestEntries++
					continue
				}
				relative := relativeTo(name, sourceRoot)
				if !set.SelectsSource(relative) {
					w.report.ExcludedFiles++
					continue
				}
				w.report.Files++
				if w.report.Files > w.limits.MaxFiles {
					return fmt.Errorf("%w: more than %d files to analyse (a file in several source sets counts once for each)", ErrLimit, w.limits.MaxFiles)
				}
				hash, size, err := w.hash(name)
				if err != nil {
					return err
				}
				src := ir.Source{RepositoryID: w.build.RepositoryID, SnapshotID: w.build.SnapshotID, BuildContextID: string(w.build.ID), Path: name,
					ContentSHA256: hash, SizeBytes: size, Language: language, LanguageVersion: version, ModuleID: string(set.ModuleID), SourceSetID: string(set.ID)}
				idBytes, _ := json.Marshal(struct{ Repository, Snapshot, Build, Path, Module, Set string }{src.RepositoryID, src.SnapshotID, src.BuildContextID, src.Path, src.ModuleID, src.SourceSetID})
				id := sha256.Sum256(idBytes)
				src.FileID = ir.FileID("file:" + hex.EncodeToString(id[:]))
				if err := w.emit(src); err != nil {
					return err
				}
			}
		}
		if readErr == io.EOF {
			return nil
		}
		if readErr != nil {
			return readErr
		}
	}
}
func relativeTo(name, sourceRoot string) string {
	if sourceRoot == "." {
		return name
	}
	return strings.TrimPrefix(name, sourceRoot+"/")
}

// hiddenPath reports whether a checkout path has a segment that starts
// with a dot.
func hiddenPath(p string) bool {
	for _, segment := range strings.Split(p, "/") {
		if segment != "." && strings.HasPrefix(segment, ".") {
			return true
		}
	}
	return false
}

// testDirectory reports whether a directory holds tests by convention:
// Maven's and Gradle's src/test in every language, and test, tests,
// __tests__ and __mocks__ outside Java, where a package may be named test.
func testDirectory(set bc.SourceSet, parent, name string) bool {
	if name == "test" && path.Base(parent) == "src" {
		return true
	}
	if language, _ := set.SyntaxLanguage(); language == "java" {
		return false
	}
	switch name {
	case "test", "tests", "__tests__", "__mocks__":
		return true
	}
	return false
}

// testFile reports whether a file is a test by its name: pytest's test_*.py,
// *_test.py and conftest.py, Django's tests.py, and the *.test.* and
// *.spec.* of JavaScript and TypeScript test runners.
func testFile(name string) bool {
	ext := path.Ext(name)
	stem := strings.TrimSuffix(name, ext)
	switch ext {
	case ".py", ".pyi":
		return strings.HasPrefix(stem, "test_") || strings.HasSuffix(stem, "_test") || stem == "conftest" || stem == "tests"
	case ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs":
		return strings.HasSuffix(stem, ".test") || strings.HasSuffix(stem, ".spec")
	}
	return false
}

// excludesTree reports whether an exclude pattern ending in /** covers a
// directory, so that its whole subtree can be skipped without visiting it.
// Patterns that select by file name are still applied file by file.
func excludesTree(set bc.SourceSet, dir string) bool {
	for _, pattern := range set.ExcludePatterns {
		if strings.HasSuffix(pattern, "/**") && bc.MatchSourcePattern(strings.TrimSuffix(pattern, "/**"), dir) {
			return true
		}
	}
	return false
}

func (w *walker) hash(p string) (string, uint64, error) {
	f, info, err := openRegular(w.root, p)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()

	if uint64(info.Size()) > w.limits.MaxHashBytes-w.report.HashBytes {
		return "", 0, fmt.Errorf("%w: more than %d bytes of source to read", ErrLimit, w.limits.MaxHashBytes)
	}
	h := sha256.New()
	var size uint64
	for {
		if err := w.ctx.Err(); err != nil {
			return "", 0, err
		}
		n, err := f.Read(w.buffer)
		if uint64(n) > w.limits.MaxHashBytes-w.report.HashBytes {
			return "", 0, fmt.Errorf("%w: more than %d bytes of source to read", ErrLimit, w.limits.MaxHashBytes)
		}
		size += uint64(n)
		w.report.HashBytes += uint64(n)
		h.Write(w.buffer[:n])
		if err == io.EOF {
			break
		}
		if err != nil {
			return "", 0, err
		}
	}
	after, err := f.Stat()
	if err != nil {
		return "", 0, err
	}
	if uint64(info.Size()) != size || info.Size() != after.Size() || !info.ModTime().Equal(after.ModTime()) {
		return "", 0, ErrChanged
	}
	return hex.EncodeToString(h.Sum(nil)), size, nil
}

func validPath(p string) bool {
	return len(p) <= 4096 && bc.ValidPath(p) && utf8.ValidString(p) && strings.IndexFunc(p, unicode.IsControl) < 0
}
func inspect(root *os.Root, p string) (os.FileInfo, error) {
	if !validPath(p) {
		return nil, ErrUnsupported
	}
	current := "."
	for _, part := range strings.Split(p, "/") {
		if part == ".git" {
			return nil, ErrUnsupported
		}
		current = path.Join(current, part)
		info, err := root.Lstat(current)
		if err != nil {
			return nil, err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return nil, ErrUnsupported
		}
	}
	return root.Lstat(p)
}
func openRegular(root *os.Root, p string) (*os.File, os.FileInfo, error) {
	info, err := inspect(root, p)
	if err != nil {
		return nil, nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, nil, ErrUnsupported
	}
	f, err := root.Open(p)
	if err != nil {
		return nil, nil, err
	}
	actual, err := f.Stat()
	if err != nil || !actual.Mode().IsRegular() || !os.SameFile(info, actual) {
		f.Close()
		return nil, nil, ErrChanged
	}
	return f, actual, nil
}

// ReadSource bounds allocation, refuses symlinks and authenticates exact bytes
// against discovery. Checkout immutability is still a caller responsibility.
// SniffSource reads at most the first headBytes and the last tailBytes of a
// source, for a file too large to read whole.
func SniffSource(ctx context.Context, root *os.Root, src ir.Source, headBytes, tailBytes int) ([]byte, []byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, nil, err
	}
	f, info, err := openRegular(root, src.Path)
	if err != nil {
		return nil, nil, err
	}
	defer f.Close()
	size := info.Size()
	head := make([]byte, min(int64(headBytes), size))
	if _, err := io.ReadFull(f, head); err != nil {
		return nil, nil, err
	}
	tail := make([]byte, min(int64(tailBytes), size))
	if _, err := f.ReadAt(tail, size-int64(len(tail))); err != nil && err != io.EOF {
		return nil, nil, err
	}
	return head, tail, nil
}

func ReadSource(ctx context.Context, root *os.Root, src ir.Source, maxBytes uint64) ([]byte, error) {
	if src.SizeBytes > maxBytes || maxBytes > 16<<20 {
		return nil, ErrLimit
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	f, info, err := openRegular(root, src.Path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	if uint64(info.Size()) != src.SizeBytes {
		return nil, ErrChanged
	}
	data := make([]byte, 0, int(src.SizeBytes))
	buffer := make([]byte, 32<<10)
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		n, err := f.Read(buffer)
		if uint64(len(data)+n) > src.SizeBytes {
			return nil, ErrChanged
		}
		data = append(data, buffer[:n]...)
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
	}
	h := sha256.Sum256(data)
	if uint64(len(data)) != src.SizeBytes || hex.EncodeToString(h[:]) != src.ContentSHA256 {
		return nil, ErrChanged
	}
	return data, nil
}
