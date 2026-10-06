"""
Organizations' chat agents (``forge.chat_agent/v1``) in MongoDB.

An agent has one ID for life (``ca_…``) and goes through versions:

- **Draft**: what the builder edits and autosaves. A new agent is a draft of
  version 1. Saves name the revision they were made from, and an older one is
  refused (:class:`ChatAgentConflict`), as for workflows.
- **Published**: publishing freezes the draft as version N in
  ``chat_agent_versions``. Nothing changes a published version afterwards;
  the store has no way to.
- **New version**: a draft of version N+1, copied from a published version
  (the latest by default), edited while the published ones keep running.
  An agent has one draft at a time.

The agent's record keeps the document the builder shows: its draft when it
has one, otherwise its latest published version. Only the format's JSON
Schema is checked on save: a draft that can't run yet is saved all the same.
Publishing checks that it builds. The workflows are versioned the same way
(``forge_admin.adk_workflows.versioned_store``).
"""

import json
from datetime import datetime
from typing import Any

from forge_agent_runtime.document import FORMAT, schema_problems, with_defaults

from forge_admin.adk_workflows.document_store import iso, new_id
from forge_admin.adk_workflows.versioned_store import (
    VersionedDocumentStore,
    VersionState,
    status_of,
)

COLLECTION = "chat_agents"
VERSIONS = "chat_agent_versions"
ID_PATTERN = r"^ca_[a-z0-9]{6,40}$"
MAX_PROBLEMS = 5


class ChatAgentError(Exception):
    """A document that isn't a chat agent, or is too large to keep."""


class ChatAgentConflict(Exception):
    """A save made from an older revision of the draft."""


class ChatAgentTaken(Exception):
    """A new agent's ID is used already."""


#: The agent isn't in the state the change needs (``NO_DRAFT``,
#: ``DRAFT_EXISTS``, ``NOT_PUBLISHED``), as for any versioned document.
ChatAgentState = VersionState


def new_chat_agent_id() -> str:
    """:return: A new agent ID, ``ca_`` and ten random letters and digits."""
    return new_id("ca_")


def checked_document(
    document: dict[str, Any],
    *,
    agent_id: str,
    organization_id: str,
    created_at: datetime,
    updated_at: datetime,
    max_bytes: int,
) -> dict[str, Any]:
    """
    The document to keep: the builder's, its settings filled in, with its ID,
    organization and times set here, checked against the format. Exports'
    ``version`` and ``dependencies`` aren't kept: the store knows the one, and
    the other is made when exporting.

    :raises ChatAgentError: It's too large, or isn't a chat agent.
    """
    if document.get("format") != FORMAT:
        raise ChatAgentError(
            f'It isn\'t a chat agent: its "format" should be "{FORMAT}".'
        )
    kept = {
        key: value
        for key, value in document.items()
        if key not in ("version", "dependencies")
    }
    stored = {
        **with_defaults(kept),
        "id": agent_id,
        "organization_id": organization_id,
        "created_at": iso(created_at),
        "updated_at": iso(updated_at),
    }
    size = len(json.dumps(stored, ensure_ascii=False).encode())
    if size > max_bytes:
        raise ChatAgentError(
            f"The agent is {size:,} bytes; the most an agent can be is {max_bytes:,}."
        )
    problems = schema_problems(stored)
    if problems:
        named = problems[:MAX_PROBLEMS]
        more = len(problems) - len(named)
        raise ChatAgentError(
            f"It isn't a {FORMAT} document. "
            + "; ".join(named)
            + (f"; and {more} more." if more else ".")
        )
    return stored


__all__ = [
    "ChatAgentConflict",
    "ChatAgentError",
    "ChatAgentState",
    "ChatAgentStore",
    "ChatAgentTaken",
    "checked_document",
    "new_chat_agent_id",
    "status_of",
]


class ChatAgentStore(VersionedDocumentStore):
    """
    The agents and their published versions (``VersionedDocumentStore``):
    ``chat_agents``, and ``chat_agent_versions`` (``ca_…@1``).
    """

    collection_name = COLLECTION
    versions_name = VERSIONS
    id_prefix = "ca_"
    conflict = ChatAgentConflict
    id_taken = ChatAgentTaken

    def checked(
        self,
        document: dict[str, Any],
        *,
        agent_id: str,
        organization_id: str,
        created_at: datetime,
        updated_at: datetime,
        max_bytes: int,
    ) -> dict[str, Any]:
        """:raises ChatAgentError: It's too large, or isn't a chat agent."""
        return checked_document(
            document,
            agent_id=agent_id,
            organization_id=organization_id,
            created_at=created_at,
            updated_at=updated_at,
            max_bytes=max_bytes,
        )
