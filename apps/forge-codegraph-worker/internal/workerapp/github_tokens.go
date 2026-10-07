package workerapp

import "context"

type tokenSource = func(context.Context, string, string) (string, error)

// staticTokenSource authenticates every clone, fetch and ls-remote with
// CODEGRAPH_GITHUB_TOKEN, whichever repository it reads and whoever
// requested the run: requested_by is kept for audit and logs. Without a
// token it is nil, and every fetch is anonymous, which reads public
// repositories only.
func staticTokenSource(token string) tokenSource {
	if token == "" {
		return nil
	}
	return func(context.Context, string, string) (string, error) { return token, nil }
}
