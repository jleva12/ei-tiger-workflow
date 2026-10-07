package github

import (
	"strings"
	"testing"
)

func TestParseURL(t *testing.T) {
	sha := strings.Repeat("abcdef0123", 4)
	for _, raw := range []string{"https://github.com/Owner/repo", "https://github.com/Owner/repo.git", "https://github.com/Owner/repo.git/"} {
		r, err := ParseURL(raw, sha)
		if err != nil || r != (Request{Owner: "Owner", Name: "repo", CommitSHA: sha}) {
			t.Fatalf("%s: %+v %v", raw, r, err)
		}
	}
	for _, raw := range []string{"http://github.com/a/b", "https://github.com.evil/a/b", "https://github.com:443/a/b", "https://u:secret@github.com/a/b", "https://github.com/a/b/tree/main", "https://github.com/a/b?x=1", "https://github.com/a/b#x", "https://github.com/a/b?", "https://github.com/a/%62", "https://github.com/a/..", "git@github.com:a/b.git", "https://github.com/a/b//"} {
		if _, err := ParseURL(raw, sha); err == nil {
			t.Fatalf("accepted %s", raw)
		}
	}
	for _, bad := range []string{"", "--upload-pack=evil", "refs/heads/main", strings.ToUpper(sha), sha[:39]} {
		if _, err := ParseURL("https://github.com/a/b", bad); err == nil {
			t.Fatalf("accepted commit %q", bad)
		}
	}
}
