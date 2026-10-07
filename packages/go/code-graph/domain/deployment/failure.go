package deployment

import (
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// MaxErrorMessageBytes bounds Run.ErrorMessage and Run.WarningMessage.
const MaxErrorMessageBytes = 2048

var (
	// user:password@ in URLs.
	urlCredentials = regexp.MustCompile(`([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@`)
	// Authorization values and GitHub tokens that reach error text.
	authorizationValue = regexp.MustCompile(`(?i)(authorization:\s*(?:(?:basic|bearer|token)\s+)?)\S+`)
	bearerToken        = regexp.MustCompile(`(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}`)
	githubToken        = regexp.MustCompile(`\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})`)
)

// FailureMessage is the part of a failure's text a run keeps for the people
// who read it. Credentials in URLs, authorization values and token-shaped
// strings are masked, control characters other than newlines and tabs are
// dropped, and the text is cut to MaxErrorMessageBytes on a character
// boundary. The start is kept: a failure's cause comes first. Warnings are
// kept the same way.
func FailureMessage(text string) string {
	text = strings.ToValidUTF8(text, "�")
	text = urlCredentials.ReplaceAllString(text, "${1}***@")
	text = authorizationValue.ReplaceAllString(text, "${1}***")
	text = bearerToken.ReplaceAllString(text, "${1}***")
	text = githubToken.ReplaceAllString(text, "***")
	text = strings.Map(func(r rune) rune {
		if r != '\n' && r != '\t' && unicode.IsControl(r) {
			return -1
		}
		return r
	}, text)
	text = strings.TrimSpace(text)
	if len(text) <= MaxErrorMessageBytes {
		return text
	}
	const more = "…"
	cut := MaxErrorMessageBytes - len(more)
	for cut > 0 && !utf8.RuneStart(text[cut]) {
		cut--
	}
	return text[:cut] + more
}
