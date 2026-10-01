"""The assistant: Google ADK agents, their conversations and their files.

``runtime.AgentRuntime`` is what the ``/agents`` routes run: one ADK
``Runner`` per app (``forge.py``, the Forge assistant) over shared session and
artifact services. ``wire.py`` turns ADK's models into the JSON the web
console's assistant reads.

This module stays free of ADK imports, so the migrations can read
``ADK_TABLES`` without loading it.
"""

# The tables ADK's DatabaseSessionService creates and migrates itself, next to
# ours in the admin database: conversations (sessions), their events, and the
# app- and user-wide state. Alembic's autogenerate leaves them alone.
ADK_TABLES = frozenset(
    {"adk_internal_metadata", "sessions", "events", "app_states", "user_states"}
)
