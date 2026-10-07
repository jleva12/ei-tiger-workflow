package discover

import (
	"fmt"
	"strings"
	"unicode"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

type token struct {
	text   string
	quoted bool
}
type statement struct {
	head []token
	body []statement
}

// Tokenize strings/comments before recognizing DSL constructs. In particular,
// an apparent Java version inside a comment, string, or conditional block must
// not become the compilation profile. This is a declaration reader, not Groovy
// or Kotlin execution; unsupported syntax remains covered by explicit gaps.
func (s *scan) gradleStatements(file string) ([]statement, error) {
	data, err := s.read(file)
	if err != nil {
		return nil, err
	}
	src := string(data)
	var tokens []token
	for i := 0; i < len(src); {
		if err := s.ctx.Err(); err != nil {
			return nil, err
		}
		if uint64(len(tokens)) >= s.r.Limits.MaxRecords {
			return nil, bc.ErrLimitExceeded
		}
		c := src[i]
		if c == '\n' || c == ';' {
			tokens = append(tokens, token{text: "\n"})
			i++
			continue
		}
		if c == ' ' || c == '\t' || c == '\r' {
			i++
			continue
		}
		if strings.HasPrefix(src[i:], "//") {
			for i < len(src) && src[i] != '\n' {
				i++
			}
			continue
		}
		if strings.HasPrefix(src[i:], "/*") {
			i += 2
			depth := 1
			for i < len(src) && depth > 0 {
				if strings.HasPrefix(src[i:], "/*") {
					depth++
					i += 2
				} else if strings.HasPrefix(src[i:], "*/") {
					depth--
					i += 2
				} else {
					i++
				}
			}
			if depth != 0 {
				return nil, fmt.Errorf("%w: unterminated build comment in %s", bc.ErrInvalidInput, file)
			}
			continue
		}
		if c == '\'' || c == '"' || c == '`' {
			delim := string(c)
			if c != '`' && strings.HasPrefix(src[i:], strings.Repeat(delim, 3)) {
				delim = strings.Repeat(delim, 3)
			}
			i += len(delim)
			var b strings.Builder
			closed := false
			for i < len(src) {
				if strings.HasPrefix(src[i:], delim) {
					i += len(delim)
					closed = true
					break
				}
				if src[i] == '\\' && len(delim) == 1 && c != '`' {
					i++
					if i == len(src) {
						break
					}
					switch src[i] {
					case 'n':
						b.WriteByte('\n')
					case 'r':
						b.WriteByte('\r')
					case 't':
						b.WriteByte('\t')
					case '\\', '\'', '"':
						b.WriteByte(src[i])
					default:
						b.WriteByte('\\')
						b.WriteByte(src[i])
					}
					i++
					continue
				}
				b.WriteByte(src[i])
				i++
			}
			if !closed {
				return nil, fmt.Errorf("%w: unterminated build string in %s", bc.ErrInvalidInput, file)
			}
			tokens = append(tokens, token{text: b.String(), quoted: c != '`'})
			continue
		}
		if c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '$' {
			start := i
			i++
			for i < len(src) {
				c = src[i]
				if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '$') {
					break
				}
				i++
			}
			tokens = append(tokens, token{text: src[start:i]})
			continue
		}
		tokens = append(tokens, token{text: string(c)})
		i++
	}
	i := 0
	var block func(uint32, bool) ([]statement, error)
	block = func(depth uint32, nested bool) ([]statement, error) {
		if depth > s.r.Limits.MaxDepth {
			return nil, bc.ErrLimitExceeded
		}
		var out []statement
		current := statement{}
		var delimiters []string
		flush := func() {
			if len(current.head) > 0 || current.body != nil {
				out = append(out, current)
				current = statement{}
			}
		}
		for i < len(tokens) {
			t := tokens[i]
			i++
			if t.quoted {
				current.head = append(current.head, t)
				continue
			}
			switch t.text {
			case "(", "[":
				delimiters = append(delimiters, t.text)
				if uint32(len(delimiters))+depth > s.r.Limits.MaxDepth {
					return nil, bc.ErrLimitExceeded
				}
				current.head = append(current.head, t)
			case ")", "]":
				want := "("
				if t.text == "]" {
					want = "["
				}
				if len(delimiters) == 0 || delimiters[len(delimiters)-1] != want {
					return nil, fmt.Errorf("%w: unbalanced Gradle delimiters in %s", bc.ErrInvalidInput, file)
				}
				delimiters = delimiters[:len(delimiters)-1]
				current.head = append(current.head, t)
			case "{":
				body, err := block(depth+1, true)
				if err != nil {
					return nil, err
				}
				current.body = body
				flush()
			case "}":
				if !nested || len(delimiters) != 0 {
					return nil, fmt.Errorf("%w: unbalanced Gradle block in %s", bc.ErrInvalidInput, file)
				}
				flush()
				return out, nil
			case "\n":
				if len(delimiters) == 0 {
					flush()
				}
			default:
				current.head = append(current.head, t)
			}
		}
		if nested || len(delimiters) != 0 {
			return nil, fmt.Errorf("%w: incomplete Gradle block in %s", bc.ErrInvalidInput, file)
		}
		flush()
		return out, nil
	}
	return block(0, false)
}

func compact(ts []token) string {
	var b strings.Builder
	for _, t := range ts {
		b.WriteString(t.text)
	}
	return b.String()
}
func first(st statement) string {
	if len(st.head) > 0 {
		return st.head[0].text
	}
	return ""
}
func stringsIn(ts []token) []string {
	var out []string
	for _, t := range ts {
		if t.quoted {
			out = append(out, t.text)
		}
	}
	return out
}

// literalArgs accepts only a literal argument/list sequence, never expressions,
// identifiers, interpolated method calls or string concatenation.
func literalArgs(ts []token) ([]string, bool) {
	var out []string
	for _, t := range ts {
		if t.quoted {
			out = append(out, t.text)
			continue
		}
		switch t.text {
		case "(", ")", "[", "]", ",", "=", "\n":
		default:
			return nil, false
		}
	}
	return out, len(out) > 0
}

func (s *scan) properties(file string) (map[string]string, error) {
	props := map[string]string{}
	if !s.markers[file] {
		return props, nil
	}
	data, err := s.read(file)
	if err != nil {
		return nil, err
	}
	// Java properties continuation/unicode escapes need a full Java-properties
	// interpretation; preserve those as unresolved instead of changing bytes.
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "!") {
			continue
		}
		if strings.Contains(line, "\\") {
			continue
		}
		i := strings.IndexFunc(line, func(r rune) bool { return r == '=' || r == ':' || unicode.IsSpace(r) })
		if i < 0 {
			continue
		}
		key := strings.TrimSpace(line[:i])
		value := strings.TrimSpace(strings.TrimLeft(line[i:], " \t=:"))
		props[key] = value
	}
	return props, nil
}

func gradleValue(raw string, props map[string]string) string {
	// Normalize Groovy/Kotlin's $name interpolation to Maven-style expansion.
	var b strings.Builder
	for i := 0; i < len(raw); {
		if raw[i] == '$' && i+1 < len(raw) && raw[i+1] != '{' {
			j := i + 1
			for j < len(raw) && (raw[j] >= 'a' && raw[j] <= 'z' || raw[j] >= 'A' && raw[j] <= 'Z' || raw[j] >= '0' && raw[j] <= '9' || raw[j] == '_') {
				j++
			}
			if j > i+1 {
				b.WriteString("${" + raw[i+1:j] + "}")
				i = j
				continue
			}
		}
		b.WriteByte(raw[i])
		i++
	}
	return interpolate(b.String(), props)
}
