package codesearch

import (
	"ei-aitiger-codegraph/pkg/graph"

	"ei-aitiger-codegraph/pkg/ir"
	"strings"
	"testing"
)

func TestDocumentRetainsCodeAndDocsButIgnoresLocationOnlyChanges(t *testing.T) {
	n := graph.Node{ID: "method", Kind: "method", Name: "streamEvents", QualifiedName: "Agent.streamEvents", Properties: map[string]graph.PropertyValue{"signature": graph.StringValue("Flux<Event> streamEvents(Context context)"), "docstring": graph.StringValue("Streams agent events without blocking."), "source_text": graph.StringValue("return runner.stream(context);"), "file_path": graph.StringValue("Agent.java")}}
	a, ok := FromNode(n)
	if !ok {
		t.Fatal("method not searchable")
	}
	for _, text := range []string{"streamEvents", "Flux<Event>", "without blocking", "runner.stream(context)", "Agent.java"} {
		if !strings.Contains(a.Text, text) {
			t.Fatalf("document lost %q", text)
		}
	}
	n.Source = &graph.SourceAnchor{Lineage: "file:variant", ContentSHA256: strings.Repeat("a", 64), Span: ir.Span{Start: ir.Position{Line: 20}, End: ir.Position{Line: 20}}}
	n.ID = "different-address"
	b, _ := FromNode(n)
	if a.Hash != b.Hash {
		t.Fatal("location or node address invalidated semantic text")
	}
	n.Properties["source_text"] = graph.StringValue("return runner.streamUpdated(context);")
	b, _ = FromNode(n)
	if a.Hash == b.Hash {
		t.Fatal("edited code did not invalidate embedding")
	}
	if _, ok = FromNode(graph.Node{Kind: "resolution"}); ok {
		t.Fatal("internal resolution node should not be embedded")
	}
	// A type's document lists its members, so the type is found for a
	// concept its methods implement; the list is part of the hash.
	c := graph.Node{ID: "class", Kind: "class", Name: "Functions", QualifiedName: "flows.Functions", Properties: map[string]graph.PropertyValue{"docstring": graph.StringValue("Utility class."), "members": graph.StringValue("method handleFunctionCalls\nmethod callTool")}}
	withMembers, _ := FromNode(c)
	if !strings.Contains(withMembers.Text, "Members:\nmethod handleFunctionCalls\nmethod callTool") {
		t.Fatalf("members missing from the document: %q", withMembers.Text)
	}
	delete(c.Properties, "members")
	if without, _ := FromNode(c); without.Hash == withMembers.Hash {
		t.Fatal("members did not change the document hash")
	}
}

func TestSplitIdentifierAndTerms(t *testing.T) {
	cases := map[string][]string{
		"runAsyncImpl": {"run", "Async", "Impl"},
		"HTTPServer2":  {"HTTP", "Server", "2"},
		"max_heap_mib": {"max", "heap", "mib"},
		"LlmAgent":     {"Llm", "Agent"},
		"x":            {"x"},
		"ALLCAPS":      {"ALLCAPS"},
		"getX509Cert":  {"get", "X", "509", "Cert"},
		"":             nil,
	}
	for in, want := range cases {
		if got := SplitIdentifier(in); strings.Join(got, ",") != strings.Join(want, ",") {
			t.Errorf("SplitIdentifier(%q) = %v, want %v", in, got, want)
		}
	}
	terms := IdentifierTerms("runAsyncImpl", "runAsyncImpl(com.google.adk.agents.InvocationContext)")
	for _, want := range []string{"runAsyncImpl", "run", "Async", "Impl", "com", "google", "adk", "agents", "InvocationContext", "Invocation", "Context"} {
		if !strings.Contains(" "+terms+" ", " "+want+" ") {
			t.Errorf("identifier terms lack %q: %s", want, terms)
		}
	}
	if strings.Count(terms, " run ") != 1 {
		t.Errorf("identifier terms must be deduplicated: %s", terms)
	}
	if got := Terms("LlmAgent.runAsync(ctx) OR flow"); strings.Join(got, ",") != "llmagent,runasync,ctx,or,flow" {
		t.Errorf("Terms: %v", got)
	}
}

func TestSnippet(t *testing.T) {
	text := "line one\nprotected Flowable<Event> runAsyncImpl(InvocationContext ctx) {\n    return llmFlow.run(ctx);\n}\n"
	got := Snippet(text, []string{"llmflow"}, 40)
	if !strings.Contains(got, "llmFlow.run") || len(got) > 40 {
		t.Errorf("snippet around the match: %q", got)
	}
	if got = Snippet(text, []string{"absent"}, 20); !strings.HasPrefix(got, "line one") {
		t.Errorf("snippet without a match starts at the top: %q", got)
	}
	if Snippet("", []string{"x"}, 10) != "" || Snippet("abc", nil, 0) != "" {
		t.Error("empty inputs give an empty snippet")
	}
}
