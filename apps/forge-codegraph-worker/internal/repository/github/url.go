package github

import (
	"fmt"

	"ei-aitiger-codegraph/pkg/deployment"
)

// ParseURL accepts a github.com HTTPS repository URL, under
// deployment.ParseGitHubURL's rules, and the full commit SHA to check out.
// Revision selection is explicit in commitSHA, never inferred from a browser
// /tree or /blob URL. Credentials belong in service configuration.
func ParseURL(raw, commitSHA string) (Request, error) {
	l, err := deployment.ParseGitHubURL(raw)
	if err != nil {
		return Request{}, fmt.Errorf("%w: %w", ErrInvalidArgument, err)
	}
	r := Request{Owner: l.Owner, Name: l.Name, CommitSHA: commitSHA}
	if err := r.validate(); err != nil {
		return Request{}, err
	}
	return r, nil
}
