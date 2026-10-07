package java

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strings"

	"ei-aitiger-codegraph/pkg/ir"
)

// event is one JSONL frame of the bridge stream. Field names are the wire
// protocol; every position is a UTF-8 byte offset into the materialized file.
type event struct {
	Kind             string `json:"kind"`
	Path             string `json:"path"`
	Start            int64  `json:"start"`
	End              int64  `json:"end"`
	TreeKind         string `json:"tree_kind"`
	ElementKind      string `json:"element_kind"`
	Name             string `json:"name"`
	Owner            string `json:"owner"`
	OwnerSourcePath  string `json:"owner_source_path"`
	OwnerSourceStart int64  `json:"owner_source_start"`
	OwnerSourceEnd   int64  `json:"owner_source_end"`
	Signature        string `json:"signature"`
	SignatureValid   string `json:"signature_valid"`
	Nesting          string `json:"nesting"`
	TargetPath       string `json:"target_path"`
	TargetStart      int64  `json:"target_start"`
	TargetEnd        int64  `json:"target_end"`
	// Generated names the processor that generated the target ("lombok");
	// its target span is then the field or annotation it came from, and
	// SourceOwner* describe the nearest enclosing type in the sources.
	Generated        string  `json:"generated"`
	SourceOwner      string  `json:"source_owner"`
	SourceOwnerPath  string  `json:"source_owner_path"`
	SourceOwnerStart int64   `json:"source_owner_start"`
	SourceOwnerEnd   int64   `json:"source_owner_end"`
	ArtifactURI      string  `json:"artifact_uri"`
	Type             string  `json:"type"`
	TypeKind         string  `json:"type_kind"`
	ArrayDimensions  int     `json:"array_dimensions"`
	Status           string  `json:"status"`
	Code             string  `json:"code"`
	Severity         string  `json:"severity"`
	Message          string  `json:"message"`
	InferredType     *event  `json:"inferred_type,omitempty"`
	TypeComponents   []event `json:"type_components,omitempty"`
	Interface        *event  `json:"interface,omitempty"`
	Errors           uint64  `json:"errors"`
	SourceFiles      uint64  `json:"source_files"`
	Compiler         string  `json:"compiler"`
	Context          string  `json:"context"`
	Millis           int64   `json:"millis"`
	Error            string  `json:"error"`
	// SkippedClasspath lists, on context_done, the classpath entries javac
	// could not open, each with the reason.
	SkippedClasspath []string `json:"skipped_classpath,omitempty"`
	// Excluded lists, on context_done, the files left out of the context
	// ("path\treason"): javac crashed on them, or their walk could not finish.
	Excluded []string `json:"excluded,omitempty"`
	// Processor is, on context_done, the Lombok JAR the context ran with;
	// ProcessorFailures the ones that could not run, each with the reason.
	Processor         string   `json:"processor,omitempty"`
	ProcessorFailures []string `json:"processor_failures,omitempty"`
	// ClassesWritten is, on context_done, "true" when the context's class
	// files were written to its generate directory.
	ClassesWritten string `json:"classes_written,omitempty"`
	// ClassesLeftOut lists, on context_done, the files left out of the
	// class files written for a context with errors: those javac rejected,
	// and those that failed without them.
	ClassesLeftOut []string `json:"classes_left_out,omitempty"`
}

// errCompilerStream marks a context's event stream that breaks the protocol:
// the files read before it are resolved, the rest go without compiler events.
var errCompilerStream = errors.New("compiler stream")

// The bridge stream is a strict JSONL protocol. Blank, contaminated, or
// truncated frames are failures with bounded evidence, never skipped records.
func decodeEvent(line []byte, lineNumber uint64) (event, error) {
	var e event
	if err := json.Unmarshal(line, &e); err != nil {
		return e, fmt.Errorf("%w: JSON frame %d (%d bytes, prefix %q): %v", errCompilerStream, lineNumber, len(line), truncate(string(line), 256), err)
	}
	switch e.Kind {
	case "binding", "candidate", "declaration", "diagnostic", "override", "implements", "summary":
	default:
		return e, fmt.Errorf("%w: JSON frame %d has unknown kind %q", errCompilerStream, lineNumber, e.Kind)
	}
	return e, nil
}

type spanKey struct{ start, end int64 }

// fileEvents holds the events of one compilation unit while it is resolved.
// The bridge walks units in order, so a change of path ends a file.
type fileEvents struct {
	path            string
	declarations    []event
	bindings        map[spanKey][]event
	bindingsByStart map[int64][]event
	candidates      map[spanKey][]event
	errors          []event // ERROR diagnostics, sorted by start
	overrides       []event
	implements      []event
	index           *declarationIndex
	// failure is why the compiler reported nothing for the file: its lookups
	// are unsupported with it. Empty for a file compiled as usual.
	failure string
}

func newFileEvents(path string) *fileEvents {
	return &fileEvents{path: path, bindings: map[spanKey][]event{}, bindingsByStart: map[int64][]event{}, candidates: map[spanKey][]event{}}
}

func (fe *fileEvents) add(e event) {
	switch e.Kind {
	case "declaration":
		fe.declarations = append(fe.declarations, e)
	case "binding":
		k := spanKey{e.Start, e.End}
		fe.bindings[k] = append(fe.bindings[k], e)
		fe.bindingsByStart[e.Start] = append(fe.bindingsByStart[e.Start], e)
	case "candidate":
		k := spanKey{e.Start, e.End}
		fe.candidates[k] = append(fe.candidates[k], e)
	case "diagnostic":
		if e.Severity == "ERROR" {
			fe.errors = append(fe.errors, e)
		}
	case "override":
		fe.overrides = append(fe.overrides, e)
	case "implements":
		fe.implements = append(fe.implements, e)
	}
}

// streamEvents reads one context's JSONL file and hands each file's events
// to fn as soon as the path changes. At most one file's events are alive.
func streamEvents(ctx context.Context, path string, fn func(*fileEvents) error) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 256<<10), 64<<20)
	var current *fileEvents
	var lineNumber uint64
	summary := false
	flush := func() error {
		if current == nil {
			return nil
		}
		fe := current
		current = nil
		sort.SliceStable(fe.errors, func(i, j int) bool { return fe.errors[i].Start < fe.errors[j].Start })
		return fn(fe)
	}
	for scanner.Scan() {
		if err := ctx.Err(); err != nil {
			return err
		}
		lineNumber++
		e, err := decodeEvent(scanner.Bytes(), lineNumber)
		if err != nil {
			return err
		}
		if e.Kind == "summary" {
			if summary {
				return fmt.Errorf("%w carries two summaries", errCompilerStream)
			}
			summary = true
			continue
		}
		if summary {
			return fmt.Errorf("%w continues after its summary", errCompilerStream)
		}
		if current == nil || current.path != e.Path {
			if err := flush(); err != nil {
				return err
			}
			current = newFileEvents(e.Path)
		}
		current.add(e)
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("%w: %v", errCompilerStream, err)
	}
	if !summary {
		return fmt.Errorf("%w is truncated: no summary", errCompilerStream)
	}
	return flush()
}

// at finds the binding attributed at exactly this span, preferring the tree
// kind the syntax occurrence describes.
func (fe *fileEvents) at(span ir.Span, mode string) (event, bool) {
	var best event
	score := 0
	for _, v := range fe.bindings[spanKey{int64(span.Start.ByteOffset), int64(span.End.ByteOffset)}] {
		n := 1
		switch mode {
		case "call":
			if v.TreeKind != "METHOD_INVOCATION" && v.TreeKind != "NEW_CLASS" {
				continue
			}
			n = 10
		case "callable_reference":
			if v.TreeKind != "MEMBER_REFERENCE" {
				continue
			}
			n = 10
		case "reference":
			if v.TreeKind != "IDENTIFIER" && v.TreeKind != "MEMBER_SELECT" {
				continue
			}
			n = 10
		case "type":
			switch v.TreeKind {
			case "PARAMETERIZED_TYPE", "ARRAY_TYPE", "UNION_TYPE", "INTERSECTION_TYPE", "UNBOUNDED_WILDCARD", "EXTENDS_WILDCARD", "SUPER_WILDCARD", "PRIMITIVE_TYPE":
				n = 10
			default:
				if v.ElementKind == "CLASS" || v.ElementKind == "INTERFACE" || v.ElementKind == "TYPE_PARAMETER" || v.ElementKind == "ENUM" || v.ElementKind == "ANNOTATION_TYPE" || v.ElementKind == "RECORD" {
					n = 5
				}
			}
		}
		if n > score {
			best, score = v, n
		}
	}
	return best, score > 0
}

// diagnosticAt returns the first compiler error overlapping the span.
func (fe *fileEvents) diagnosticAt(span ir.Span) string {
	if e, ok := fe.errorAt(span); ok {
		return diagnosticReason(e)
	}
	return ""
}

// errorAt returns the first compiler error overlapping the span.
func (fe *fileEvents) errorAt(span ir.Span) (event, bool) {
	end := int64(span.End.ByteOffset)
	i := sort.Search(len(fe.errors), func(i int) bool { return fe.errors[i].Start >= end })
	for _, e := range fe.errors[:i] {
		if e.End > int64(span.Start.ByteOffset) {
			return e, true
		}
	}
	return event{}, false
}

func diagnosticReason(e event) string {
	return truncate(strings.Join(strings.Fields("compiler_error: "+e.Code+": "+e.Message), " "), 500)
}

// undermines reports whether a compiler error makes javac's choice of a
// target unreliable: an ambiguous reference, or several candidates none of
// which applies. Other errors in the same place (a private member used, a
// static context, a type that does not convert) leave the target javac
// attributed the one the code refers to.
func undermines(code string) bool {
	switch code {
	case "compiler.err.ref.ambiguous", "compiler.err.cant.apply.symbols":
		return true
	}
	return false
}

// failureAt returns the reason a binding javac attributed with status
// fails for an error within span: any error when javac attributed nothing,
// only an undermining one when it did.
func (fe *fileEvents) failureAt(span ir.Span, status string) string {
	e, ok := fe.errorAt(span)
	if !ok {
		return ""
	}
	switch status {
	case "resolved", "structural_type", "intrinsic", "array_constructor", "array_clone":
		if !undermines(e.Code) {
			return ""
		}
	}
	return diagnosticReason(e)
}

// declarationFor finds the innermost compiler declaration containing the
// declaration's name span (or its whole span) that describes the same kind
// of element.
func (fe *fileEvents) declarationFor(d *declSummary) (event, bool) {
	start, end := d.Span.Start.ByteOffset, d.Span.End.ByteOffset
	if d.NameSpan != nil {
		start, end = d.NameSpan.Start.ByteOffset, d.NameSpan.End.ByteOffset
	}
	if fe.index == nil {
		fe.index = newDeclarationIndex(fe.declarations)
	}
	return fe.index.innermost(int64(start), int64(end), func(e event) bool { return declarationMatches(d.Kind, d.Name, e) })
}

// declarationIndex answers innermost-containing queries over declaration
// events. Javac declaration spans nest like the tree they come from, so the
// events form a forest ordered by start; a query descends that forest.
type declarationIndex struct {
	events   []event
	order    []int   // event indexes sorted by (start asc, end desc)
	children [][]int // per order position: child positions, ascending start
	roots    []int
}

func newDeclarationIndex(events []event) *declarationIndex {
	x := &declarationIndex{events: events, order: make([]int, len(events)), children: make([][]int, len(events))}
	for i := range events {
		x.order[i] = i
	}
	sort.SliceStable(x.order, func(a, b int) bool {
		ea, eb := events[x.order[a]], events[x.order[b]]
		if ea.Start != eb.Start {
			return ea.Start < eb.Start
		}
		return ea.End > eb.End
	})
	var stack []int
	for pos := range x.order {
		e := events[x.order[pos]]
		for len(stack) > 0 && events[x.order[stack[len(stack)-1]]].End < e.End {
			stack = stack[:len(stack)-1]
		}
		if len(stack) == 0 {
			x.roots = append(x.roots, pos)
		} else {
			parent := stack[len(stack)-1]
			x.children[parent] = append(x.children[parent], pos)
		}
		stack = append(stack, pos)
	}
	return x
}

func (x *declarationIndex) innermost(start, end int64, match func(event) bool) (event, bool) {
	var path []int
	level := x.roots
	for len(level) > 0 {
		i := sort.Search(len(level), func(i int) bool { return x.events[x.order[level[i]]].Start > start })
		if i == 0 {
			break
		}
		pos := level[i-1]
		e := x.events[x.order[pos]]
		if e.Start > start || e.End < end {
			break
		}
		path = append(path, pos)
		level = x.children[pos]
	}
	for i := len(path) - 1; i >= 0; i-- {
		if e := x.events[x.order[path[i]]]; match(e) {
			return e, true
		}
	}
	// Overlapping (non-nested) spans are not on the descent path; a full
	// scan keeps the answer exact for them.
	var best event
	found := false
	for _, e := range x.events {
		if e.Start <= start && e.End >= end && match(e) && (!found || e.End-e.Start < best.End-best.Start) {
			best, found = e, true
		}
	}
	return best, found
}

func declarationMatches(kind ir.DeclarationKind, name string, e event) bool {
	if kind == ir.DeclarationConstructor {
		return e.ElementKind == "CONSTRUCTOR"
	}
	if name != e.Name {
		return false
	}
	switch kind {
	case ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationRecord, ir.DeclarationAnnotationType:
		return e.ElementKind == "CLASS" || e.ElementKind == "INTERFACE" || e.ElementKind == "ENUM" || e.ElementKind == "RECORD" || e.ElementKind == "ANNOTATION_TYPE"
	case ir.DeclarationMethod:
		return e.ElementKind == "METHOD"
	case ir.DeclarationField:
		return e.ElementKind == "FIELD"
	case ir.DeclarationParameter:
		return e.ElementKind == "PARAMETER"
	case ir.DeclarationTypeParameter:
		return e.ElementKind == "TYPE_PARAMETER"
	case ir.DeclarationEnumConstant:
		return e.ElementKind == "ENUM_CONSTANT"
	case ir.DeclarationLocal:
		return e.ElementKind == "LOCAL_VARIABLE" || e.ElementKind == "EXCEPTION_PARAMETER" || e.ElementKind == "RESOURCE_VARIABLE"
	case ir.DeclarationPatternVariable:
		return e.ElementKind == "BINDING_VARIABLE"
	case ir.DeclarationRecordComponent:
		return e.ElementKind == "RECORD_COMPONENT"
	}
	return false
}
