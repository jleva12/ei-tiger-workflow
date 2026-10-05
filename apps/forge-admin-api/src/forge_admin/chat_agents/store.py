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
Publishing checks that it builds.
"""

import json
from datetime import datetime
from typing import Any

from forge_agent_runtime.document import FORMAT, schema_problems, with_defaults
from pymongo import ASCENDING, IndexModel, ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError

from forge_admin.adk_workflows.document_store import DocumentStore, iso, new_id, now

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


class ChatAgentState(Exception):
    """
    The agent isn't in the state the change needs: there's no draft to save
    or publish, or a draft already, or nothing published to start from.

    :ivar code: ``NO_DRAFT``, ``DRAFT_EXISTS`` or ``NOT_PUBLISHED``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


def status_of(record: dict[str, Any]) -> str:
    """:return: ``draft`` (never published), ``published``, or ``published+draft``."""
    if record.get("published_version") is None:
        return "draft"
    return "published+draft" if record.get("has_draft") else "published"


class ChatAgentStore(DocumentStore):
    """
    The agents and their published versions. Agent records look like::

        {"_id": "ca_…", "organization_id", "revision": 3, "document": {…},
         "has_draft": True, "draft_version": 2, "published_version": 1,
         "published_at", "created_at", "created_by", "updated_at",
         "updated_by", "updated_by_name", "deleted_at": None}

    and version records like::

        {"_id": "ca_…@1", "agent_id", "organization_id", "version": 1,
         "document": {…, "version": 1}, "published_at", "published_by",
         "published_by_name"}
    """

    collection_name = COLLECTION
    conflict = ChatAgentConflict
    id_taken = ChatAgentTaken

    def __init__(self, client: Any, database: str) -> None:
        super().__init__(client, database)
        self._versions: AsyncCollection = client[database][VERSIONS]
        self._versions_indexed = False

    async def _versions_ready(self) -> AsyncCollection:
        if not self._versions_indexed:
            await self._versions.create_indexes(
                [
                    IndexModel(
                        [("agent_id", ASCENDING), ("version", ASCENDING)],
                        name="agent_version",
                        unique=True,
                    ),
                    IndexModel([("organization_id", ASCENDING)], name="organization"),
                ]
            )
            self._versions_indexed = True
        return self._versions

    async def create_agent(
        self,
        organization_id: str,
        document: dict[str, Any],
        *,
        by: str,
        by_name: str | None,
        max_bytes: int,
    ) -> dict[str, Any]:
        """
        :return: A new agent: a draft of version 1.
        :raises ChatAgentError: The document isn't a chat agent.
        :raises ChatAgentTaken: The ID it was given is used (try again).
        """
        moment = now()
        agent_id = new_chat_agent_id()
        stored = checked_document(
            document,
            agent_id=agent_id,
            organization_id=organization_id,
            created_at=moment,
            updated_at=moment,
            max_bytes=max_bytes,
        )
        return await self.create(
            {
                "_id": agent_id,
                "organization_id": organization_id,
                "revision": 1,
                "document": stored,
                "has_draft": True,
                "draft_version": 1,
                "published_version": None,
                "published_at": None,
                "created_at": moment,
                "created_by": by,
                "updated_at": moment,
                "updated_by": by,
                "updated_by_name": by_name,
                "deleted_at": None,
            }
        )

    async def save_draft(
        self,
        organization_id: str,
        agent_id: str,
        revision: int,
        document: dict[str, Any],
        *,
        by: str,
        by_name: str | None,
        max_bytes: int,
    ) -> dict[str, Any] | None:
        """
        :return: The agent at its next revision; None when there's no such agent.
        :raises ChatAgentState: ``NO_DRAFT``: it's published, with no draft to save to.
        :raises ChatAgentConflict: The draft was saved since ``revision``.
        :raises ChatAgentError: The document isn't a chat agent.
        """
        current = await self.get(organization_id, agent_id)
        if current is None:
            return None
        if not current.get("has_draft"):
            raise ChatAgentState(
                "NO_DRAFT",
                "It's published, and published versions can't change: "
                "start a new version to edit it.",
            )
        if current["revision"] != revision:
            raise ChatAgentConflict(agent_id)
        moment = now()
        stored = checked_document(
            document,
            agent_id=agent_id,
            organization_id=organization_id,
            created_at=current["created_at"],
            updated_at=moment,
            max_bytes=max_bytes,
        )
        return await self._change(
            organization_id,
            agent_id,
            revision,
            {
                "document": stored,
                "updated_at": moment,
                "updated_by": by,
                "updated_by_name": by_name,
            },
            draft=True,
        )

    async def _change(
        self,
        organization_id: str,
        agent_id: str,
        revision: int,
        changes: dict[str, Any],
        *,
        draft: bool | None,
    ) -> dict[str, Any]:
        collection = await self._ready()
        where: dict[str, Any] = {
            "_id": agent_id,
            "organization_id": organization_id,
            "deleted_at": None,
            "revision": revision,
        }
        if draft is not None:
            where["has_draft"] = draft
        found = await collection.find_one_and_update(
            where,
            {"$set": changes, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if found is None:
            raise ChatAgentConflict(agent_id)
        return found

    async def publish(
        self,
        organization_id: str,
        agent_id: str,
        revision: int,
        *,
        by: str,
        by_name: str | None,
    ) -> dict[str, Any] | None:
        """
        Freeze the draft as its version. Check it builds first: this doesn't.

        :return: The agent, its draft now published; None when there's no such agent.
        :raises ChatAgentState: ``NO_DRAFT``: there's nothing to publish.
        :raises ChatAgentConflict: The draft was saved since ``revision``.
        """
        current = await self.get(organization_id, agent_id)
        if current is None:
            return None
        if not current.get("has_draft"):
            raise ChatAgentState(
                "NO_DRAFT", "There's no draft to publish: start a new version first."
            )
        if current["revision"] != revision:
            raise ChatAgentConflict(agent_id)
        version = int(current["draft_version"])
        moment = now()
        document = {**current["document"], "version": version}
        versions = await self._versions_ready()
        try:
            await versions.insert_one(
                {
                    "_id": f"{agent_id}@{version}",
                    "agent_id": agent_id,
                    "organization_id": organization_id,
                    "version": version,
                    "document": document,
                    "published_at": moment,
                    "published_by": by,
                    "published_by_name": by_name,
                }
            )
        except DuplicateKeyError as error:  # Published by someone else just now.
            raise ChatAgentConflict(agent_id) from error
        try:
            return await self._change(
                organization_id,
                agent_id,
                revision,
                {
                    "has_draft": False,
                    "draft_version": None,
                    "published_version": version,
                    "published_at": moment,
                    "updated_at": moment,
                    "updated_by": by,
                    "updated_by_name": by_name,
                },
                draft=True,
            )
        except ChatAgentConflict:
            # The draft changed between the check and now: the version it froze isn't it.
            await versions.delete_one({"_id": f"{agent_id}@{version}"})
            raise

    async def start_version(
        self,
        organization_id: str,
        agent_id: str,
        *,
        from_version: int | None,
        by: str,
        by_name: str | None,
    ) -> dict[str, Any] | None:
        """
        :return: The agent with a new draft (the next version), copied from
            ``from_version`` or the latest published; None when there's no such
            agent or version.
        :raises ChatAgentState: ``DRAFT_EXISTS`` or ``NOT_PUBLISHED``.
        """
        current = await self.get(organization_id, agent_id)
        if current is None:
            return None
        if current.get("has_draft"):
            raise ChatAgentState(
                "DRAFT_EXISTS",
                "It has a draft already: edit that, or discard it first.",
            )
        latest = current.get("published_version")
        if latest is None:
            raise ChatAgentState(
                "NOT_PUBLISHED", "It hasn't been published yet: edit its draft."
            )
        source = await self.version(
            organization_id, agent_id, from_version or int(latest)
        )
        if source is None:
            return None
        moment = now()
        document = {
            key: value for key, value in source["document"].items() if key != "version"
        }
        document["updated_at"] = iso(moment)
        return await self._change(
            organization_id,
            agent_id,
            current["revision"],
            {
                "document": document,
                "has_draft": True,
                "draft_version": int(latest) + 1,
                "updated_at": moment,
                "updated_by": by,
                "updated_by_name": by_name,
            },
            draft=False,
        )

    async def discard_draft(
        self, organization_id: str, agent_id: str, *, by: str, by_name: str | None
    ) -> dict[str, Any] | None:
        """
        :return: The agent back at its latest published version; None when there's no such agent.
        :raises ChatAgentState: ``NO_DRAFT``, or ``NOT_PUBLISHED`` (delete the agent instead).
        """
        current = await self.get(organization_id, agent_id)
        if current is None:
            return None
        if not current.get("has_draft"):
            raise ChatAgentState("NO_DRAFT", "There's no draft to discard.")
        latest = current.get("published_version")
        if latest is None:
            raise ChatAgentState(
                "NOT_PUBLISHED",
                "It has never been published: delete the agent instead.",
            )
        published = await self.version(organization_id, agent_id, int(latest))
        assert published is not None
        moment = now()
        document = {
            key: value
            for key, value in published["document"].items()
            if key != "version"
        }
        return await self._change(
            organization_id,
            agent_id,
            current["revision"],
            {
                "document": document,
                "has_draft": False,
                "draft_version": None,
                "updated_at": moment,
                "updated_by": by,
                "updated_by_name": by_name,
            },
            draft=True,
        )

    async def version(
        self, organization_id: str, agent_id: str, version: int
    ) -> dict[str, Any] | None:
        """:return: One of the agent's published versions, or None."""
        versions = await self._versions_ready()
        return await versions.find_one(
            {
                "agent_id": agent_id,
                "organization_id": organization_id,
                "version": version,
            }
        )

    async def versions(
        self, organization_id: str, agent_id: str
    ) -> list[dict[str, Any]]:
        """:return: The agent's published versions, newest first, without their documents."""
        versions = await self._versions_ready()
        cursor = versions.find(
            {"agent_id": agent_id, "organization_id": organization_id}, {"document": 0}
        ).sort("version", -1)
        return await cursor.to_list()

    async def find(self, agent_id: str) -> dict[str, Any] | None:
        """:return: An agent by its ID alone, for the runtime; None when it's gone."""
        collection = await self._ready()
        return await collection.find_one({"_id": agent_id, "deleted_at": None})

    async def find_version(self, agent_id: str, version: int) -> dict[str, Any] | None:
        """:return: A published version by the agent's ID alone, for the runtime."""
        versions = await self._versions_ready()
        return await versions.find_one({"agent_id": agent_id, "version": version})

    async def delete_organization(self, organization_id: str) -> int:
        versions = await self._versions_ready()
        await versions.delete_many({"organization_id": organization_id})
        return await super().delete_organization(organization_id)
