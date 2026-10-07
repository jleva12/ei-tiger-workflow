package codesearch

import (
	"reflect"
	"testing"
)

func TestParseQuery(t *testing.T) {
	cases := []struct {
		text     string
		sentence bool
		terms    []string
		symbols  []string
		paths    []string
	}{
		// An identifier keeps its single term and is itself the symbol.
		{"runAsyncImpl", false, []string{"runasyncimpl"}, []string{"runAsyncImpl"}, nil},
		// A partial identifier is a plain term; the substring index handles it.
		{"AsyncImp", false, []string{"asyncimp"}, []string{"AsyncImp"}, nil},
		{"run", false, []string{"run"}, nil, nil},
		// Two words are not a sentence: every term stays, nothing is a symbol.
		{"run ctx", false, []string{"run", "ctx"}, nil, nil},
		// A question drops its function and code words, keeps the symbols it names.
		{"who calls runAsyncImpl in LlmAgent?", true, []string{"runasyncimpl", "llmagent"}, []string{"runAsyncImpl", "LlmAgent"}, nil},
		// Quotes, backticks and call parentheses are stripped from symbols.
		{"Where is `OrderService.placeOrder()` defined", true, []string{"orderservice", "placeorder"}, []string{"OrderService.placeOrder()"}, nil},
		// Sentence openers are not symbols; a capitalised type name is.
		{"Explain how Order validation works", true, []string{"order", "validation", "works"}, []string{"Order"}, nil},
		// Paths are recognised by separator or extension and never become symbols.
		{"what does core/src/main/java/flows/LlmFlow.java export", true, []string{"core", "src", "main", "java", "flows", "llmflow", "export"}, nil, []string{"core/src/main/java/flows/LlmFlow.java"}},
		{"retry policy in LlmFlow.java", true, []string{"retry", "policy", "llmflow", "java"}, nil, []string{"LlmFlow.java"}},
		// A URL is not a path and a version number is not a symbol.
		{"see https://example.com/docs for version 2.0", true, []string{"see", "https", "example", "com", "docs", "version"}, nil, nil},
		// When every word is a stop word the original terms survive.
		{"what is the class", true, []string{"what", "is", "the", "class"}, nil, nil},
		{"   ", false, nil, nil, nil},
	}
	for _, c := range cases {
		got := ParseQuery(c.text)
		if got.Sentence != c.sentence || !reflect.DeepEqual(got.Terms, c.terms) || !reflect.DeepEqual(got.Symbols, c.symbols) || !reflect.DeepEqual(got.Paths, c.paths) {
			t.Errorf("ParseQuery(%q) = %+v, want sentence=%v terms=%v symbols=%v paths=%v", c.text, got, c.sentence, c.terms, c.symbols, c.paths)
		}
	}
}

func TestParseQueryBounds(t *testing.T) {
	text := ""
	for i := 0; i < 20; i++ {
		text += " symbolNumber" + string(rune('A'+i)) + " dir" + string(rune('a'+i)) + "/file.java"
	}
	q := ParseQuery(text)
	// "file" is a stop word; the cap applies after filtering, so 24 real terms remain.
	if len(q.Symbols) != maxQuerySymbols || len(q.Paths) != maxQueryPaths || len(q.Terms) != maxTerms {
		t.Fatalf("bounds: %d symbols, %d paths, %d terms", len(q.Symbols), len(q.Paths), len(q.Terms))
	}
}

func TestSimpleNameAndOwner(t *testing.T) {
	cases := []struct{ symbol, name, owner string }{
		{"runAsyncImpl", "runAsyncImpl", ""},
		{"com.acme.Foo", "Foo", ""},
		{"OrderService.placeOrder()", "placeOrder", "OrderService"},
		{"bar(com.acme.Foo)", "bar", ""},
		{"a.b", "b", ""},
		{"Outer.Inner.run", "run", "Inner"},
	}
	for _, c := range cases {
		if got := SimpleName(c.symbol); got != c.name {
			t.Errorf("SimpleName(%q) = %q, want %q", c.symbol, got, c.name)
		}
		if got := Owner(c.symbol); got != c.owner {
			t.Errorf("Owner(%q) = %q, want %q", c.symbol, got, c.owner)
		}
	}
}
