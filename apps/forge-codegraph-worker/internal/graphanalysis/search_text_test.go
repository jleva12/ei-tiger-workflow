package graphanalysis

import (
	"strings"
	"testing"
	"unicode/utf8"

	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
)

func TestContainerMembers(t *testing.T) {
	decls := []ir.Declaration{
		{ID: "c", Kind: ir.DeclarationClass, Name: "Functions"},
		{ID: "m1", Kind: ir.DeclarationMethod, Name: "handleFunctionCalls", OwnerID: "c"},
		{ID: "m2", Kind: ir.DeclarationMethod, Name: "callTool", OwnerID: "c"},
		{ID: "f", Kind: ir.DeclarationField, Name: "logger", OwnerID: "c"},
		{ID: "tp", Kind: ir.DeclarationTypeParameter, Name: "T", OwnerID: "c"},
		{ID: "anon", Kind: ir.DeclarationClass, Name: "", OwnerID: "c"},
		{ID: "inner", Kind: ir.DeclarationClass, Name: "Inner", OwnerID: "c"},
		{ID: "im", Kind: ir.DeclarationMethod, Name: "run", OwnerID: "inner"},
		{ID: "local", Kind: ir.DeclarationVariable, Name: "x", OwnerID: "m1"}, // owned by a method, not a type
		{ID: "top", Kind: ir.DeclarationMethod, Name: "free"},
	}
	members := containerMembers(decls)
	if got := members["c"]; got != "method handleFunctionCalls\nmethod callTool\nfield logger\nclass Inner" {
		t.Fatalf("outer members: %q", got)
	}
	if got := members["inner"]; got != "method run" {
		t.Fatalf("inner members: %q", got)
	}
	if _, ok := members["m1"]; ok {
		t.Fatal("a method is not a container")
	}
	// The list is bounded by entries and bytes.
	many := []ir.Declaration{{ID: "c", Kind: ir.DeclarationClass, Name: "Big"}}
	for i := 0; i < maxMemberEntries+50; i++ {
		many = append(many, ir.Declaration{ID: ir.DeclarationID(strings.Repeat("m", 3) + string(rune('a'+i%26)) + strings.Repeat("x", i%7)), Kind: ir.DeclarationMethod, Name: "member" + strings.Repeat("Z", i%40), OwnerID: "c"})
	}
	if got := members["c"]; got == "" {
		t.Fatal("unchanged map")
	}
	bounded := containerMembers(many)["c"]
	if n := strings.Count(bounded, "\n") + 1; n > maxMemberEntries || len(bounded) > maxMemberBytes {
		t.Fatalf("members list not bounded: %d entries, %d bytes", n, len(bounded))
	}
}

// A Java field's signature carries its initializer, which can be a table of
// data far over the graph's property limit (a 125 KB array of user agents);
// documentation can be long too. Both stay valid, bounded properties.
func TestSearchPropertiesBoundSignatureAndDocumentation(t *testing.T) {
	signature := "userAgents = {" + strings.Repeat(`"Mozilla/5.0 (Windows NT 10.0; Win64; x64) é", `, 3000) + "}"
	doc := "/** " + strings.Repeat("ü", 20000) + " */"
	n := graph.Node{ID: graph.ID("field", "userAgents"), Kind: "field", Name: "userAgents"}
	searchProperties(&n, ir.Source{Path: "src/A.java", Language: "java"}, ir.Declaration{SignatureText: signature, DocComment: doc}, nil)
	for key, max := range map[string]int{"signature": maxSignatureBytes, "docstring": maxDocstringBytes} {
		got := *n.Properties[key].String
		if len(got) > max || !utf8.ValidString(got) || !strings.HasSuffix(got, "…") {
			t.Fatalf("%s is %d bytes, valid %v: %.40q…", key, len(got), utf8.ValidString(got), got)
		}
	}
	if err := n.Validate(); err != nil {
		t.Fatal(err)
	}
	if got := summaryText("A=1", maxSignatureBytes); got != "A=1" {
		t.Fatalf("short signature changed: %q", got)
	}
	if got := summaryText("a\x00b\xff", maxSignatureBytes); got != "ab�" {
		t.Fatalf("NUL and invalid UTF-8 kept: %q", got)
	}
}
