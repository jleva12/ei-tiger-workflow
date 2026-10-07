// Package jsonc reads JSON with comments, the format of tsconfig.json,
// jsconfig.json and pyrightconfig.json.
package jsonc

// Strip removes line and block comments and trailing commas from JSON with
// comments, leaving string contents untouched.
func Strip(data []byte) []byte {
	out := make([]byte, 0, len(data))
	inString := false
	for i := 0; i < len(data); i++ {
		c := data[i]
		switch {
		case inString:
			out = append(out, c)
			if c == '\\' && i+1 < len(data) {
				i++
				out = append(out, data[i])
			} else if c == '"' {
				inString = false
			}
		case c == '"':
			inString = true
			out = append(out, c)
		case c == '/' && i+1 < len(data) && data[i+1] == '/':
			for i < len(data) && data[i] != '\n' {
				i++
			}
			if i < len(data) {
				out = append(out, '\n')
			}
		case c == '/' && i+1 < len(data) && data[i+1] == '*':
			i += 2
			for i+1 < len(data) && !(data[i] == '*' && data[i+1] == '/') {
				i++
			}
			i++
		default:
			out = append(out, c)
		}
	}
	// Trailing commas: a comma followed only by whitespace and a closer.
	cleaned := make([]byte, 0, len(out))
	inString = false
	for i := 0; i < len(out); i++ {
		c := out[i]
		if inString {
			cleaned = append(cleaned, c)
			if c == '\\' && i+1 < len(out) {
				i++
				cleaned = append(cleaned, out[i])
			} else if c == '"' {
				inString = false
			}
			continue
		}
		if c == '"' {
			inString = true
		} else if c == ',' {
			j := i + 1
			for j < len(out) && (out[j] == ' ' || out[j] == '\t' || out[j] == '\n' || out[j] == '\r') {
				j++
			}
			if j < len(out) && (out[j] == '}' || out[j] == ']') {
				continue
			}
		}
		cleaned = append(cleaned, c)
	}
	return cleaned
}
