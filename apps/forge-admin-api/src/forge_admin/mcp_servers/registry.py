"""The auth methods MCP servers may use. Register a new one here."""

from forge_admin.mcp_servers.auth import AuthMethods
from forge_admin.mcp_servers.methods import ApiKeyAuth, BearerAuth, NoAuth
from forge_admin.mcp_servers.oauth import OAuthAuthorizationCode, OAuthClientCredentials

AUTH_METHODS = AuthMethods(
    [
        NoAuth(),
        ApiKeyAuth(),
        BearerAuth(),
        OAuthAuthorizationCode(),
        OAuthClientCredentials(),
    ]
)
