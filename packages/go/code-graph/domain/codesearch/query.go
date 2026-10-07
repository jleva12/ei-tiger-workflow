package codesearch

import (
	"strings"
	"unicode"
)

// Query is what search understood from free text. An agent sends either an
// identifier, a partial identifier, or a question in natural language that
// names symbols and paths in passing ("who calls runAsyncImpl in LlmAgent",
// "where is the retry policy for core/src/main/java/flows configured").
// Retrieval treats those differently: symbols go to the exact-name tier,
// paths become a proximity hint, and question words never reach the index.
type Query struct {
	Text string `json:"text"`
	// Sentence reports natural language: three or more words.
	Sentence bool `json:"sentence"`
	// Terms are the lexical terms the search index evaluates. Stop words are
	// removed from sentences; an identifier or a two-word query keeps every term.
	Terms []string `json:"terms,omitempty"`
	// Symbols are identifier-shaped words as written: camelCase, dotted,
	// snake_case or followed by parentheses. Each is looked up by exact name.
	Symbols []string `json:"symbols,omitempty"`
	// Paths are words that look like file paths; hits under them rank higher.
	Paths []string `json:"paths,omitempty"`
}

const (
	maxQuerySymbols = 8
	maxQueryPaths   = 4
)

// stopWords are English function and question words plus the generic code
// vocabulary an agent uses to describe what it wants. They carry no signal
// against a code document, and the kind words ("class", "method") appear in
// every document header, so leaving them in only adds noise to SCORE.
var stopWords = map[string]bool{}

func init() {
	for _, w := range strings.Fields(`
a about above after all also am an and any are as at be been before being below between
both but by can could did do does doing done down during each either every few for from
further had has have having he her here hers him his how i if in into is it its itself
just let may me might more most must my myself no nor not of off on once only or other
our ours out over own please same shall she should show so some such tell than that the
their theirs them then there these they this those through to too under until up us very
was we were what when where which while who whom whose why will with would you your yours
code codebase source file files folder directory project repository repo class classes
interface interfaces enum enums record records constructor constructors method methods
function functions field fields type types module modules package packages definition
definitions define defined defines declaration declarations declared implementation
implementations implement implemented implementing implements use used uses using usage
call calls called caller callers callee callees invoke invokes invoked reference references
referenced referencing find list explain describe give want need looking look
`) {
		stopWords[w] = true
	}
}

// ParseQuery classifies free text for retrieval. It is deterministic and
// never consults the graph: whether a symbol exists is the exact tier's job.
func ParseQuery(text string) Query {
	q := Query{Text: strings.TrimSpace(text)}
	words := strings.Fields(q.Text)
	q.Sentence = len(words) >= 3
	seenSymbol, seenPath := map[string]bool{}, map[string]bool{}
	for _, raw := range words {
		word := trimWord(raw)
		if word == "" {
			continue
		}
		switch {
		case pathLike(word):
			if !seenPath[word] && len(q.Paths) < maxQueryPaths {
				seenPath[word] = true
				q.Paths = append(q.Paths, word)
			}
		case symbolLike(word):
			if !seenSymbol[word] && len(q.Symbols) < maxQuerySymbols {
				seenSymbol[word] = true
				q.Symbols = append(q.Symbols, word)
			}
		}
	}
	if !q.Sentence {
		q.Terms = Terms(q.Text)
		return q
	}
	q.Terms = collectTerms(q.Text, func(term string) bool { return !stopWords[term] })
	if len(q.Terms) == 0 {
		q.Terms = Terms(q.Text)
	}
	return q
}

// SimpleName is the last dotted segment of a symbol before any parameter
// list: "com.acme.Foo", "Foo.bar()" and "bar(com.acme.Foo)" give Foo, bar
// and bar.
func SimpleName(symbol string) string {
	if i := strings.IndexByte(symbol, '('); i >= 0 {
		symbol = symbol[:i]
	}
	if i := strings.LastIndexByte(symbol, '.'); i >= 0 {
		symbol = symbol[i+1:]
	}
	return symbol
}

// Owner is the dotted segment before the simple name when the symbol spells
// a member of a type ("OrderService.placeOrder" gives OrderService), empty
// otherwise. Lower-case segments are package names, not owners.
func Owner(symbol string) string {
	if i := strings.IndexByte(symbol, '('); i >= 0 {
		symbol = symbol[:i]
	}
	segments := strings.Split(symbol, ".")
	if len(segments) < 2 {
		return ""
	}
	owner := segments[len(segments)-2]
	if owner == "" {
		return ""
	}
	if r := []rune(owner)[0]; !unicode.IsUpper(r) {
		return ""
	}
	return owner
}

// trimWord strips the punctuation a sentence wraps around a symbol or path:
// quotes, backticks, brackets and trailing clause punctuation. Parentheses
// that belong to the symbol ("save()") survive.
func trimWord(word string) string {
	word = strings.TrimFunc(word, func(r rune) bool {
		switch r {
		case '"', '\'', '`', ',', ';', ':', '?', '!', '[', ']', '{', '}', '<', '>':
			return true
		}
		return false
	})
	word = strings.TrimRight(word, ".")
	if strings.HasPrefix(word, "(") && !strings.Contains(word[1:], "(") {
		word = strings.TrimSuffix(strings.TrimPrefix(word, "("), ")")
	}
	return word
}

var sourceExtensions = []string{".java", ".kt", ".scala", ".groovy", ".go", ".py", ".ts", ".tsx", ".js", ".jsx", ".cs", ".rb", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".xml", ".yaml", ".yml", ".json", ".toml", ".properties", ".gradle", ".sql", ".md", ".proto"}

// pathLike reports a word that names a file or directory: it contains a path
// separator that is not part of a URL, or ends with a source file extension.
func pathLike(word string) bool {
	if strings.Contains(word, "://") {
		return false
	}
	if strings.Contains(word, "/") {
		return len(strings.Trim(word, "/")) > 0
	}
	lower := strings.ToLower(word)
	for _, ext := range sourceExtensions {
		if strings.HasSuffix(lower, ext) && len(lower) > len(ext) {
			return true
		}
	}
	return false
}

// symbolLike reports a word that spells a symbol rather than describing one:
// camelCase, a dotted or qualified name, snake_case, or a call with
// parentheses. Plain words, numbers and all-capital abbreviations are not
// symbols; the exact tier still tries plain words as names.
func symbolLike(word string) bool {
	if len(word) < 2 {
		return false
	}
	letters := 0
	for _, r := range word {
		if unicode.IsLetter(r) {
			letters++
		} else if !unicode.IsNumber(r) && !strings.ContainsRune("._$():<>[]", r) {
			return false
		}
	}
	if letters == 0 {
		return false
	}
	if strings.ContainsAny(word, "._$(") {
		return strings.Trim(word, "._$()") != ""
	}
	lower, upper, innerUpper := false, false, false
	for i, r := range word {
		lower = lower || unicode.IsLower(r)
		upper = upper || unicode.IsUpper(r)
		innerUpper = innerUpper || (i > 0 && unicode.IsUpper(r))
	}
	if !(lower && upper) {
		return false
	}
	// A word capitalised only at its start is a type name ("Order") unless it
	// is a sentence opener such as "Where" or "Explain".
	return innerUpper || !stopWords[strings.ToLower(word)]
}
