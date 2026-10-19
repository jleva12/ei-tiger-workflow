"""
Organizations' team UI components (``forge.ui_component/v1``) in MongoDB:
compositions of the base components chats draw, which their agents' Rich
answers nodes pick by key (``forge_agent_runtime.generative_ui``).

A component is versioned as agents and workflows are
(``forge_admin.adk_workflows.versioned_store``): a draft the editor saves
from the revision it was made from (an older one is refused,
:class:`UiComponentConflict`), published as version N in
``ui_component_versions``, which nothing changes afterwards, and a new
draft (the next version) copied from any published one. Restoring an old
version is that: a new version copied from it.

- **Key**: ``org.…``, unique in its organization across archived components
  too (``organization_key``), so a key always means one component; it
  never changes once made.
- **Draft**: only its format is checked on save (:func:`checked_definition`);
  a draft that can't be drawn yet is kept all the same. Publishing checks it
  whole first (``check_definition``); the store doesn't.
- **Disabled**: set on the record beside its versions (:meth:`UiComponentStore.set_disabled`),
  like a chat page; it isn't a change to the component. A disabled component
  is in no organization catalog and no build until it's enabled again.
- **Archived**: deleted as documents are: out of every read, its key kept.

Agents use each component's latest published version from their next turn,
as they use their saved agents' and skills' latest.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from forge_agent_runtime.generative_ui import (
    FORMAT,
    Diagnostic,
    UiComponentDefinition,
)
from pydantic import ValidationError
from pymongo import ASCENDING, IndexModel, ReturnDocument
from pymongo.errors import DuplicateKeyError

from forge_admin.adk_workflows.document_store import now
from forge_admin.adk_workflows.versioned_store import (
    VersionedDocumentStore,
    VersionState,
    status_of,
    versioned,
)

COLLECTION = "ui_components"
VERSIONS = "ui_component_versions"
ID_PATTERN = r"^uic_[a-z0-9]{6,40}$"
#: Kept on the record only: not the component's to say.
_NOT_KEPT = ("version", "id", "organization_id")


class UiComponentError(Exception):
    """
    A document that isn't a team component, or one that changes its key.

    :ivar diagnostics: What's wrong, and where.
    """

    def __init__(
        self, message: str, diagnostics: list[Diagnostic] | None = None
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or []


class UiComponentConflict(Exception):
    """A save made from an older revision of the draft."""


class UiComponentTaken(Exception):
    """A new component's ID is used already."""


class UiComponentKeyTaken(Exception):
    """The organization has (or had) a component with the key already."""


#: The component isn't in the state the change needs (``NO_DRAFT``,
#: ``DRAFT_EXISTS``, ``NOT_PUBLISHED``), as for any versioned document.
UiComponentState = VersionState


def checked_definition(
    document: Mapping[str, Any], *, max_bytes: int
) -> dict[str, Any]:
    """
    :return: The definition to keep: its format's fields, as the editor sent
        them; the version is the store's to set.
    :raises UiComponentError: It isn't a ``forge.ui_component/v1`` definition,
        or is larger than ``max_bytes``.
    """
    kept = {key: value for key, value in document.items() if key not in _NOT_KEPT}
    try:
        definition = UiComponentDefinition.model_validate(kept)
    except ValidationError as error:
        diagnostics = [
            Diagnostic(
                "definition_invalid",
                ".".join(str(part) for part in each["loc"]),
                each["msg"],
            )
            for each in error.errors()
        ]
        raise UiComponentError(
            f"It isn't a {FORMAT} definition: " + "; ".join(map(str, diagnostics)),
            diagnostics,
        ) from None
    stored = definition.model_dump(mode="json", exclude={"version"})
    size = len(json.dumps(stored, ensure_ascii=False).encode())
    if size > max_bytes:
        raise UiComponentError(
            f"The component is {size:,} bytes; the most one can be is {max_bytes:,}.",
            [
                Diagnostic(
                    "definition_too_large",
                    "",
                    f"it's {size} bytes: {max_bytes} at most",
                )
            ],
        )
    return stored


class UiComponentStore(VersionedDocumentStore):
    """
    The organizations' team components and their published versions:
    ``ui_components``, and ``ui_component_versions`` (``uic_…@1``).
    """

    collection_name = COLLECTION
    versions_name = VERSIONS
    id_prefix = "uic_"
    noun = "component"
    conflict = UiComponentConflict
    id_taken = UiComponentTaken
    extra_indexes = (
        # A key is one component in its organization, archived ones too.
        IndexModel(
            [("organization_id", ASCENDING), ("document.key", ASCENDING)],
            name="organization_key",
            unique=True,
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
        """:raises UiComponentError: It isn't a team component, or is too large."""
        return checked_definition(document, max_bytes=max_bytes)

    async def create(self, record: dict[str, Any]) -> dict[str, Any]:
        """:raises UiComponentKeyTaken: The organization has a component with its key."""
        collection = await self._ready()
        try:
            await collection.insert_one(record)
        except DuplicateKeyError as error:
            pattern = (error.details or {}).get("keyPattern") or {}
            if "document.key" in pattern:
                raise UiComponentKeyTaken(record["document"]["key"]) from error
            raise self.id_taken(record["_id"]) from error
        return record

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
        """:raises UiComponentError: Also when the draft changes the component's key."""
        current = await self.get(organization_id, agent_id)
        if current is None:
            return None
        if document.get("key") != current["document"].get("key"):
            raise UiComponentError(
                f"A component's key never changes: it's {current['document'].get('key')}. "
                "Make a new component for another key.",
                [Diagnostic("key_immutable", "key", "a component's key never changes")],
            )
        return await super().save_draft(
            organization_id,
            agent_id,
            revision,
            document,
            by=by,
            by_name=by_name,
            max_bytes=max_bytes,
        )

    async def set_disabled(
        self,
        organization_id: str,
        component_id: str,
        *,
        disabled: bool,
        by: str,
        by_name: str | None,
    ) -> dict[str, Any] | None:
        """
        Take the component out of its organization's catalog and builds, or
        put it back. It isn't a change to the component: its revision and
        versions stay as they are.

        :return: The record; None when there's no such one.
        """
        collection = await self._ready()
        return versioned(
            await collection.find_one_and_update(
                {
                    "_id": component_id,
                    "organization_id": organization_id,
                    "deleted_at": None,
                },
                {
                    "$set": {
                        "disabled": disabled,
                        "disabled_changed_at": now(),
                        "disabled_changed_by": by,
                        "disabled_changed_by_name": by_name,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
        )

    async def live(
        self, organization_id: str
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """
        :return: The organization's components its agents may use, by key:
            each published and enabled one's record and its latest published
            version's definition (``version`` set).
        """
        return await self._live(organization_id, await self.list(organization_id))

    async def find_live(
        self, organization_id: str, keys: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """
        :return: Of these keys, those the organization's agents may use,
            each its latest published version's definition (``version`` set):
            what the hosted runtime builds them with.
        """
        collection = await self._ready()
        cursor = collection.find(
            {
                "organization_id": organization_id,
                "deleted_at": None,
                "document.key": {"$in": list(keys)},
            }
        )
        records = [
            record for record in map(versioned, await cursor.to_list()) if record
        ]
        return {
            str(record["document"]["key"]): document
            for record, document in await self._live(organization_id, records)
        }

    async def stamps(
        self, organization_id: str, keys: Sequence[str]
    ) -> dict[str, tuple[int | None, bool]]:
        """
        :return: Of these keys, the organization's components' latest
            published version and whether each is disabled: what changes
            when what agents build with does (drafts don't). A key with none
            (archived, or never made) isn't in it.
        """
        collection = await self._ready()
        cursor = collection.find(
            {
                "organization_id": organization_id,
                "deleted_at": None,
                "document.key": {"$in": list(keys)},
            },
            {"document.key": 1, "published_version": 1, "disabled": 1},
        )
        return {
            str(record["document"]["key"]): (
                record.get("published_version"),
                bool(record.get("disabled")),
            )
            for record in await cursor.to_list()
        }

    async def _live(
        self, organization_id: str, records: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        records = [
            record
            for record in records
            if record.get("published_version") is not None
            and not record.get("disabled")
        ]
        # A record without a draft holds its latest published version; one
        # with a draft, the draft: its live version is in the versions.
        drafting = sorted(
            f"{record['_id']}@{record['published_version']}"
            for record in records
            if record.get("has_draft")
        )
        published: dict[str, dict[str, Any]] = {}
        if drafting:
            versions = await self._versions_ready()
            cursor = versions.find(
                {"_id": {"$in": drafting}, "organization_id": organization_id}
            )
            published = {
                found["agent_id"]: found["document"] for found in await cursor.to_list()
            }
        live = []
        for record in records:
            document = (
                published.get(record["_id"])
                if record.get("has_draft")
                else {**record["document"], "version": record["published_version"]}
            )
            if document is not None:
                live.append((record, document))
        return sorted(live, key=lambda each: str(each[0]["document"].get("key")))


__all__ = [
    "UiComponentConflict",
    "UiComponentError",
    "UiComponentKeyTaken",
    "UiComponentState",
    "UiComponentStore",
    "UiComponentTaken",
    "checked_definition",
    "status_of",
]
