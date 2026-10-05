"""Organizations' MCP servers: remote (streamable HTTP) MCP servers their
agents use as toolsets.

- :mod:`.auth`: the protocol auth methods follow, and their registry's type.
- :mod:`.methods`, :mod:`.oauth`: the methods there are (none, API key,
  bearer token, OAuth sign-in, OAuth client credentials).
- :mod:`.registry`: :data:`~.registry.AUTH_METHODS`, where a new one is added.
- :mod:`.secrets`: credentials encrypted at rest.
- :mod:`.client`: listing a server's tools.
- :mod:`.service`: what the API does with a server through its method.
- :mod:`.toolset`: a server as an ADK ``McpToolset``, for agents.

Routes: ``forge_admin.api.routes.mcp_servers``; tables:
``forge_admin.models.mcp_servers``.
"""
