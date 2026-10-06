# forge-mcp-servers

Organizations' MCP servers, as Forge's agents use them, shared by the admin API
(which keeps the servers, checks them and runs the OAuth sign-in) and the async
worker (whose workflow runs connect to them):

| Module | |
|---|---|
| `auth` | The protocol an auth method follows, and their registry's type |
| `methods`, `oauth` | The methods: none, API key, bearer token, OAuth sign-in, OAuth client credentials |
| `registry` | `AUTH_METHODS`: add a new method here |
| `secrets` | `SecretBox`: credentials and grants encrypted at rest, keyed by the admin's `FORGE_ADMIN_SECRETS_KEY` |
| `client` | Listing a server's tools |
| `service` | `McpServers`: what to send a server (refreshing its grant), checking it, signing in |
| `toolset` | A server as an ADK `McpToolset`, over a `ServerStore` |
| `store` | `SqlServerStore`: the admin MySQL's `mcp_servers` rows, for processes without the admin's models |

A server's grant (OAuth tokens) is renewed under a lock on its row, so the
admin and the worker never both spend one refresh token.

```sh
uv run --package forge-mcp-servers pytest packages/python/mcp-servers
```
