package deployment

import (
	"fmt"
	"net/url"
	"regexp"
	"strings"

	"ei-aitiger-codegraph/pkg/graph"
)

// GitHubLocator names a repository on github.com as its URL was written.
// CanonicalURL lower-cases it and RepositoryID derives the stable internal
// identity from that form, so the worker, the admin API and the ingest
// command register and look up the same repository.
type GitHubLocator struct {
	Owner string
	Name  string
}

var (
	githubOwner = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`)
	githubName  = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}$`)
)

// ParseGitHubURL accepts a github.com HTTPS repository URL, with an optional
// .git suffix and trailing slash, and nothing else: no credentials, port,
// query, fragment, escaped path or browser path such as /tree/main. Revision
// selection is never inferred from the URL.
func ParseGitHubURL(raw string) (GitHubLocator, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || !strings.EqualFold(u.Host, "github.com") ||
		u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.RawPath != "" || u.Opaque != "" {
		return GitHubLocator{}, fmt.Errorf("%w: use https://github.com/owner/repository", ErrInvalidRequest)
	}
	parts := strings.Split(strings.TrimSuffix(strings.TrimPrefix(u.Path, "/"), "/"), "/")
	if len(parts) != 2 {
		return GitHubLocator{}, fmt.Errorf("%w: expected a repository URL", ErrInvalidRequest)
	}
	l := GitHubLocator{Owner: parts[0], Name: strings.TrimSuffix(parts[1], ".git")}
	if !githubOwner.MatchString(l.Owner) || !githubName.MatchString(l.Name) || l.Name == "." || l.Name == ".." {
		return GitHubLocator{}, fmt.Errorf("%w: expected GitHub owner and repository name", ErrInvalidRequest)
	}
	return l, nil
}

// CanonicalURL is the lower-case https://github.com/owner/name form stored
// as Repository.GitHubURL.
func (l GitHubLocator) CanonicalURL() string {
	return "https://github.com/" + strings.ToLower(l.Owner+"/"+l.Name)
}

// RepositoryID is the repository's stable internal identity.
func (l GitHubLocator) RepositoryID() string { return graph.ID("repo", l.CanonicalURL()) }
