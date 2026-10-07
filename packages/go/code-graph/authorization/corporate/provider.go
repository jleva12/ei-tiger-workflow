// Package corporate is the single company-specific authentication integration
// point shared by the REST services, MCP and the operator bootstrap command.
package corporate

import (
	"os"

	"ei-aitiger-codegraph/authorization"
)

// NewProvider must return your verified SSO/session and directory adapter.
// See authorization.Provider's trust and membership-freshness contract.
// Corporate mode remains a deny-only stub. Local auto-login is an explicit,
// separate development option; it does not implement corporate verification.
func NewProvider() authorization.Provider {
	if os.Getenv("CODEGRAPH_AUTHORIZATION_MODE") == "local-development" {
		return authorization.LocalDevelopmentProvider{
			TenantID:    os.Getenv("CODEGRAPH_AUTHORIZATION_LOCAL_TENANT_ID"),
			User:        os.Getenv("CODEGRAPH_AUTHORIZATION_LOCAL_USER"),
			DisplayName: os.Getenv("CODEGRAPH_AUTHORIZATION_LOCAL_NAME"),
		}
	}
	return authorization.UnconfiguredProvider{}
}
