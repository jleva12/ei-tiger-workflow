package authorization

import (
	"context"
	"net/http"
)

// OutboundAuthenticator is an optional company integration for delegated API
// calls (assistant -> MCP). It must mint/forward audience-bound credentials
// only to configured internal services. Never serialize unsigned Identity.
type OutboundAuthenticator interface {
	AuthorizeOutbound(context.Context, *http.Request) error
}
type DelegatingTransport struct {
	Provider Provider
	Base     http.RoundTripper
}

func (t DelegatingTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if HasContext(r.Context()) {
		if _, err := CurrentIdentity(r.Context()); err != nil {
			return nil, err
		}
		provider, ok := t.Provider.(OutboundAuthenticator)
		if !ok {
			return nil, ErrUnavailable
		}
		r = r.Clone(r.Context())
		if err := provider.AuthorizeOutbound(r.Context(), r); err != nil {
			return nil, err
		}
	}
	base := t.Base
	if base == nil {
		base = http.DefaultTransport
	}
	return base.RoundTrip(r)
}
