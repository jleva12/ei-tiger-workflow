"""
Organizations' documents in MongoDB: what the workflows (``workflows.py``) and
agents (``agent_documents.py``) the web console's builders save have in common.

Each is one record per document, found only through its organization. The
document is stored as the builder sends it (MongoDB keeps its keys in order,
which JSON Schemas in it rely on), checked against its format's JSON Schema,
with its ``id``, ``organization_id`` and times set here.

Every save names the revision it was made from; a save from an older one is
refused, so two people's changes never silently overwrite each other.
Deleting keeps the record, marked deleted, out of every read; IDs are never
reused.
"""

import copy
import json
import secrets
import string
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Self

from jsonschema import Draft202012Validator
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, IndexModel, ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError

from forge_admin.config import Settings

_ID_ALPHABET = string.ascii_lowercase + string.digits


def now() -> datetime:
    """:return: The time, to the millisecond, as MongoDB keeps it."""
    moment = datetime.now(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def iso(moment: datetime) -> str:
    """:return: A time as a document holds it, ``2026-09-26T09:30:00.000Z``."""
    return (
        moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def new_id(prefix: str) -> str:
    """:return: A new ID: the prefix and ten random lowercase letters and digits."""
    return prefix + "".join(secrets.choice(_ID_ALPHABET) for _ in range(10))


def load_validator(path: Path) -> Draft202012Validator:
    """
    :param path: A format's JSON Schema (draft 2020-12).
    :return: Its validator.
    """
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def problems_in(
    validator: Draft202012Validator, document: dict[str, Any], *, whole: str
) -> list[str]:
    """
    :param validator: The format's validator.
    :param document: A document.
    :param whole: What a problem with the whole document is said of.
    :return: Every way it doesn't fit the format, each with where
        (``nodes/3/config/url: ...``), in document order.
    """
    found = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    return [
        f"{'/'.join(map(str, problem.absolute_path)) or whole}: {problem.message}"
        for problem in found
    ]


def fill_added_settings(
    document: dict[str, Any], added: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """
    A document with the settings its nodes' kinds gained since it was saved.
    Nothing it has is changed.

    :param document: A document with ``nodes``, each with its ``kind`` and
        ``config``.
    :param added: By kind, the settings it gained and what a document saved
        before them means by leaving them out.
    :return: The document; the same object when nothing was missing.
    """
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        return document
    filled = []
    for node in nodes:
        config = node.get("config") if isinstance(node, dict) else None
        kind = node.get("kind") if isinstance(node, dict) else None
        if not isinstance(config, dict) or not isinstance(kind, str):
            filled.append(node)
            continue
        missing = {
            key: copy.deepcopy(value)
            for key, value in added.get(kind, {}).items()
            if key not in config
        }
        filled.append({**node, "config": {**config, **missing}} if missing else node)
    if all(a is b for a, b in zip(filled, nodes, strict=True)):
        return document
    return {**document, "nodes": filled}


class DocumentStore:
    """
    A collection of organizations' documents. Records look like::

        {"_id": "…", "organization_id", "revision": 3, "document": {…},
         "created_at", "created_by", "updated_at", "updated_by",
         "updated_by_name", "deleted_at": None}

    Reads never return deleted records. Every read and write is by organization:
    a document is found only through the organization it belongs to.

    A subclass names its collection, the indexes it needs besides an
    organization's most recently changed first, and what it raises.
    """

    #: The collection.
    collection_name: ClassVar[str]
    #: Its indexes besides ``organization_recent``.
    extra_indexes: ClassVar[Sequence[IndexModel]] = ()
    #: Raised with the ID when a save was made from an older revision.
    conflict: ClassVar[type[Exception]]
    #: Raised with the ID when a new document's ID is used already.
    id_taken: ClassVar[type[Exception]]

    def __init__(self, client: AsyncMongoClient, database: str) -> None:
        self._client = client
        self._collection: AsyncCollection = client[database][self.collection_name]
        self._indexed = False

    @classmethod
    def from_settings(cls, settings: Settings) -> Self | None:
        """
        :param settings: The application settings.
        :return: The store, or None when MongoDB isn't set up. The client
            connects on first use, so the API starts while MongoDB is down.
        """
        if settings.mongo_uri is None:
            return None
        timeout_ms = int(settings.mongo_timeout * 1000)
        client: AsyncMongoClient = AsyncMongoClient(
            settings.mongo_uri.get_secret_value(),
            appname="forge-admin",
            tz_aware=True,
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
        )
        return cls(client, settings.mongo_database)

    async def aclose(self) -> None:
        """Close the client's connections."""
        await self._client.close()

    async def _ready(self) -> AsyncCollection:
        # Indexes once per process, on first use rather than at startup, so
        # MongoDB being down doesn't stop the API from starting.
        if not self._indexed:
            await self._collection.create_indexes(
                [
                    # An organization's documents, most recently changed first.
                    IndexModel(
                        [
                            ("organization_id", ASCENDING),
                            ("deleted_at", ASCENDING),
                            ("updated_at", DESCENDING),
                        ],
                        name="organization_recent",
                    ),
                    *self.extra_indexes,
                ]
            )
            self._indexed = True
        return self._collection

    async def list(self, organization_id: str) -> list[dict[str, Any]]:
        """:return: The organization's documents, most recently changed first."""
        collection = await self._ready()
        cursor = collection.find(
            {"organization_id": organization_id, "deleted_at": None}
        ).sort([("updated_at", DESCENDING), ("_id", ASCENDING)])
        return await cursor.to_list()

    async def get(
        self, organization_id: str, document_id: str
    ) -> dict[str, Any] | None:
        """:return: The organization's document, or None when it has no such one."""
        collection = await self._ready()
        return await collection.find_one(
            {"_id": document_id, "organization_id": organization_id, "deleted_at": None}
        )

    async def create(self, record: dict[str, Any]) -> dict[str, Any]:
        """
        :param record: The whole record, ``_id`` included.
        :return: The record as stored.
        :raises Exception: ``id_taken`` when its ID is used already.
        """
        collection = await self._ready()
        try:
            await collection.insert_one(record)
        except DuplicateKeyError as error:
            raise self.id_taken(record["_id"]) from error
        return record

    async def replace(
        self,
        organization_id: str,
        document_id: str,
        revision: int,
        changes: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        Save a new version, made from ``revision``.

        :param organization_id: The organization.
        :param document_id: The document.
        :param revision: The revision the new version was made from.
        :param changes: The fields to set: the document and who saved it when.
        :return: The record as now stored, at the next revision; None when
            the organization has no such document.
        :raises Exception: ``conflict`` when it was saved since ``revision``.
        """
        collection = await self._ready()
        found = await collection.find_one_and_update(
            {
                "_id": document_id,
                "organization_id": organization_id,
                "deleted_at": None,
                "revision": revision,
            },
            {"$set": changes, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if found is not None:
            return found
        if await self.get(organization_id, document_id) is None:
            return None
        raise self.conflict(document_id)

    async def delete(self, organization_id: str, document_id: str, *, by: str) -> bool:
        """
        Mark the document deleted: it leaves every read, and its ID is
        never used again.

        :return: Whether the organization had the document.
        """
        collection = await self._ready()
        result = await collection.update_one(
            {
                "_id": document_id,
                "organization_id": organization_id,
                "deleted_at": None,
            },
            {"$set": {"deleted_at": datetime.now(UTC), "deleted_by": by}},
        )
        return result.matched_count == 1

    async def delete_organization(self, organization_id: str) -> int:
        """
        Remove every document of an organization that no longer exists, deleted
        ones too.

        :return: How many were removed.
        """
        collection = await self._ready()
        result = await collection.delete_many({"organization_id": organization_id})
        return result.deleted_count
