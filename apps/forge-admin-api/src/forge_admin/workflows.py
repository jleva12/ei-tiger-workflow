"""
Organizations' workflows: the ``forge.workflow/v1`` documents the web console's
builder saves as they're edited, kept in MongoDB, one per workflow.

A workflow belongs to its organization, so everyone in the organization reads
and changes the same one. The document is stored as the builder sends it
(MongoDB keeps its keys in order, which JSON Schemas in it rely on), checked
against the format's JSON Schema, with its ``id``, ``organization_id`` and
times set here.

Every save names the revision it was made from; a save from an older one
is refused (:class:`WorkflowConflict`), so two people's changes never
silently overwrite each other. Deleting keeps the record, marked deleted,
out of every read; IDs are never reused.
"""

import json
import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pymongo import ASCENDING, IndexModel

from forge_admin.document_store import (
    DocumentStore,
    fill_added_settings,
    iso,
    load_validator,
    new_id,
    problems_in,
)

logger = logging.getLogger(__name__)

COLLECTION = "workflows"
FORMAT = "forge.workflow/v1"
# The builder's IDs and this module's: wf_ and lowercase letters and digits.
ID_PATTERN = r"^wf_[a-z0-9]{6,40}$"
# The most schema problems a refusal names.
MAX_PROBLEMS = 5

SCHEMA_PATH = Path(__file__).with_name("workflow.schema.json")
# Settings a kind of step gained after workflows were stored, and what a
# document saved before them means by leaving them out. The schema requires
# every setting, so they're filled in when such a document is saved again
# (the web's document.ts fills them in when the builder opens one).
ADDED_SETTINGS: dict[str, dict[str, Any]] = {
    "agent": {"thinking_level": ""},
    "http": {"output_schema": {}},
}


class WorkflowError(Exception):
    """A workflow the API won't save; the message says why."""


class WorkflowConflict(Exception):
    """The workflow was saved by someone else since the revision given."""


class WorkflowIdTaken(Exception):
    """A new workflow's ID is already used, by this organization or another."""


@lru_cache
def _validator() -> Draft202012Validator:
    # Generated from the web's src/lib/workflows/schema.ts
    # (npm run generate:workflow-schema), so both check the same format.
    return load_validator(SCHEMA_PATH)


def with_added_settings(document: dict[str, Any]) -> dict[str, Any]:
    """
    A document with the settings its steps' kinds gained since it was saved
    (``ADDED_SETTINGS``): a stored document is sent back as it was kept, by
    the builder or when the Events page links an event type to it. Nothing
    it has is changed.

    :param document: A workflow document.
    :return: The document; the same object when nothing was missing.
    """
    return fill_added_settings(document, ADDED_SETTINGS)


def new_workflow_id() -> str:
    """:return: A new workflow ID, ``wf_`` and ten random letters and digits."""
    return new_id("wf_")


def checked_document(
    document: dict[str, Any],
    *,
    workflow_id: str,
    organization_id: str,
    created_at: datetime,
    updated_at: datetime,
    max_bytes: int,
) -> dict[str, Any]:
    """
    The document to store: the one given with its ID, organization and times set
    here, checked against the workflow format.

    :param document: The document the builder sent.
    :param workflow_id: The workflow's ID.
    :param organization_id: The organization it belongs to.
    :param created_at: When the workflow was made.
    :param updated_at: When this version was saved.
    :param max_bytes: The largest document accepted, in bytes of JSON.
    :return: The document, keys in the order given.
    :raises WorkflowError: When it's too large or isn't a workflow.
    """
    stored = {
        **with_added_settings(document),
        "id": workflow_id,
        "organization_id": organization_id,
        "created_at": iso(created_at),
        "updated_at": iso(updated_at),
    }
    size = len(json.dumps(stored, ensure_ascii=False).encode())
    if size > max_bytes:
        raise WorkflowError(
            f"The workflow is {size:,} bytes; the most a workflow can be is {max_bytes:,}."
        )
    problems = schema_problems(stored)
    if problems:
        named = problems[:MAX_PROBLEMS]
        more = len(problems) - len(named)
        raise WorkflowError(
            "It isn't a forge.workflow/v1 document. "
            + "; ".join(named)
            + (f"; and {more} more." if more else ".")
        )
    return stored


def schema_problems(document: dict[str, Any]) -> list[str]:
    """
    :param document: A workflow document.
    :return: Every way it doesn't fit the format's JSON Schema, each with
        where (``nodes/3/config/url: ...``), in document order.
    """
    return problems_in(_validator(), document, whole="The workflow")


class WorkflowStore(DocumentStore):
    """
    The workflows collection. Records look like::

        {"_id": "wf_…", "organization_id", "revision": 3, "document": {…},
         "created_at", "created_by", "updated_at", "updated_by",
         "updated_by_name", "deleted_at": None}

    Reads never return deleted records. Every read and write is by organization:
    a workflow is found only through the organization it belongs to.
    """

    collection_name = COLLECTION
    extra_indexes = (
        # The workflows whose start step names an organization's event type,
        # for starting runs from events.
        IndexModel(
            [
                ("organization_id", ASCENDING),
                ("document.nodes.config.event_types", ASCENDING),
            ],
            name="organization_event_triggers",
        ),
    )
    conflict = WorkflowConflict
    id_taken = WorkflowIdTaken
