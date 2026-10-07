package deployment

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestFailureMessageMasksCredentials(t *testing.T) {
	for in, want := range map[string]string{
		"fetch https://user:secret@example.com/repo.git failed":              "fetch https://***@example.com/repo.git failed",
		"401 with Authorization: Basic eC1hY2Nlc3MtdG9rZW46Z2hw":             "401 with Authorization: Basic ***",
		"header authorization: abcdef":                                       "header authorization: ***",
		"sent Bearer abc.def.ghi-jkl to the API":                             "sent Bearer *** to the API",
		"token ghp_abcdefghijklmnopqrstuvwxyz0123 refused":                   "token *** refused",
		"github_pat_11ABCDEFG0123456789_abcdefghij expired":                  "*** expired",
		"a basic setup keeps its words":                                      "a basic setup keeps its words",
		"Maven prepare failed: exit status 1;\n[ERROR]\tInvalid CEN\x00\x1b": "Maven prepare failed: exit status 1;\n[ERROR]\tInvalid CEN",
	} {
		if got := FailureMessage(in); got != want {
			t.Errorf("FailureMessage(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestFailureMessageKeepsTheStartWithinBounds(t *testing.T) {
	text := "ingestion: build context: " + strings.Repeat("é", MaxErrorMessageBytes)
	got := FailureMessage(text)
	if len(got) > MaxErrorMessageBytes || !utf8.ValidString(got) || !strings.HasPrefix(got, "ingestion: build context: ") || !strings.HasSuffix(got, "…") {
		t.Fatalf("len=%d valid=%v prefix=%q suffix=%q", len(got), utf8.ValidString(got), got[:30], got[len(got)-8:])
	}
	if FailureMessage(string([]byte{0xff, 'x'})) != "�x" {
		t.Fatal("invalid UTF-8 kept")
	}
}

func TestRunValidatesErrorMessage(t *testing.T) {
	if err := (Run{ErrorMessage: strings.Repeat("x", MaxErrorMessageBytes+1)}).Validate(); err == nil {
		t.Fatal("oversized message accepted")
	}
}
