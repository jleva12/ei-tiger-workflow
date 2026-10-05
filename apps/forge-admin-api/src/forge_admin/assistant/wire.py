"""ADK's models as the JSON the web console's assistant reads: the agent
runtime's (``forge_agent_runtime.wire``), which chat agents are served with
too. See it for how it differs from ``adk api_server``'s.
"""

from forge_agent_runtime.wire import sse, to_wire

__all__ = ["sse", "to_wire"]
