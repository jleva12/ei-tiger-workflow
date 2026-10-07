package buildcontext

import "strconv"

// SyntaxLanguage returns the explicit profile, with compatibility for existing
// Java manifests. Validate rejects conflicting Java versions; this never guesses
// another language's version from a filename or the host environment.
func (s SourceSet) SyntaxLanguage() (language, version string) {
	language, version = s.Language, s.LanguageVersion
	if language == "" {
		language = "java"
	}
	if language == "java" && version == "" {
		version = strconv.Itoa(s.TargetRelease)
	}
	return language, version
}
