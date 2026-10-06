"""The knowledge base tools: what an assistant needs to find an
organization's knowledge bases, see what's in them and how their documents'
ingestion went, and answer from them, as the person it's talking to.

Every tool calls the admin API's own routes as the conversation's user
(``person_api.py``), whom the ``/agents`` routes checked is the signed-in
person: it reads only the knowledge bases of organizations they may read
(``organizations:read``), and retries or re-indexes only where they may
manage them (``knowledge_bases:manage``), exactly as on the knowledge base
pages, each change audited as theirs.

A search answers as a chat agent's knowledge base tool does (``{searched,
passages}``, each passage with its ref), so the assistant cites the passages
it answers from and the web console draws the citations as sources
(``@forge-ui`` ``knowledgeBaseSources``). Uploading, filing and removing
documents, and creating or deleting knowledge bases, stay with the person on
the knowledge base pages. As a :class:`ForgeBaseToolset`, no tool raises: a
refused call answers ``failed`` with the API's reason and how to go on.
"""

import logging
from typing import Any

from fastapi import FastAPI
from forge_common.adk import ToolFailure
from google.adk.tools.tool_context import ToolContext

from forge_admin.assistant.person_api import PersonApi
from forge_admin.assistant.route_tools import RouteToolset, given, pick, uuid_arg

logger = logging.getLogger(__name__)

# The tools, by name: those that read, then those that change something.
READ_TOOLS = (
    "list_knowledge_bases",
    "get_knowledge_base",
    "list_knowledge_documents",
    "get_knowledge_document",
    "search_knowledge_bases",
)
CHANGE_TOOLS = (
    "retry_knowledge_document",
    "reindex_knowledge_base",
)
TOOL_NAMES = frozenset(READ_TOOLS + CHANGE_TOOLS)

# Added to the agent's instruction while it has these tools.
INSTRUCTION = """
Knowledge bases:
- An organization's knowledge bases are named sets of documents (PDFs, Word, \
PowerPoint, Excel, Markdown, ...) that its agents search. The knowledge base \
tools list them and their documents and search them, as the person.
- To answer a question from the documents, call search_knowledge_bases with \
the person's question: in the knowledge bases they name, or the one on their \
page, or else all of the organization's. Answer only from the passages it \
returns. When it finds none, say the knowledge bases don't cover it; never \
answer from what you know as if the documents said it.
- Cite each fact with the ref of the passage it comes from, in square \
brackets right after it and nothing else: "Appeals are due within 180 days \
[KQM4821]." or "... [KQM4821, BTR0042]." No "Cite:" or "Source:" before \
them; exactly the refs the search returned. Keep them in your report, next \
to their facts, with each passage's document and knowledge base; the \
assistant keeps them in its answer, where they become numbered sources.
- A document's phase is where its ingestion is: QUEUED or RUNNING while it's \
read, split and embedded; SUCCEEDED when it's searchable; FAILED, with the \
error saying why (retry_knowledge_document tries again); MISSING when the \
worker no longer has its job (retrying it works too).
- A knowledge base's stale documents were embedded with another model than \
searches use: they're found by their words only, not their meaning, until \
reindex_knowledge_base ingests them again.
- Uploading, filing and removing documents, and creating or deleting \
knowledge bases, aren't yours: the person does it on the knowledge base's \
page.
"""

# The most documents one list shows.
MAX_DOCUMENTS = 100
# The phases a document's ingestion can be in.
PHASES = frozenset({"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "MISSING"})


class KnowledgeToolset(RouteToolset):
    """
    Tools to list, read and search an organization's knowledge bases and
    their documents, and to retry or re-index their ingestion, as the person
    the assistant is talking to. None uploads or removes anything.

    :param api: The admin API, called as the person.
    :param confirm_changes: Ask the person to confirm each change first.
    :param kwargs: :class:`ForgeBaseToolset`'s (``max_result_chars``,
        ``tool_filter``, ``tool_name_prefix``, ...).
    """

    READ_TOOLS = READ_TOOLS
    CHANGE_TOOLS = CHANGE_TOOLS
    STATUS_FIXES = {
        403: [
            "The person lacks the permission this needs in the organization (the "
            "reason names it): reading needs organizations:read, retrying and "
            "re-indexing knowledge_bases:manage. Tell them, and who can do it; "
            "don't retry."
        ],
        404: [
            "Check the IDs: list_knowledge_bases for the organization's knowledge "
            "bases, list_knowledge_documents for a knowledge base's documents; the "
            "organization's ID is on the page or from list_my_memberships."
        ],
        409: [
            "Only a document whose ingestion FAILED or is MISSING can be retried: "
            "get_knowledge_document says its phase. Tell the person what it is."
        ],
        503: [
            "Searching, or the worker that ingests documents, isn't available right "
            "now: tell the person and try later."
        ],
    }

    async def list_knowledge_bases(
        self, organization_id: str, tool_context: ToolContext
    ) -> list[Any]:
        """
        The organization's knowledge bases, by name: each one's ID, what it
        holds, and its documents: how many, how many are searchable (ready),
        failed, or stale (embedded with another model than searches use).

        Args:
            organization_id: The organization's ID, from the page or
                list_my_memberships.
        """
        found = await self._call(tool_context, "GET", _bases(organization_id))
        return [pick(kb, KNOWLEDGE_BASE_FIELDS) for kb in found or []]

    async def get_knowledge_base(
        self, organization_id: str, knowledge_base_id: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        One knowledge base: what it holds, its documents' counts and bytes,
        the model searches embed questions with, and who last changed it.

        Args:
            organization_id: The organization's ID.
            knowledge_base_id: The knowledge base's ID, from the page or
                list_knowledge_bases.
        """
        found = await self._call(
            tool_context, "GET", _base(organization_id, knowledge_base_id)
        )
        return pick(
            found,
            KNOWLEDGE_BASE_FIELDS + ("size_bytes", "embedding_model") + AUDIT_FIELDS,
        )

    async def list_knowledge_documents(
        self,
        organization_id: str,
        knowledge_base_id: str,
        tool_context: ToolContext,
        search: str | None = None,
        phase: str | None = None,
        limit: int = 25,
    ) -> list[Any]:
        """
        A knowledge base's documents, newest first: each one's ID, filename,
        where its ingestion is (phase), why it failed, its chunks, and who
        uploaded it when.

        Args:
            organization_id: The organization's ID.
            knowledge_base_id: The knowledge base's ID.
            search: Only those whose filename contains this, ignoring case.
            phase: Only those in this phase: QUEUED, RUNNING, SUCCEEDED,
                FAILED or MISSING.
            limit: How many to show, 1 to 100.
        """
        params: dict[str, Any] = {"limit": max(1, min(int(limit), MAX_DOCUMENTS))}
        if search and search.strip():
            params["q"] = search.strip()
        if phase:
            params["phase"] = _phase(phase)
        found = await self._call(
            tool_context,
            "GET",
            f"{_base(organization_id, knowledge_base_id)}/documents",
            params=params,
        )
        return [pick(document, DOCUMENT_FIELDS) for document in found or []]

    async def get_knowledge_document(
        self,
        organization_id: str,
        knowledge_base_id: str,
        document_id: str,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """
        One document of a knowledge base: its filename, type and size, where
        its ingestion is and why it failed, its chunks and the model they're
        embedded with, and who uploaded it when.

        Args:
            organization_id: The organization's ID.
            knowledge_base_id: The knowledge base's ID.
            document_id: The document's ID, from the page (its document search
                parameter) or list_knowledge_documents.
        """
        found = await self._call(
            tool_context,
            "GET",
            _document(organization_id, knowledge_base_id, document_id),
        )
        return pick(found, DOCUMENT_FIELDS + ("media_type", "sha256"))

    async def search_knowledge_bases(
        self,
        organization_id: str,
        query: str,
        tool_context: ToolContext,
        knowledge_base_ids: list[str] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """
        Search the organization's knowledge bases for the passages of their
        documents that answer a question, by meaning and by their words,
        ranked together, best first. Each passage has a ref to cite it by,
        e.g. [KQM4821], its document and knowledge base, the headings it's
        under, where in the document it is, and how relevant it is.

        Args:
            organization_id: The organization's ID.
            query: What to look for, as a question or the words the passages
                would use.
            knowledge_base_ids: Only these knowledge bases, from the page or
                list_knowledge_bases; every one of the organization's when
                left out.
            limit: The most passages, 1 to 20.
        """
        if not query or not query.strip():
            raise ToolFailure("Say what to look for in query.")
        ids = [uuid_arg(i, "knowledge_base_ids") for i in knowledge_base_ids or []]
        found = await self._call(
            tool_context,
            "POST",
            f"{_bases(organization_id)}/search",
            json=given(
                query=query.strip(),
                knowledge_base_ids=list(dict.fromkeys(ids)) or None,
                limit=max(1, min(int(limit), 20)),
            ),
        )
        searched = [kb.get("name") for kb in (found or {}).get("searched") or []]
        passages = [_passage(hit) for hit in (found or {}).get("hits") or []]
        result: dict[str, Any] = {
            "organization_id": uuid_arg(organization_id, "organization_id"),
            "searched": searched,
            "passages": passages,
        }
        if not passages:
            result["note"] = (
                "Nothing in these knowledge bases matches: say so, and don't "
                "answer as if they did."
            )
        return result

    async def retry_knowledge_document(
        self,
        organization_id: str,
        knowledge_base_id: str,
        document_id: str,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """
        Ingest a document whose ingestion FAILED or is MISSING again: it's
        read, split and embedded anew, and searchable once it SUCCEEDS.

        Args:
            organization_id: The organization's ID.
            knowledge_base_id: The knowledge base's ID.
            document_id: The document's ID.
        """
        found = await self._call(
            tool_context,
            "POST",
            f"{_document(organization_id, knowledge_base_id, document_id)}/retry",
        )
        return pick(found, DOCUMENT_FIELDS)

    async def reindex_knowledge_base(
        self,
        organization_id: str,
        knowledge_base_id: str,
        tool_context: ToolContext,
        stale_only: bool = True,
    ) -> dict[str, Any]:
        """
        Ingest a knowledge base's documents again, with the embedding model
        searches use and the current parsers and chunking: its stale
        documents, or every finished one. Each stays searchable as it was
        until its new chunks replace the old. Answers how many were submitted.

        Args:
            organization_id: The organization's ID.
            knowledge_base_id: The knowledge base's ID.
            stale_only: Only its stale documents (embedded with another
                model); false re-indexes every finished one, e.g. after its
                parsers or chunking changed.
        """
        found = await self._call(
            tool_context,
            "POST",
            f"{_base(organization_id, knowledge_base_id)}/reindex",
            json={"stale_only": bool(stale_only)},
        )
        return pick(found, ("submitted",))


def toolset(app: FastAPI, **kwargs: Any) -> KnowledgeToolset | None:
    """
    The knowledge base tools, calling ``app``'s routes as each
    conversation's person.

    :param app: The admin API's application.
    :param kwargs: :class:`KnowledgeToolset`'s options.
    :return: The toolset, or None without ``jwt_secret``, which the person's
        tokens are signed with.
    """
    if app.state.settings.jwt_secret is None:
        logger.warning(
            "FORGE_ADMIN_JWT_SECRET isn't set: the assistant can't read knowledge "
            "bases as the people it talks to"
        )
        return None
    return KnowledgeToolset(PersonApi.of_app(app), **kwargs)


# -- Paths, from IDs checked first -------------------------------------------


def _bases(organization_id: str) -> str:
    return (
        f"/organizations/{uuid_arg(organization_id, 'organization_id')}/knowledge-bases"
    )


def _base(organization_id: str, knowledge_base_id: str) -> str:
    return (
        f"{_bases(organization_id)}/{uuid_arg(knowledge_base_id, 'knowledge_base_id')}"
    )


def _document(organization_id: str, knowledge_base_id: str, document_id: str) -> str:
    base = _base(organization_id, knowledge_base_id)
    return f"{base}/documents/{uuid_arg(document_id, 'document_id')}"


def _phase(value: str) -> str:
    phase = str(value).strip().upper()
    if phase not in PHASES:
        raise ToolFailure(
            f"phase must be one of {', '.join(sorted(PHASES))}, not {value!r}"
        )
    return phase


# -- Summaries ---------------------------------------------------------------

AUDIT_FIELDS = ("created_at", "created_by", "updated_at", "updated_by")
KNOWLEDGE_BASE_FIELDS = (
    "id",
    "name",
    "description",
    "documents",
    "ready",
    "failed",
    "stale",
    "chunks",
)
DOCUMENT_FIELDS = (
    "id",
    "filename",
    "collection_id",
    "size_bytes",
    "phase",
    "error",
    "chunk_count",
    "embedding_model",
    "created_by",
    "created_at",
    "finished_at",
)


def _passage(hit: Any) -> dict[str, Any]:
    """A search hit as a knowledge base tool's passage, which chat UIs cite."""
    if not isinstance(hit, dict):
        return {}
    return {
        "ref": hit.get("ref"),
        "knowledge_base": hit.get("knowledge_base"),
        "knowledge_base_id": hit.get("knowledge_base_id"),
        "document": hit.get("filename"),
        "document_id": hit.get("document_id"),
        "section": " > ".join(hit.get("section_path") or []),
        "location": hit.get("location") or "",
        "text": hit.get("text"),
        "score": round(float(hit.get("score") or 0.0), 4),
    }
