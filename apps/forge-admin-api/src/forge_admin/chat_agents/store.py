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

Who may use an agent can be limited to some of its organization's agent
permissions (``access`` on the record, holding for every version:
``VersionedDocumentStore.set_access``; see ``forge_admin.chat_agents.access``);
like the chat page, setting it isn't a change to the agent.

A published agent can also be shared as a **chat page**: a full-page chat at
a link of its own (``chat_page`` on the record, beside its versions; see
``forge_admin.chat_agents.pages``). Turning it on or off, or making a new
link, isn't a change to the agent: its revision and times stay as they are.
"""

import json
import secrets
import string
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from forge_agent_runtime.document import FORMAT, schema_problems, with_defaults
from pymongo import ASCENDING, IndexModel, ReturnDocument

from forge_admin.adk_workflows.document_store import iso, new_id, now
from forge_admin.adk_workflows.versioned_store import (
    VersionedDocumentStore,
    VersionState,
    status_of,
    versioned,
)
from forge_admin.chat_agents.page_settings import page_settings

COLLECTION = "chat_agents"
VERSIONS = "chat_agent_versions"
ID_PATTERN = r"^ca_[a-z0-9]{6,40}$"
#: A chat page's link: ``cp_`` and 24 random lowercase letters and digits.
PAGE_TOKEN_PATTERN = r"^cp_[a-z0-9]{24}$"
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


def new_page_token() -> str:
    """
    :return: A new chat page link, ``cp_`` and 24 random lowercase letters
        and digits (about 124 bits): whoever holds it may use the page, so
        it can't be guessed.
    """
    alphabet = string.ascii_lowercase + string.digits
    return "cp_" + "".join(secrets.choice(alphabet) for _ in range(24))


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
    ``version``, ``dependencies`` and ``ui_components`` aren't kept: the store
    knows the first, the second is made when exporting, and the team UI
    components a hosted agent picks are always its organization's.

    :raises ChatAgentError: It's too large, or isn't a chat agent.
    """
    if document.get("format") != FORMAT:
        raise ChatAgentError(
            f'It isn\'t a chat agent: its "format" should be "{FORMAT}".'
        )
    kept = {
        key: value
        for key, value in document.items()
        if key not in ("version", "dependencies", "ui_components")
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


def _picks(document: Mapping[str, Any], keys: Sequence[str]) -> set[str]:
    """:return: Which of these components a document's Rich answers nodes pick."""
    picked: set[str] = set()
    for node in document.get("nodes") or []:
        if not isinstance(node, Mapping) or node.get("kind") != "rich_answers":
            continue
        config = node.get("config")
        components = config.get("components") if isinstance(config, Mapping) else None
        picked.update(key for key in components or [] if key in keys)
    return picked


__all__ = [
    "ChatAgentConflict",
    "ChatAgentError",
    "ChatAgentState",
    "ChatAgentStore",
    "ChatAgentTaken",
    "checked_document",
    "new_chat_agent_id",
    "new_page_token",
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
    extra_indexes = (
        # A chat page's link finds its agent.
        IndexModel(
            [("chat_page.token", ASCENDING)],
            name="chat_page_token",
            unique=True,
            partialFilterExpression={"chat_page.token": {"$type": "string"}},
        ),
    )

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

    async def set_chat_page(
        self,
        organization_id: str,
        agent_id: str,
        *,
        enabled: bool,
        new_link: bool,
        by: str,
        by_name: str | None,
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        Turn the agent's chat page on or off, keeping its link; or give it a
        new link, which stops the old one. The agent's revision and times
        stay as they are: the builder saves its draft as before.

        :param settings: The page's settings to change (``page_settings``:
            its prompts, what it leaves out, its words and look); the rest
            stay as they are.

        :return: The agent; None when there's no such one.
        :raises VersionState: ``NOT_PUBLISHED``: turning on a page for an
            agent never published (a page runs its latest published version).
        """
        current = await self.get(organization_id, agent_id)
        if current is None:
            return None
        if enabled and current.get("published_version") is None:
            raise VersionState(
                "NOT_PUBLISHED",
                "Publish it first: its chat page runs its latest published version.",
            )
        kept = current.get("chat_page") or {}
        if not kept and not enabled and not settings:
            return current
        page = {
            **page_settings(kept),
            **(settings or {}),
            "token": new_page_token() if new_link or not kept else kept["token"],
            "enabled": enabled,
            "updated_at": now(),
            "updated_by": by,
            "updated_by_name": by_name,
        }
        collection = await self._ready()
        return versioned(
            await collection.find_one_and_update(
                {
                    "_id": agent_id,
                    "organization_id": organization_id,
                    "deleted_at": None,
                },
                {"$set": {"chat_page": page}},
                return_document=ReturnDocument.AFTER,
            )
        )

    async def using_components(
        self, organization_id: str, keys: Sequence[str]
    ) -> list[dict[str, Any]]:
        """
        Which of the organization's agents pick any of these team UI
        components in their Rich answers nodes, in their draft or their
        latest published version (what runs): ``forge_admin.ui_components``.

        :return: Each such agent, by name: ``{"id", "name", "draft": [keys],
            "published": [keys]}``, the keys each of the two picks.
        """
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return []
        picking = {
            "$elemMatch": {"kind": "rich_answers", "config.components": {"$in": wanted}}
        }
        shown = {
            "document.name": 1,
            "document.nodes.kind": 1,
            "document.nodes.config.components": 1,
        }
        uses: dict[str, dict[str, Any]] = {}

        def use(agent_id: str, document: Mapping[str, Any]) -> dict[str, Any]:
            return uses.setdefault(
                agent_id,
                {
                    "id": agent_id,
                    "name": document.get("name"),
                    "draft": set(),
                    "published": set(),
                },
            )

        collection = await self._ready()
        records = collection.find(
            {
                "organization_id": organization_id,
                "deleted_at": None,
                "document.nodes": picking,
            },
            {**shown, "has_draft": 1},
        )
        for found in await records.to_list():
            record = versioned(found)
            assert record is not None
            # Its document is its draft, or its latest published version without one.
            side = "draft" if record.get("has_draft") else "published"
            use(record["_id"], record["document"])[side] |= _picks(
                record["document"], wanted
            )
        versions = await self._versions_ready()
        published = await versions.find(
            {"organization_id": organization_id, "document.nodes": picking},
            {**shown, "agent_id": 1, "version": 1},
        ).to_list()
        if published:
            # Only what runs: each agent's latest version, when it has a draft beside it.
            drafting = {
                record["_id"]: record
                for record in await collection.find(
                    {
                        "_id": {
                            "$in": sorted({each["agent_id"] for each in published})
                        },
                        "organization_id": organization_id,
                        "deleted_at": None,
                        "has_draft": True,
                    },
                    {"published_version": 1, "document.name": 1},
                ).to_list()
            }
            for each in published:
                record = drafting.get(each["agent_id"])
                if record is None or record.get("published_version") != each["version"]:
                    continue
                use(each["agent_id"], record["document"])["published"] |= _picks(
                    each["document"], wanted
                )
        return sorted(
            (
                {
                    **each,
                    "draft": sorted(each["draft"]),
                    "published": sorted(each["published"]),
                }
                for each in uses.values()
            ),
            key=lambda each: (str(each["name"] or "").lower(), each["id"]),
        )

    async def find_chat_page(self, token: str) -> dict[str, Any] | None:
        """:return: The agent whose chat page answers at a link; None when none does."""
        collection = await self._ready()
        return versioned(
            await collection.find_one(
                {
                    "chat_page.token": token,
                    "chat_page.enabled": True,
                    "deleted_at": None,
                }
            )
        )
