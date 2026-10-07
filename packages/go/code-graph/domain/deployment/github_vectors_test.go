package deployment

import (
	"encoding/json"
	"os"
	"testing"
)

// The Forge admin API names repositories as these functions do; it tests its
// port against the same cases (testdata/github_urls.json).
func TestGitHubNamingVectors(t *testing.T) {
	raw, err := os.ReadFile("testdata/github_urls.json")
	if err != nil {
		t.Fatal(err)
	}
	var vectors struct {
		URLs []struct {
			URL       string `json:"url"`
			Canonical string `json:"canonical"`
		} `json:"urls"`
		Branches struct{ Valid, Invalid []string } `json:"branches"`
		Commits  struct{ Valid, Invalid []string } `json:"commits"`
	}
	if err = json.Unmarshal(raw, &vectors); err != nil {
		t.Fatal(err)
	}
	for _, v := range vectors.URLs {
		locator, err := ParseGitHubURL(v.URL)
		switch {
		case v.Canonical == "" && err == nil:
			t.Errorf("%q accepted as %s", v.URL, locator.CanonicalURL())
		case v.Canonical != "" && err != nil:
			t.Errorf("%q refused: %v", v.URL, err)
		case v.Canonical != "" && locator.CanonicalURL() != v.Canonical:
			t.Errorf("%q is %s, want %s", v.URL, locator.CanonicalURL(), v.Canonical)
		}
	}
	for _, b := range vectors.Branches.Valid {
		if !ValidBranch(b) {
			t.Errorf("branch %q refused", b)
		}
	}
	for _, b := range vectors.Branches.Invalid {
		if ValidBranch(b) {
			t.Errorf("branch %q accepted", b)
		}
	}
	for _, c := range vectors.Commits.Valid {
		if !ValidCommit(c) {
			t.Errorf("commit %q refused", c)
		}
	}
	for _, c := range vectors.Commits.Invalid {
		if ValidCommit(c) {
			t.Errorf("commit %q accepted", c)
		}
	}
}
