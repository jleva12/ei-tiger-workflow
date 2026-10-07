package buildcontext

import (
	"path"
	"strings"
)

// Source patterns are portable, source-root-relative globs. ** as a complete
// path segment matches zero or more directories; ordinary segments use the
// standard *, ?, and character-class glob syntax. Exclusions take precedence.
func ValidSourcePattern(pattern string) bool {
	if pattern == "" || len(pattern) > 4096 || strings.HasPrefix(pattern, "/") || strings.Contains(pattern, "\\") || strings.ContainsRune(pattern, 0) {
		return false
	}
	for _, part := range strings.Split(pattern, "/") {
		if part == "" || part == "." || part == ".." {
			return false
		}
		if part != "**" {
			if _, err := path.Match(part, ""); err != nil {
				return false
			}
		}
	}
	return true
}
func MatchSourcePattern(pattern, name string) bool {
	if !ValidSourcePattern(pattern) || !ValidPath(name) {
		return false
	}
	p := strings.Split(pattern, "/")
	n := strings.Split(name, "/")
	// Dynamic programming avoids exponential recursion with repeated ** patterns.
	previous := make([]bool, len(n)+1)
	previous[0] = true
	for _, part := range p {
		next := make([]bool, len(n)+1)
		if part == "**" {
			next[0] = previous[0]
			for i := 1; i <= len(n); i++ {
				next[i] = previous[i] || next[i-1]
			}
		} else {
			for i := 1; i <= len(n); i++ {
				match, _ := path.Match(part, n[i-1])
				next[i] = previous[i-1] && match
			}
		}
		previous = next
	}
	return previous[len(n)]
}
func (s SourceSet) SelectsSource(relativePath string) bool {
	included := len(s.IncludePatterns) == 0
	for _, pattern := range s.IncludePatterns {
		if MatchSourcePattern(pattern, relativePath) {
			included = true
			break
		}
	}
	if !included {
		return false
	}
	for _, pattern := range s.ExcludePatterns {
		if MatchSourcePattern(pattern, relativePath) {
			return false
		}
	}
	return true
}
