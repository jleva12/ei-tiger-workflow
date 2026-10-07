package python

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"slices"
	"testing"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
)

const sample = `from .store import Store as Storage
import os.path as paths

@register("save")
async def save(store: Storage, /, value: str = "", *args, flag: bool = True, **kwargs) -> str:
    """Save a value."""
    response = await store.save(value, *args, flag=flag, **kwargs)
    values = [factory(x).name for x in args if allowed(x)]
    callback = lambda item: factory(item)
    return response

class Child(Storage):
    def run(self):
        return super().save("x")
`

func TestPythonExtraction(t *testing.T) {
	f := parseTest(t, newTestParser(t), inputFor("app.py", sample))
	if f.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("coverage: %+v", f.Coverage.Issues)
	}
	d := one(t, f, ir.DeclarationFunction, "save")
	if d.DocComment == "" || len(d.DecoratorExpressionIDs) != 1 || len(d.Callable.ParameterIDs) != 5 {
		t.Fatalf("function: %+v", d)
	}
	if len(f.Imports) != 2 || f.Imports[0].RelativeLevel != 1 || f.Imports[0].BindingName != "Storage" || f.Imports[1].Module != "os.path" {
		t.Fatalf("imports: %+v", f.Imports)
	}
	if len(f.Lambdas) != 1 {
		t.Fatal("lambda missing")
	}
	var expanded bool
	for _, c := range f.Calls {
		if c.Kind != ir.CallInvocation || c.CalleeID == "" {
			t.Fatalf("callee lost: %+v", c)
		}
		if c.Name == "save" && len(c.Arguments) == 4 {
			expanded = c.Arguments[1].Expansion == "iterable" && c.Arguments[2].Keyword == "flag" && c.Arguments[3].Expansion == "mapping"
		}
	}
	if !expanded {
		t.Fatal("call argument forms lost")
	}
	if one(t, f, ir.DeclarationParameter, "store").Variable.ParameterKind != "positional_only" {
		t.Fatal("positional-only parameter lost")
	}
	if one(t, f, ir.DeclarationParameter, "flag").Variable.ParameterKind != "keyword_only" {
		t.Fatal("keyword-only parameter lost")
	}
	for _, s := range f.Scopes {
		if s.Kind == ir.ScopeLoop || s.Kind == ir.ScopeBlock {
			t.Fatal("invented Python block scope")
		}
	}
}

func TestPythonPositionsLimitsRecoveryAndReuse(t *testing.T) {
	p := newTestParser(t)
	in := inputFor("app.py", "\ufeffmessage = '😀'\r\nprint(message)\r\n")
	f := parseTest(t, p, in)
	data, _ := json.Marshal(f)
	again, _ := json.Marshal(parseTest(t, p, in))
	if !bytes.Equal(data, again) {
		t.Fatal("nondeterministic parser")
	}
	for _, c := range f.Calls {
		if string(in.Content[c.Occurrence.Span.Start.ByteOffset:c.Occurrence.Span.End.ByteOffset]) != "print(message)" {
			t.Fatal("incorrect UTF-8 byte anchor")
		}
	}
	limited := in
	limited.Limits.MaxIRRecords = 1
	if _, err := p.Parse(context.Background(), limited); !errors.Is(err, parser.ErrLimitExceeded) {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.Parse(ctx, in); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	parseTest(t, p, in)
	bad := parseTest(t, p, inputFor("bad.py", "def broken(:\n    foo(\n"))
	if bad.Coverage.Status == ir.ExtractionComplete {
		t.Fatal("syntax errors hidden")
	}
}

func TestPythonProfiles(t *testing.T) {
	for _, version := range []string{"3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"} {
		if err := Registration().Validate(parser.Profile{Language: "python", Version: version}, parser.DefaultLimits()); err != nil {
			t.Fatal(err)
		}
	}
	for _, version := range []string{"2.7", "3.09", "latest"} {
		if err := Registration().Validate(parser.Profile{Language: "python", Version: version}, parser.DefaultLimits()); !errors.Is(err, parser.ErrUnsupportedConfig) {
			t.Fatal(version, err)
		}
	}
}

func TestPythonStructuralFormsAndQualifiedHeritage(t *testing.T) {
	f := parseTest(t, newTestParser(t), inputFor("forms.py", `import base
class Child(base.Parent):
    def read(self, items):
        with open("input") as stream:
            label = f"{stream!r}"
            values = list(item for item in items if item)
            match values:
                case [first, *rest]:
                    return first
                case {"name": name}:
                    return name
`))
	if f.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("coverage: %+v", f.Coverage)
	}
	for _, name := range []string{"stream", "item"} {
		one(t, f, ir.DeclarationLocal, name)
	}
	for _, name := range []string{"first", "rest", "name"} {
		one(t, f, ir.DeclarationPatternVariable, name)
	}
	child := one(t, f, ir.DeclarationClass, "Child")
	var heritage bool
	for _, u := range f.TypeUses {
		if u.Role == ir.TypeUseHeritage {
			heritage = true
			if u.Occurrence.EnclosingDeclarationID != child.ID {
				t.Fatalf("heritage occurrence owned by %q, want the class %q", u.Occurrence.EnclosingDeclarationID, child.ID)
			}
		}
	}
	if !heritage {
		t.Fatal("qualified base type lost")
	}
	for _, c := range f.Calls {
		if c.Name == "list" && len(c.Arguments) != 1 {
			t.Fatal("generator arguments flattened")
		}
	}
}

// Literal's arguments and Annotated's metadata are values: a string there is
// no forward reference, a call no type; Annotated's first argument and a
// quoted name elsewhere are types.
func TestPythonLiteralAndAnnotatedArgumentsAreValues(t *testing.T) {
	src := "import typing\nfrom typing import Annotated, Literal\nclass Color:\n    RED = 1\ndef get_db(): pass\nx: Literal[\"a\", \"b\"] = \"a\"\ny: typing.Literal['c', Color.RED]\nz: Annotated[int, \"Color\", get_db()]\nw: list[\"Color\"]\n"
	file := parseTest(t, newTestParser(t), inputFor("a.py", src))
	var uses, refs []string
	for _, u := range file.TypeUses {
		s := u.Occurrence.Span
		uses = append(uses, src[s.Start.ByteOffset:s.End.ByteOffset])
	}
	for _, r := range file.References {
		s := r.Occurrence.Span
		refs = append(refs, src[s.Start.ByteOffset:s.End.ByteOffset])
	}
	if want := []string{"Literal", "Literal", "Annotated", "int", "list", "Color"}; !slices.Equal(uses, want) {
		t.Fatalf("type uses = %q, want %q", uses, want)
	}
	red := 0
	for _, r := range refs {
		if r == "RED" {
			red++
		}
	}
	if !slices.Contains(refs, "get_db") || red != 2 {
		t.Fatalf("references = %q", refs)
	}
	if file.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("coverage = %+v", file.Coverage)
	}
}
