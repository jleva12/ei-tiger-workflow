"""Forge chat agents on Google ADK.

- :func:`load_document`: a chat agent's exported JSON (``forge.chat_agent/v1``).
- :func:`build_app`: one as an ADK app.
- :class:`AgentExecutor`: builds agents by ID from an :class:`AgentSource`
  (a file, a folder, a database) and runs their turns.
- :class:`AgentServer` (the ``server`` extra): an agent's whole web app, put
  together with a builder: ``AgentServer().with_agent("agent.json").build()``.
- :mod:`forge_agent_runtime.server`: ADK's run API alone, to mount in your
  own FastAPI app; ``forge-agent serve`` serves it.
- :mod:`forge_agent_runtime.a2a` (the ``a2a`` extra): the agents over Google's
  A2A protocol (JSON-RPC, 1.0 and 0.3) and their agent cards, beside the run
  API; ``AgentServer().with_a2a()`` serves it.
"""

from forge_agent_runtime.app import AgentServer, AgentServerSettings, MissingSetting, env
from forge_agent_runtime.build import TOOL_KINDS, BuildError, build_app, build_root_agent, build_tools
from forge_agent_runtime.document import ChatAgentDocument, DocumentError, load_document, parse_document
from forge_agent_runtime.executor import (
    AgentExecutor,
    AgentNotFound,
    AgentRef,
    AgentSource,
    DirectoryAgentSource,
    DocumentAgentSource,
    FileAgentSource,
    ResolvedAgent,
    RunObserver,
    RunRequest,
    StateRefused,
)
from forge_agent_runtime.services import (
    AgentResolver,
    KnowledgeBases,
    McpServers,
    RuntimeServices,
    WorkflowRunner,
)

__all__ = [
    "AgentExecutor",
    "AgentNotFound",
    "AgentRef",
    "AgentResolver",
    "AgentServer",
    "AgentServerSettings",
    "AgentSource",
    "BuildError",
    "ChatAgentDocument",
    "DirectoryAgentSource",
    "DocumentAgentSource",
    "DocumentError",
    "FileAgentSource",
    "KnowledgeBases",
    "McpServers",
    "MissingSetting",
    "ResolvedAgent",
    "RunObserver",
    "RunRequest",
    "RuntimeServices",
    "StateRefused",
    "TOOL_KINDS",
    "WorkflowRunner",
    "build_app",
    "build_root_agent",
    "build_tools",
    "env",
    "load_document",
    "parse_document",
]
