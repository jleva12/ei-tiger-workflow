package parser_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"testing"
	"unicode/utf8"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
)

func inputFor(content []byte) parser.Input {
	digest := sha256.Sum256(content)
	return parser.Input{
		Source: ir.Source{
			FileID: "file:demo", RepositoryID: "repository", SnapshotID: "snapshot",
			Path: "src/Demo.java", ContentSHA256: hex.EncodeToString(digest[:]),
			SizeBytes: uint64(len(content)), Language: "java", LanguageVersion: "21",
			BuildContextID: "build", ModuleID: "module", SourceSetID: "main", ArtifactID: "artifact",
		},
		Content: content,
		Limits:  parser.DefaultLimits(),
	}
}

func TestValidatePreservesExactOriginalBytes(t *testing.T) {
	for name, content := range map[string][]byte{
		"nil_empty_file": nil,
		"empty_file":     {},
		"ascii":          []byte("class Demo {}\n"),
		"multibyte_crlf": []byte("// π\r\nclass Demo {}\r\n"),
		"bom_and_escape": []byte("\xef\xbb\xbf// \\u03c0\nclass Demo {}\n"),
	} {
		t.Run(name, func(t *testing.T) {
			input := inputFor(content)
			before := append([]byte(nil), content...)
			// Hex casing is irrelevant to digest identity and must not cause
			// valid existing IR source metadata to be silently rewritten.
			input.Source.ContentSHA256 = strings.ToUpper(input.Source.ContentSHA256)
			source := input.Source
			if err := input.Validate(); err != nil {
				t.Fatal(err)
			}
			if input.Source != source || !bytes.Equal(input.Content, before) {
				t.Fatal("preflight rewrote source bytes or identity")
			}
		})
	}
}

func TestValidateRejectsMismatchedOrIncompleteInput(t *testing.T) {
	tests := []struct {
		name   string
		change func(*parser.Input)
	}{
		{"file_id", func(i *parser.Input) { i.Source.FileID = "" }},
		{"repository", func(i *parser.Input) { i.Source.RepositoryID = "" }},
		{"snapshot", func(i *parser.Input) { i.Source.SnapshotID = "" }},
		{"absolute_path", func(i *parser.Input) { i.Source.Path = "/src/Demo.java" }},
		{"path_escape", func(i *parser.Input) { i.Source.Path = "../Demo.java" }},
		{"unclean_path", func(i *parser.Input) { i.Source.Path = "src/../Demo.java" }},
		{"platform_path", func(i *parser.Input) { i.Source.Path = `src\Demo.java` }},
		{"language", func(i *parser.Input) { i.Source.Language = "" }},
		{"implicit_version", func(i *parser.Input) { i.Source.LanguageVersion = "" }},
		{"blank_version", func(i *parser.Input) { i.Source.LanguageVersion = "  " }},
		{"invalid_metadata_utf8", func(i *parser.Input) { i.Source.BuildContextID = "\xff" }},
		{"wrong_size", func(i *parser.Input) { i.Source.SizeBytes++ }},
		{"malformed_hash", func(i *parser.Input) { i.Source.ContentSHA256 = "sha256" }},
		{"wrong_hash", func(i *parser.Input) { i.Source.ContentSHA256 = strings.Repeat("0", 64) }},
		{"changed_bytes_same_size", func(i *parser.Input) { i.Content[0] = 'C' }},
		{"utf16_bom", func(i *parser.Input) { *i = inputFor([]byte("\xff\xfec\x00l\x00a\x00s\x00s\x00")) }},
		{"utf16_without_bom", func(i *parser.Input) { *i = inputFor([]byte("c\x00l\x00a\x00s\x00s\x00 \x00D\x00e\x00m\x00o\x00")) }},
		{"binary", func(i *parser.Input) {
			b := make([]byte, 4096)
			for k := range b {
				b[k] = byte(k*7919 + k/3)
			}
			*i = inputFor(b)
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			input := inputFor([]byte("class Demo {}"))
			test.change(&input)
			if err := input.Validate(); !errors.Is(err, parser.ErrInvalidInput) {
				t.Fatalf("got %v, want ErrInvalidInput", err)
			}
		})
	}
}

func TestSourceBudgetAndRequiredLimits(t *testing.T) {
	input := inputFor([]byte("// π\r\n"))
	input.Limits.MaxSourceBytes = uint64(len(input.Content))
	if err := input.Validate(); err != nil {
		t.Fatalf("exact byte limit rejected: %v", err)
	}
	input.Limits.MaxSourceBytes--
	if err := input.Validate(); !errors.Is(err, parser.ErrLimitExceeded) {
		t.Fatalf("got %v, want byte limit error", err)
	}
	for _, clear := range []func(*parser.Limits){
		func(l *parser.Limits) { l.MaxSourceBytes = 0 },
		func(l *parser.Limits) { l.MaxSyntaxNodes = 0 },
		func(l *parser.Limits) { l.MaxSyntaxDepth = 0 },
		func(l *parser.Limits) { l.MaxIRRecords = 0 },
		func(l *parser.Limits) { l.MaxDiagnostics = 0 },
		func(l *parser.Limits) { l.MaxOutputBytes = 0 },
	} {
		limits := parser.DefaultLimits()
		clear(&limits)
		if err := limits.Validate(); !errors.Is(err, parser.ErrInvalidInput) {
			t.Fatalf("zero must not silently remove a limit: %v", err)
		}
	}
}

func TestWrappedSourceValidationRemainsInspectable(t *testing.T) {
	input := inputFor(nil)
	input.Source.FileID = ""
	err := input.Validate()
	var detail ir.ValidationError
	if !errors.Is(err, parser.ErrInvalidInput) || !errors.As(err, &detail) || detail.Path != "source.file_id" {
		t.Fatalf("lost source error identity/path: %v", err)
	}
}

func TestLanguageSupportBelongsToAdapter(t *testing.T) {
	input := inputFor(nil)
	input.Source.Language, input.Source.LanguageVersion = "another-language", "future-version"
	input.Options.EnablePreview = true
	if err := input.Validate(); err != nil {
		t.Fatalf("portable preflight must not select a language engine: %v", err)
	}
}

// Source in a single-byte encoding is read, not refused: every byte that is
// not part of a UTF-8 character reads as '?', one for one, so each offset
// still addresses the original bytes.
func TestTextReadsSingleByteEncodingsInPlace(t *testing.T) {
	latin1 := []byte("// caf\xe9 cr\xe8me\nclass Demo { String s = \"\xa9\"; }\n")
	input := inputFor(latin1)
	if err := input.Validate(); err != nil {
		t.Fatalf("Latin-1 source refused: %v", err)
	}
	text := input.Text()
	if len(text) != len(latin1) || !utf8.Valid(text) || string(text) != "// caf? cr?me\nclass Demo { String s = \"?\"; }\n" {
		t.Fatalf("text = %q", text)
	}
	// Mostly-accented Latin-1 prose in comments is still text.
	prose := inputFor([]byte("// " + strings.Repeat("caf\xe9 cr\xe8me br\xfbl\xe9e ", 40) + "\nclass Demo {}\n"))
	if err := prose.Validate(); err != nil {
		t.Fatalf("Latin-1 comments refused: %v", err)
	}
	utf := inputFor([]byte("// π\n"))
	if got := utf.Text(); &got[0] != &utf.Content[0] {
		t.Fatal("UTF-8 content is read as is")
	}
}
