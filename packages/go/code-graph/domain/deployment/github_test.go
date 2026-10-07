package deployment

import (
	"errors"
	"testing"
)

func TestParseGitHubURL(t *testing.T) {
	for _, raw := range []string{"https://github.com/Owner/repo", "https://github.com/Owner/repo.git", "https://github.com/Owner/repo.git/", "https://GitHub.com/Owner/repo/"} {
		l, err := ParseGitHubURL(raw)
		if err != nil || l != (GitHubLocator{Owner: "Owner", Name: "repo"}) {
			t.Fatalf("%s: %+v %v", raw, l, err)
		}
		if l.CanonicalURL() != "https://github.com/owner/repo" || l.RepositoryID() != (GitHubLocator{Owner: "owner", Name: "REPO"}).RepositoryID() {
			t.Fatalf("%s: canonical form %s id %s", raw, l.CanonicalURL(), l.RepositoryID())
		}
	}
	for _, raw := range []string{"", "http://github.com/a/b", "https://github.com.evil/a/b", "https://github.com:443/a/b", "https://u:secret@github.com/a/b", "https://github.com/a/b/tree/main", "https://github.com/a/b?x=1", "https://github.com/a/b#x", "https://github.com/a/b?", "https://github.com/a/%62", "https://github.com/a/..", "https://github.com/a/.", "https://github.com/-a/b", "git@github.com:a/b.git", "https://github.com/a/b//", "https://github.com/a"} {
		if _, err := ParseGitHubURL(raw); !errors.Is(err, ErrInvalidRequest) {
			t.Fatalf("accepted %q: %v", raw, err)
		}
	}
	if (GitHubLocator{Owner: "a", Name: "b"}).RepositoryID() == (GitHubLocator{Owner: "a", Name: "c"}).RepositoryID() {
		t.Fatal("distinct repositories share an id")
	}
}
