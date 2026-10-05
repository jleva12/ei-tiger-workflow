"""Organizations' chat agents: drafts, published versions, and running them.

- :mod:`.store`: the agents (``chat_agents``) and their published versions
  (``chat_agent_versions``) in MongoDB, and how an agent goes from draft to
  published and back to a new draft.
- :mod:`.source`: where the hosted runtime (``forge_agent_runtime``) finds an
  agent to run by ID, and what it resolves for it: saved agents, the
  organization's MCP servers, its workflows.

Routes: ``forge_admin.api.routes.chat_agents`` (the lifecycle) and the
runtime's own router, mounted at ``/runtime``.
"""
