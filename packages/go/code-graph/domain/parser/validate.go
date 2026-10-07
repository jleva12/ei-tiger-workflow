package parser

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
	"unicode/utf8"
)

func (limits Limits) Validate() error {
	if limits.MaxSourceBytes == 0 || limits.MaxSyntaxNodes == 0 || limits.MaxSyntaxDepth == 0 ||
		limits.MaxIRRecords == 0 || limits.MaxDiagnostics == 0 || limits.MaxOutputBytes == 0 {
		return fmt.Errorf("%w: all limits must be positive", ErrInvalidInput)
	}
	return nil
}

// Validate checks portable request shape and the exact size/digest of Content.
// It never normalizes bytes or fills in missing metadata. Content size is
// checked before scanning/hashing. Adapters check ctx before and after this
// bounded preflight, then check language/version/options and engine budgets.
// Successful validation does not imply that a language/configuration is
// supported, nor enforce syntax/output limits for an engine that has not run.
func (input Input) Validate() error {
	if err := input.Limits.Validate(); err != nil {
		return err
	}
	if uint64(len(input.Content)) > input.Limits.MaxSourceBytes {
		return fmt.Errorf("%w: source byte limit", ErrLimitExceeded)
	}
	if err := input.Source.Validate(); err != nil {
		return fmt.Errorf("%w: %w", ErrInvalidInput, err)
	}
	if strings.TrimSpace(input.Source.LanguageVersion) == "" {
		return fmt.Errorf("%w: source.language_version must be explicit", ErrInvalidInput)
	}
	if input.Source.SizeBytes != uint64(len(input.Content)) {
		return fmt.Errorf("%w: source.size_bytes does not match content", ErrInvalidInput)
	}
	if wide(input.Content) {
		return fmt.Errorf("%w: content must be UTF-8, not UTF-16 or UTF-32", ErrInvalidInput)
	}
	if binary(input.Content) {
		return fmt.Errorf("%w: content is binary, not text", ErrInvalidInput)
	}
	expected, _ := hex.DecodeString(input.Source.ContentSHA256) // Source.Validate checked its shape.
	actual := sha256.Sum256(input.Content)
	if !bytes.Equal(expected, actual[:]) {
		return fmt.Errorf("%w: source.content_sha256 does not match content", ErrInvalidInput)
	}
	return nil
}

// wide reports text in a UTF-16 or UTF-32 encoding: a byte order mark, or
// the NUL byte of every other character that such ASCII text has.
func wide(content []byte) bool {
	if bytes.HasPrefix(content, []byte{0xFF, 0xFE}) || bytes.HasPrefix(content, []byte{0xFE, 0xFF}) {
		return true
	}
	sample := content[:min(len(content), 4096)]
	return len(sample) >= 16 && bytes.Count(sample, []byte{0}) > len(sample)/4
}

// binary reports content that is not text in any encoding: in its first
// 8 KiB, more than 1% NUL or other control bytes (tab, line and page breaks
// and escape aside), or more than two fifths of the bytes undecodable as
// UTF-8.
// Source in a single-byte encoding has a few undecodable bytes, in its
// comments and strings; a binary file has them throughout.
func binary(content []byte) bool {
	sample := content[:min(len(content), 8192)]
	if len(sample) < 64 {
		return false
	}
	control, undecodable := 0, 0
	for i := 0; i < len(sample); {
		c := sample[i]
		if c < 0x20 && c != '\t' && c != '\n' && c != '\r' && c != '\f' && c != '\v' && c != 0x1b {
			control++
		}
		if c < 0x80 {
			i++
			continue
		}
		r, size := utf8.DecodeRune(sample[i:])
		if r == utf8.RuneError && size <= 1 {
			if len(sample)-i >= utf8.UTFMax { // a character cut at the sample's end is not evidence
				undecodable++
			}
			i++
			continue
		}
		i += size
	}
	return control*100 > len(sample) || undecodable*5 > len(sample)*2
}

// Text is the content as the engines read it: the bytes themselves when
// they are UTF-8, otherwise a copy of the same length in which every byte
// that is not part of a UTF-8 character reads as '?'. Source written in a
// single-byte encoding (Latin-1, Windows-1252) keeps its declarations, and
// every byte offset, so every span, still addresses the original file.
func (input Input) Text() []byte {
	if utf8.Valid(input.Content) {
		return input.Content
	}
	out := make([]byte, len(input.Content))
	for i := 0; i < len(input.Content); {
		r, size := utf8.DecodeRune(input.Content[i:])
		if r == utf8.RuneError && size <= 1 {
			out[i] = '?'
			i++
			continue
		}
		copy(out[i:], input.Content[i:i+size])
		i += size
	}
	return out
}
