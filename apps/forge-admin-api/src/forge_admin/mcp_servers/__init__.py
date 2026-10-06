"""Organizations' MCP servers: remote (streamable HTTP) MCP servers their
agents use as toolsets.

What's done with a server (its auth methods, credentials encrypted at rest,
listing its tools, connecting it) is ``forge_mcp_servers``
(``packages/python/mcp-servers``), which the async worker shares; a new auth
method is added to its ``registry``. Here:

- :mod:`.toolset`: the servers as this API keeps them (its models), and a
  server as an ADK ``McpToolset`` for the hosted runtime's agents.

Routes: ``forge_admin.api.routes.mcp_servers``; tables:
``forge_admin.models.mcp_servers``.
"""
