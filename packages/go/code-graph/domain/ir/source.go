package ir

import (
	"encoding/hex"
	"errors"
	"path"
	"strings"
	"unicode/utf8"
)

// Validate checks source identity and metadata without reading source bytes.
// It does not verify the content digest/size or interpret language versions.
func (source Source) Validate() error {
	var problems []error
	for _, field := range []struct {
		name, value string
		required    bool
	}{
		{"file_id", string(source.FileID), true},
		{"repository_id", source.RepositoryID, true},
		{"snapshot_id", source.SnapshotID, true},
		{"path", source.Path, true},
		{"language", source.Language, true},
		{"language_version", source.LanguageVersion, false},
		{"build_context_id", source.BuildContextID, false},
		{"module_id", source.ModuleID, false},
		{"source_set_id", source.SourceSetID, false},
		{"artifact_id", source.ArtifactID, false},
	} {
		if field.required && strings.TrimSpace(field.value) == "" {
			problems = append(problems, ValidationError{Path: "source." + field.name, Message: "must not be empty"})
		}
		if !utf8.ValidString(field.value) {
			problems = append(problems, ValidationError{Path: "source." + field.name, Message: "must be valid UTF-8"})
		}
	}
	p := source.Path
	if p == "" || p == "." || path.IsAbs(p) || strings.ContainsAny(p, "\\:\x00") || path.Clean(p) != p || p == ".." || strings.HasPrefix(p, "../") {
		problems = append(problems, ValidationError{Path: "source.path", Message: "must be a normalized repository-relative slash path"})
	}
	hash, err := hex.DecodeString(source.ContentSHA256)
	if err != nil || len(hash) != 32 {
		problems = append(problems, ValidationError{Path: "source.content_sha256", Message: "must contain a SHA-256 hex digest"})
	}
	return errors.Join(problems...)
}
