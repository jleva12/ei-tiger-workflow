"""
Documents with versions, in MongoDB: the chat agents (``ca_…``) and the
workflows (``ag_…``). Each has one ID for life and goes through versions:

- **Draft**: what the builder edits and autosaves. A new one is a draft of
  version 1. Saves name the revision they were made from, and an older one
  is refused (the store's ``conflict``).
- **Published**: publishing freezes the draft as version N in a collection
  of its own. Nothing changes a published version afterwards; the store has
  no way to.
- **New version**: a draft of version N+1, copied from a published version
  (the latest by default), edited while the published ones keep running.
  One draft at a time.

The record keeps the document the builder shows: its draft when it has one,
otherwise its latest published version. A record saved before versions
existed (a workflow) reads as a draft of version 1, never published.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pymongo import ASCENDING, IndexModel, ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError

from forge_admin.adk_workflows.document_store import DocumentStore, iso, new_id, now

#: What a record saved before versions existed is: a draft of version 1.
UNVERSIONED = {
    "has_draft": True,
    "draft_version": 1,
    "published_version": None,
    "published_at": None,
}


class VersionState(Exception):
    """
    The document isn't in the state the change needs: there's no draft to
    save or publish, or a draft already, or nothing published to start from.

    :ivar code: ``NO_DRAFT``, ``DRAFT_EXISTS`` or ``NOT_PUBLISHED``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def status_of(record: dict[str, Any]) -> str:
    """:return: ``draft`` (never published), ``published``, or ``published+draft``."""
    if record.get("published_version") is None:
        return "draft"
    return "published+draft" if record.get("has_draft") else "published"


def versioned(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """:return: The record, its version fields filled in when it predates them."""
    if record is None or "has_draft" in record:
        return record
    return {**record, **UNVERSIONED}


class VersionedDocumentStore(DocumentStore):
    """
    Documents and their published versions. Records look like::

        {"_id": "…", "organization_id", "revision": 3, "document": {…},
         "has_draft": True, "draft_version": 2, "published_version": 1,
         "published_at", "created_at", "created_by", "updated_at",
         "updated_by", "updated_by_name", "deleted_at": None}

    and version records like::

        {"_id": "…@1", "agent_id", "organization_id", "version": 1,
         "document": {…, "version": 1}, "published_at", "published_by",
         "published_by_name"}

    A subclass names its collections and ID prefix, and checks documents.
    """

    #: Where published versions are kept.
    versions_name: ClassVar[str]
    #: A new document's ID is this and ten random letters and digits.
    id_prefix: ClassVar[str]
    #: What a document is called in refusals: "agent", "workflow".
    noun: ClassVar[str] = "agent"

    def __init__(self, client: Any, database: str) -> None:
        super().__init__(client, database)
        self._versions: AsyncCollection = client[database][self.versions_name]
        self._versions_indexed = False

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
        """
        :return: The document to keep: checked against its format, with its
            ID, organization and times set here.
        """
        raise NotImplementedError

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

    async def list(self, organization_id: str) -> list[dict[str, Any]]:
        return [
            record
            for record in map(versioned, await super().list(organization_id))
            if record is not None
        ]

    async def get(
        self, organization_id: str, document_id: str
    ) -> dict[str, Any] | None:
        return versioned(await super().get(organization_id, document_id))

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
        :return: A new document: a draft of version 1.
        :raises Exception: The document doesn't fit its format; ``id_taken``
            when the ID it was given is used (try again).
        """
        moment = now()
        agent_id = new_id(self.id_prefix)
        stored = self.checked(
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
                **UNVERSIONED,
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
        :return: The document at its next revision; None when there's no such one.
        :raises VersionState: ``NO_DRAFT``: it's published, with no draft to save to.
        :raises Exception: ``conflict``: the draft was saved since ``revision``;
            the document doesn't fit its format.
        """
        current = await self.get(organization_id, agent_id)
        if current is None:
            return None
        if not current.get("has_draft"):
            raise VersionState(
                "NO_DRAFT",
                "It's published, and published versions can't change: "
                "start a new version to edit it.",
            )
        if current["revision"] != revision:
            raise self.conflict(agent_id)
        moment = now()
        stored = self.checked(
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
                # A record from before versions gets them as it's saved.
                "has_draft": True,
                "draft_version": current["draft_version"],
                "published_version": current["published_version"],
                "published_at": current["published_at"],
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
        if draft is True:
            # A record from before versions has no has_draft: it's a draft.
            where["has_draft"] = {"$ne": False}
        elif draft is False:
            where["has_draft"] = False
        found = await collection.find_one_and_update(
            where,
            {"$set": changes, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if found is None:
            raise self.conflict(agent_id)
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
        Freeze the draft as its version. Check it can be published first:
        this doesn't.

        :return: The document, its draft now published; None when there's no such one.
        :raises VersionState: ``NO_DRAFT``: there's nothing to publish.
        :raises Exception: ``conflict``: the draft was saved since ``revision``.
        """
        current = await self.get(organization_id, agent_id)
        if current is None:
            return None
        if not current.get("has_draft"):
            raise VersionState(
                "NO_DRAFT", "There's no draft to publish: start a new version first."
            )
        if current["revision"] != revision:
            raise self.conflict(agent_id)
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
            raise self.conflict(agent_id) from error
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
        except self.conflict:
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
        :return: The document with a new draft (the next version), copied from
            ``from_version`` or the latest published; None when there's no such
            document or version.
        :raises VersionState: ``DRAFT_EXISTS`` or ``NOT_PUBLISHED``.
        """
        current = await self.get(organization_id, agent_id)
        if current is None:
            return None
        if current.get("has_draft"):
            raise VersionState(
                "DRAFT_EXISTS",
                "It has a draft already: edit that, or discard it first.",
            )
        latest = current.get("published_version")
        if latest is None:
            raise VersionState(
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
        :return: The document back at its latest published version; None when
            there's no such one.
        :raises VersionState: ``NO_DRAFT``, or ``NOT_PUBLISHED`` (delete it instead).
        """
        current = await self.get(organization_id, agent_id)
        if current is None:
            return None
        if not current.get("has_draft"):
            raise VersionState("NO_DRAFT", "There's no draft to discard.")
        latest = current.get("published_version")
        if latest is None:
            raise VersionState(
                "NOT_PUBLISHED",
                f"It has never been published: delete the {self.noun} instead.",
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
        """:return: One of its published versions, or None."""
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
        """:return: Its published versions, newest first, without their documents."""
        versions = await self._versions_ready()
        cursor = versions.find(
            {"agent_id": agent_id, "organization_id": organization_id}, {"document": 0}
        ).sort("version", -1)
        return await cursor.to_list()

    async def find(self, agent_id: str) -> dict[str, Any] | None:
        """:return: A document by its ID alone, for the runtime; None when it's gone."""
        collection = await self._ready()
        return versioned(
            await collection.find_one({"_id": agent_id, "deleted_at": None})
        )

    async def find_version(self, agent_id: str, version: int) -> dict[str, Any] | None:
        """:return: A published version by the document's ID alone, for the runtime."""
        versions = await self._versions_ready()
        return await versions.find_one({"agent_id": agent_id, "version": version})

    async def delete_organization(self, organization_id: str) -> int:
        versions = await self._versions_ready()
        await versions.delete_many({"organization_id": organization_id})
        return await super().delete_organization(organization_id)
