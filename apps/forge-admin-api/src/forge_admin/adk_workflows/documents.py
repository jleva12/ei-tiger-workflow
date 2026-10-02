"""
Organizations' agents: the ``forge.agent/v1`` documents the web console's
agent builder saves as they're edited, kept in MongoDB, one per agent.

An agent is a Google ADK graph: its nodes (ADK agents with their sub-agents,
other saved agents, and Forge's own steps: approvals, human input, HTTP
requests, transforms, delays, If / Switch / Match, loops, merges and ends)
joined by edges and run from its start. ``forge_task_adk_workflows.graph`` builds one into ADK's
objects.

An agent belongs to its organization, so everyone in the organization reads
and changes the same one. The document is stored as the builder sends it
(MongoDB keeps its keys in order, which JSON Schemas in it rely on), checked
against the format's JSON Schema, with its ``id``, ``organization_id`` and
times set here. Only the schema is checked: a draft that can't run yet is
saved all the same.

Every save names the revision it was made from; a save from an older one
is refused (:class:`AgentConflict`), so two people's changes never
silently overwrite each other. Deleting keeps the record, marked deleted,
out of every read; IDs are never reused.
"""

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from forge_admin.adk_workflows.document_store import (
    DocumentStore,
    fill_added_settings,
    iso,
    load_validator,
    new_id,
    problems_in,
)

COLLECTION = "agents"
FORMAT = "forge.agent/v1"
# The builder's IDs and this module's: ag_ and lowercase letters and digits.
ID_PATTERN = r"^ag_[a-z0-9]{6,40}$"
# The most schema problems a refusal names.
MAX_PROBLEMS = 5

SCHEMA_PATH = Path(__file__).with_name("agent.schema.json")
# Settings a kind of node gained after agents were stored, and what a
# document saved before them means by leaving them out. The schema requires
# every setting, so they're filled in when such a document is saved again
# (the web's document.ts fills them in when the builder opens one).
ADDED_SETTINGS: dict[str, dict[str, Any]] = {}


class AgentError(Exception):
    """
    Represents an exception specific to agent-related errors.

    This class is used to encapsulate errors relevant to agents within the system.
    It extends the base exception class, allowing for the creation of specialized
    error handling scenarios for agents.

    :ivar message: The error message providing context about the nature of the
        exception.
    :type message: str
    """


class AgentConflict(Exception):
    """
    Represents an exception raised when there is a conflict involving agents.

    This exception is intended to signal cases where operations or states
    involving agents cannot proceed due to conflicting conditions.
    """


class AgentIdTaken(Exception):
    """
    Exception raised when an attempt is made to assign an already taken agent ID.

    This exception is used to indicate that the agent ID being assigned is not
    available, as it has already been taken by another instance or entity. It is
    particularly useful in systems where unique identification of agents or entities
    is critical for proper functionality.
    """


@lru_cache
def _validator() -> Draft202012Validator:
    # Generated from the web's src/features/adk-workflows/lib/schema.ts
    # (scripts/generate-agent-schema.mjs), so both check the same format.
    return load_validator(SCHEMA_PATH)


def with_added_settings(document: dict[str, Any]) -> dict[str, Any]:
    """
    A document with the settings its nodes' kinds gained since it was saved
    (``ADDED_SETTINGS``). Nothing it has is changed.

    :param document: An agent document.
    :return: The document; the same object when nothing was missing.
    """
    return fill_added_settings(document, ADDED_SETTINGS)


def new_agent_id() -> str:
    """:return: A new agent ID, ``ag_`` and ten random letters and digits."""
    return new_id("ag_")


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
    The document to store: the one given with its ID, organization and times set
    here, checked against the agent format.

    :param document: The document the builder sent.
    :param agent_id: The agent's ID.
    :param organization_id: The organization it belongs to.
    :param created_at: When the agent was made.
    :param updated_at: When this version was saved.
    :param max_bytes: The largest document accepted, in bytes of JSON.
    :return: The document, keys in the order given.
    :raises AgentError: When it's too large or isn't an agent.
    """
    stored = {
        **with_added_settings(document),
        "id": agent_id,
        "organization_id": organization_id,
        "created_at": iso(created_at),
        "updated_at": iso(updated_at),
    }
    size = len(json.dumps(stored, ensure_ascii=False).encode())
    if size > max_bytes:
        raise AgentError(
            f"The agent is {size:,} bytes; the most an agent can be is {max_bytes:,}."
        )
    problems = schema_problems(stored)
    if problems:
        named = problems[:MAX_PROBLEMS]
        more = len(problems) - len(named)
        raise AgentError(
            f"It isn't a {FORMAT} document. "
            + "; ".join(named)
            + (f"; and {more} more." if more else ".")
        )
    return stored


def schema_problems(document: dict[str, Any]) -> list[str]:
    """
    :param document: An agent document.
    :return: Every way it doesn't fit the format's JSON Schema, each with
        where (``nodes/5/config/sub_agents/0/config: ...``), in document order.
    """
    return problems_in(_validator(), document, whole="The agent")


class AgentStore(DocumentStore):
    """
    The agents collection. Records look like::

        {"_id": "ag_…", "organization_id", "revision": 3, "document": {…},
         "created_at", "created_by", "updated_at", "updated_by",
         "updated_by_name", "deleted_at": None}

    Reads never return deleted records. Every read and write is by organization:
    an agent is found only through the organization it belongs to.
    """

    collection_name = COLLECTION
    conflict = AgentConflict
    id_taken = AgentIdTaken
