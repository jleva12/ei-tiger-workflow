"""Documents uploaded to an organization's knowledge bases, for its chat
agents to search. Each upload is stored in the documents bucket
(``forge_admin.knowledge.storage``) and submitted to the async worker's
documents queue (``forge_admin.knowledge.queue``) with where it's stored; the
worker reads it back, parses, chunks and embeds it into the knowledge base's
chunks. A knowledge base files them in collections
(``knowledge_collections``), moves them between those, and removes them from
the bucket and the worker together. Its organization's members read each
file back as it was uploaded, for the web console's viewer.

Reading needs ``organizations:read`` in the organization; uploading, filing,
retrying and removing need ``knowledge_bases:manage``.
"""

import asyncio
import contextlib
import hashlib
import logging
import mimetypes
import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import IO, Annotated, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, StringConstraints
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import Audited, NodeId, Session, as_read
from forge_admin.auth.access import CurrentUser, Enforcer
from forge_admin.config import Settings
from forge_admin.db.audit import UtcDateTime
from forge_admin.knowledge.access import knowledge_base_of
from forge_admin.knowledge.queue import (
    KnowledgeQueue,
    Outcome,
    QueueError,
    document_job_key,
    outcome,
)
from forge_admin.knowledge.storage import DocumentStore, StorageError, document_key
from forge_admin.models import KnowledgeCollection, KnowledgeDocument
from forge_admin.models.knowledge import new_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge base documents"])

DocumentId = Annotated[str, Path(max_length=36)]
# Phases of a job that hasn't finished; the others are final.
ACTIVE = ("QUEUED", "RUNNING")
# Final phases whose document can be ingested again: its job failed, or the
# worker no longer has it.
RETRYABLE = ("FAILED", "MISSING")
Phase = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "MISSING"]
# Listing the documents in no collection.
UNFILED = "none"
# A collection's ID, or UNFILED.
CollectionFilter = Annotated[str, StringConstraints(max_length=36)]
# A file extension, without the dot.
Extension = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{1,16}$")]
NO_COLLECTION = "The knowledge base has no such collection"
# How long reading a list waits for the worker's answers.
REFRESH_TIMEOUT = 5.0
# Bytes read at a time while measuring an upload.
CHUNK = 1024 * 1024
# The media types of the formats the async worker reads, by extension:
# the platform's table misses some and misnames others (.mmd is karaoke).
_OOXML = "application/vnd.openxmlformats-officedocument"
MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": f"{_OOXML}.wordprocessingml.document",
    "docm": "application/vnd.ms-word.document.macroEnabled.12",
    "doc": "application/msword",
    "dot": "application/msword",
    "rtf": "application/rtf",
    "odt": "application/vnd.oasis.opendocument.text",
    "pptx": f"{_OOXML}.presentationml.presentation",
    "pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    "ppsx": f"{_OOXML}.presentationml.slideshow",
    "ppsm": "application/vnd.ms-powerpoint.slideshow.macroEnabled.12",
    "ppt": "application/vnd.ms-powerpoint",
    "pps": "application/vnd.ms-powerpoint",
    "pot": "application/vnd.ms-powerpoint",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "xlsx": f"{_OOXML}.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "xls": "application/vnd.ms-excel",
    "xlt": "application/vnd.ms-excel",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "mdx": "text/markdown",
    "txt": "text/plain",
    "text": "text/plain",
    "log": "text/plain",
    "vsdx": "application/vnd.ms-visio.drawing",
    "vsdm": "application/vnd.ms-visio.drawing.macroEnabled.12",
    "mmd": "text/plain",
    "mermaid": "text/plain",
}
# Media types a browser would run or render as a page of this API's origin if
# one were opened directly; they're sent as bytes instead.
ACTIVE_TYPES = frozenset(
    {
        "application/javascript",
        "application/xhtml+xml",
        "application/xml",
        "image/svg+xml",
        "text/html",
        "text/javascript",
        "text/xml",
    }
)
# Sent with every file: it's the uploader's, so it may neither run anything
# nor be sniffed as another type.
FILE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


class DocumentRead(Audited):
    id: str
    knowledge_base_id: str
    # The collection it's filed in; None while unfiled.
    collection_id: str | None
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    # The async worker's job as last read: QUEUED or RUNNING while it
    # ingests; SUCCEEDED, FAILED, or MISSING when the worker no longer has it.
    phase: str
    # Why it failed, e.g. that no parser reads its format.
    error: str
    # The chunks it was split into, once it succeeded.
    chunk_count: int
    finished_at: UtcDateTime | None


class DocumentUpdate(BaseModel):
    # Where to file it: a collection of the knowledge base, or None to unfile it.
    collection_id: str | None = None


class Totals(BaseModel):
    documents: int = 0
    size_bytes: int = 0
    # The chunks the worker made of the ones it ingested.
    chunks: int = 0
    # How many of them are searchable (SUCCEEDED), and how many FAILED.
    ready: int = 0
    failed: int = 0

    def add(self, row: "_SummaryRow") -> None:
        self.documents += 1
        self.size_bytes += row.size_bytes
        self.chunks += row.chunk_count
        self.ready += row.phase == "SUCCEEDED"
        self.failed += row.phase == "FAILED"


class Contributor(Totals):
    # Who uploaded them, as their audit column records it.
    user: str
    last_uploaded_at: UtcDateTime


class DocumentSummary(BaseModel):
    totals: Totals
    # By the phase of their job, e.g. ``SUCCEEDED``.
    by_phase: dict[str, Totals]
    # By their file's extension, lowercase and without the dot; ``""`` for
    # none. The extension picks the worker's parser.
    by_extension: dict[str, Totals]
    # Everyone who uploaded one, most documents first.
    contributors: list[Contributor]


class _SummaryRow(BaseModel):
    filename: str
    size_bytes: int
    chunk_count: int
    phase: str
    created_by: str
    created_at: datetime


class _TooLarge(Exception):
    pass


def _apply(row: KnowledgeDocument, result: Outcome) -> None:
    """Keep what a client needs to know of a job the worker reported."""
    row.phase = result.phase
    row.error = result.error
    if result.phase == "SUCCEEDED":
        row.chunk_count = int((result.detail or {}).get("chunk_count") or 0)
    if result.finished and row.finished_at is None:
        row.finished_at = datetime.now(UTC).replace(tzinfo=None)


async def refresh(
    session: AsyncSession,
    queue: KnowledgeQueue | None,
    rows: Sequence[KnowledgeDocument],
) -> None:
    """
    Read the unfinished documents' jobs from the worker, all at once and
    briefly, and save what changed. A worker that doesn't answer leaves them
    as they were; one that no longer has a job makes it MISSING.

    :param session: The request's database session.
    :param queue: The worker's client; None leaves the rows alone.
    :param rows: Documents; only unfinished ones are read.
    """
    active = [row for row in rows if row.phase in ACTIVE]
    if queue is None or not active:
        return

    async def read(row: KnowledgeDocument) -> Outcome:
        async with asyncio.timeout(REFRESH_TIMEOUT):
            return outcome(await queue.job(row.job_key))

    answers = await asyncio.gather(
        *(read(row) for row in active), return_exceptions=True
    )
    for row, answer in zip(active, answers, strict=True):
        if isinstance(answer, Outcome):
            _apply(row, answer)
        elif not isinstance(answer, QueueError | TimeoutError):
            raise answer
    if session.dirty:
        await session.commit()


async def _refresh_unfinished(
    session: AsyncSession, queue: KnowledgeQueue | None, knowledge_base_id: str
) -> None:
    """Refresh every unfinished document of the knowledge base, so phases are
    current before the database is asked about them."""
    if queue is None:
        return
    rows = await session.scalars(
        select(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.phase.in_(ACTIVE),
        )
    )
    await refresh(session, queue, list(rows))


def _clients(request: Request) -> tuple[DocumentStore, KnowledgeQueue]:
    store: DocumentStore | None = request.app.state.documents
    queue: KnowledgeQueue | None = request.app.state.knowledge_queue
    if store is None or queue is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Document uploads aren't set up: set FORGE_ADMIN_DOCUMENTS_BUCKET "
            "and FORGE_ADMIN_EMBEDDING_REDIS_URL",
        )
    return store, queue


def _filename(uploaded: str | None) -> str:
    # Browsers may send a path (C:\fakepath\a.docx); keep the last part,
    # without control characters.
    name = re.split(r"[\\/]", uploaded or "")[-1]
    name = "".join(c for c in name if c.isprintable()).strip()
    if not name:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Name the file you upload"
        )
    return name[:255]


def _key_name(filename: str) -> str:
    # Readable in the bucket, safe in any key; the name itself is on the row.
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip(".-")[-120:]
    return name or "file"


def _measure(body: IO[bytes], limit: int) -> tuple[int, str]:
    """:return: The file's size and SHA-256, reading it from the start."""
    body.seek(0)
    digest, size = hashlib.sha256(), 0
    while chunk := body.read(CHUNK):
        size += len(chunk)
        if size > limit:
            raise _TooLarge
        digest.update(chunk)
    return size, digest.hexdigest()


def _storage_refusal(error: StorageError) -> HTTPException:
    if error.code == "unreachable":
        logger.warning("Document storage unavailable: %s", error)
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Document storage is unavailable; try again",
        )
    if error.code == "NoSuchBucket":
        logger.error(
            "The documents bucket doesn't exist: create it, or set "
            "FORGE_ADMIN_S3_CREATE_BUCKET for local development"
        )
    else:
        logger.error("Document storage refused an upload: %s", error)
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY, "Document storage refused the upload"
    )


async def _collection_of(
    session: AsyncSession, knowledge_base_id: str, collection_id: str | None
) -> str | None:
    """
    :return: The collection, checked to be the knowledge base's; None for none.
    :raises HTTPException: 422 for another knowledge base's collection, or none.
    """
    if collection_id is None:
        return None
    found = await session.get(KnowledgeCollection, collection_id)
    if found is None or found.knowledge_base_id != knowledge_base_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, NO_COLLECTION)
    return found.id


async def _document_of(
    session: AsyncSession, knowledge_base_id: str, document_id: str
) -> KnowledgeDocument:
    """:raises HTTPException: 404 for another knowledge base's document, or none."""
    row = await session.get(KnowledgeDocument, document_id)
    if row is None or row.knowledge_base_id != knowledge_base_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "The knowledge base has no such document"
        )
    return row


def _extension(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower().lstrip(".")


def _served_type(filename: str, uploaded_as: str) -> str:
    """
    :return: The media type to send a file as: its extension's, else what it
        was uploaded as; bytes for anything a browser would run.
    """
    known = MEDIA_TYPES.get(_extension(filename))
    guessed = known or mimetypes.guess_type(filename)[0]
    media_type = (guessed or uploaded_as or "").split(";")[0].strip().lower()
    if not media_type or media_type in ACTIVE_TYPES:
        return "application/octet-stream"
    return media_type


def _disposition(filename: str, *, download: bool) -> str:
    """:return: A Content-Disposition naming the file, in any language."""
    kind = "attachment" if download else "inline"
    # An ASCII name for old clients, and the real one (RFC 6266 / 5987).
    ascii_name = re.sub(r'[^\x20-\x7e]|["\\]', "_", filename)
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _etag(row: KnowledgeDocument) -> str:
    # Its file never changes, so its digest names this version for good.
    return f'"{row.sha256}"'


def _literal(text: str) -> str:
    """:return: The text for a LIKE pattern: its wildcards match themselves."""
    return re.sub(r"([/%_])", r"/\1", text)


def _has_extension(extension: str) -> ColumnElement[bool]:
    return KnowledgeDocument.filename.ilike(f"%.{_literal(extension)}", escape="/")


@router.post(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/documents",
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    file: Annotated[UploadFile, File()],
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    collection_id: Annotated[str | None, Form(max_length=36)] = None,
) -> DocumentRead:
    """
    Upload a document to a knowledge base for its agents to search: Word, Excel,
    CSV, Markdown, text or Visio. It's stored, then the async worker
    reads it back and parses, chunks and embeds it into the knowledge
    base's chunks; read it back to follow its job.
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param file: The document, as multipart form data.
    :param request: The request, for the settings and the clients.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :param collection_id: The collection to file it in, a form field; none
        leaves it unfiled.
    :return: The document, its job queued.
    :raises HTTPException: 403 without ``knowledge_bases:manage`` in the organization; 404
        for a knowledge base that doesn't exist; 413 for a file over the size limit;
        422 for an empty or unnamed file, or a collection the knowledge base doesn't
        have; 502 when storage refuses it; 503 when uploads aren't set up, or
        storage or the worker is unavailable.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, manage=True
    )
    collection = await _collection_of(session, knowledge_base_id, collection_id or None)
    store, queue = _clients(request)
    settings: Settings = request.app.state.settings
    filename = _filename(file.filename)
    try:
        size, sha256 = await asyncio.to_thread(
            _measure, file.file, settings.documents_max_bytes
        )
    except _TooLarge:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Documents can be up to {settings.documents_max_bytes} bytes",
        ) from None
    if size == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The file is empty")

    document_id = new_id()
    # Under the knowledge base's place in the hierarchy, so any level lists it.
    key = document_key(
        organization_id=organization_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        name=_key_name(filename),
    )
    media_type = (file.content_type or "")[:255]
    try:
        await store.put(key, file.file, media_type=media_type)
    except StorageError as error:
        raise _storage_refusal(error) from None
    row = KnowledgeDocument(
        id=document_id,
        knowledge_base_id=knowledge_base_id,
        collection_id=collection,
        filename=filename,
        media_type=media_type,
        size_bytes=size,
        sha256=sha256,
        storage_uri=store.uri(key),
        job_key=document_job_key(knowledge_base_id, document_id),
        phase="QUEUED",
    )
    session.add(row)
    await session.commit()
    try:
        await queue.ingest_document(
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            uri=row.storage_uri,
            filename=filename,
            media_type=media_type,
            uploaded_by=user,
        )
    except QueueError as error:
        # Nothing half done: without its job, the upload never happened.
        logger.warning("The async worker's queue is unavailable: %s", error)
        await session.delete(row)
        await session.commit()
        with contextlib.suppress(StorageError):
            await store.delete(key)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The async worker is unavailable; try again",
        ) from None
    logger.info(
        "%s uploaded %s (%d bytes) to knowledge base %s: document %s",
        user,
        filename,
        size,
        knowledge_base_id,
        document_id,
    )
    return as_read(DocumentRead, row)


@router.get(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/documents"
)
async def list_documents(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    collection: Annotated[list[CollectionFilter] | None, Query()] = None,
    phase: Annotated[list[Phase] | None, Query()] = None,
    extension: Annotated[list[Extension] | None, Query()] = None,
    not_extension: Annotated[list[Extension] | None, Query()] = None,
    uploaded_by: Annotated[str | None, Query(max_length=255)] = None,
    q: Annotated[str | None, Query(max_length=255)] = None,
) -> list[DocumentRead]:
    """
    List a knowledge base's documents, most recent first, with unfinished jobs
    refreshed from the worker.
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param request: The request, for the worker's client.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :param limit: How many to return.
    :param offset: How many to skip, for the next page.
    :param collection: Only those filed in these collections (repeat it for
        several, e.g. a collection and those in it); ``none`` for the unfiled
        ones.
    :param phase: Only those whose job is in one of these phases.
    :param extension: Only files with one of these extensions (``md``),
        ignoring case.
    :param not_extension: Only files with none of these extensions.
    :param uploaded_by: Only those this user uploaded (``created_by``).
    :param q: Only those whose filename contains this, ignoring case.
    :return: The documents.
    """
    await knowledge_base_of(session, enforcer, user, organization_id, knowledge_base_id)
    queue: KnowledgeQueue | None = request.app.state.knowledge_queue
    query = select(KnowledgeDocument).where(
        KnowledgeDocument.knowledge_base_id == knowledge_base_id
    )
    if collection:
        filed = [c for c in collection if c != UNFILED]
        places = [KnowledgeDocument.collection_id.in_(filed)] if filed else []
        if UNFILED in collection:
            places.append(KnowledgeDocument.collection_id.is_(None))
        query = query.where(or_(*places))
    if phase:
        # Phases as the worker reports them now, not as last read.
        await _refresh_unfinished(session, queue, knowledge_base_id)
        query = query.where(KnowledgeDocument.phase.in_(phase))
    if extension:
        query = query.where(or_(*(_has_extension(e) for e in extension)))
    if not_extension:
        query = query.where(*(~_has_extension(e) for e in not_extension))
    if uploaded_by:
        query = query.where(KnowledgeDocument.created_by == uploaded_by)
    if q and q.strip():
        query = query.where(
            KnowledgeDocument.filename.ilike(f"%{_literal(q.strip())}%", escape="/")
        )
    rows = list(
        await session.scalars(
            query.order_by(KnowledgeDocument.created_at.desc(), KnowledgeDocument.id)
            .offset(offset)
            .limit(limit)
        )
    )
    await refresh(session, queue, rows)
    return [as_read(DocumentRead, row) for row in rows]


@router.get(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/documents/summary"
)
async def summarize_documents(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> DocumentSummary:
    """
    Sum up a knowledge base's documents: how many, how large and how many chunks, in
    all and by their job's phase, their file's extension and who uploaded
    them. Unfinished jobs are refreshed from the worker first.
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param request: The request, for the worker's client.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The summary.
    """
    await knowledge_base_of(session, enforcer, user, organization_id, knowledge_base_id)
    await _refresh_unfinished(
        session, request.app.state.knowledge_queue, knowledge_base_id
    )
    rows = [
        _SummaryRow.model_validate(row, from_attributes=True)
        for row in await session.execute(
            select(
                KnowledgeDocument.filename,
                KnowledgeDocument.size_bytes,
                KnowledgeDocument.chunk_count,
                KnowledgeDocument.phase,
                KnowledgeDocument.created_by,
                KnowledgeDocument.created_at,
            ).where(KnowledgeDocument.knowledge_base_id == knowledge_base_id)
        )
    ]
    totals = Totals()
    by_phase: defaultdict[str, Totals] = defaultdict(Totals)
    by_extension: defaultdict[str, Totals] = defaultdict(Totals)
    contributors: dict[str, Contributor] = {}
    for row in rows:
        totals.add(row)
        by_phase[row.phase].add(row)
        by_extension[_extension(row.filename)].add(row)
        uploaded_at = row.created_at.replace(tzinfo=row.created_at.tzinfo or UTC)
        who = contributors.setdefault(
            row.created_by,
            Contributor(user=row.created_by, last_uploaded_at=uploaded_at),
        )
        who.add(row)
        who.last_uploaded_at = max(who.last_uploaded_at, uploaded_at)
    return DocumentSummary(
        totals=totals,
        by_phase=dict(by_phase),
        by_extension=dict(by_extension),
        contributors=sorted(
            contributors.values(),
            key=lambda who: (-who.documents, -who.last_uploaded_at.timestamp()),
        ),
    )


@router.get(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
)
async def get_document(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    document_id: DocumentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> DocumentRead:
    """
    Read one document, its job refreshed from the worker while unfinished.
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param document_id: The document.
    :param request: The request, for the worker's client.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The document.
    :raises HTTPException: 404 for another knowledge base's document, or none.
    """
    await knowledge_base_of(session, enforcer, user, organization_id, knowledge_base_id)
    row = await _document_of(session, knowledge_base_id, document_id)
    await refresh(session, request.app.state.knowledge_queue, [row])
    return as_read(DocumentRead, row)


@router.get(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}/content",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"application/octet-stream": {}}},
        304: {"description": "The copy the caller has (If-None-Match) is current"},
    },
)
async def document_content(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    document_id: DocumentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    download: bool = False,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    """
    Read a document's file back exactly as it was uploaded, for the web
    console to show or save. Its ETag is its SHA-256, so a caller holding
    the file revalidates without reading it again.
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param document_id: The document.
    :param request: The request, for the store.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :param download: Ask the browser to save it (``attachment``) rather than
        show it (``inline``).
    :param if_none_match: The ETag of a copy the caller has.
    :return: The file, streamed; 304 when the caller's copy is current.
    :raises HTTPException: 403 without ``organizations:read`` in the organization; 404 for
        another knowledge base's document, or none, or one whose file is gone; 502 when
        storage refuses; 503 when uploads aren't set up, or storage is
        unavailable.
    """
    await knowledge_base_of(session, enforcer, user, organization_id, knowledge_base_id)
    row = await _document_of(session, knowledge_base_id, document_id)
    etag = _etag(row)
    headers = {
        "ETag": etag,
        # The caller's own copy, checked with the API each time it's used.
        "Cache-Control": "private, no-cache",
        **FILE_HEADERS,
    }
    if if_none_match and etag in {tag.strip() for tag in if_none_match.split(",")}:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    store: DocumentStore | None = request.app.state.documents
    if store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Document storage isn't set up: set FORGE_ADMIN_DOCUMENTS_BUCKET",
        )
    key = store.key_of(row.storage_uri)
    if key is None:
        logger.error(
            "Document %s is stored outside the documents bucket: %s",
            row.id,
            row.storage_uri,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The document's file is gone")
    try:
        stored = await store.open(key)
    except StorageError as error:
        if error.code in ("NoSuchKey", "404"):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "The document's file is gone"
            ) from None
        if error.code == "unreachable":
            logger.warning("Document storage unavailable: %s", error)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Document storage is unavailable; try again",
            ) from None
        logger.error("Document storage refused a read: %s", error)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Document storage refused the read"
        ) from None
    return StreamingResponse(
        stored.chunks(),
        media_type=_served_type(row.filename, row.media_type),
        headers={
            **headers,
            "Content-Length": str(stored.size),
            "Content-Disposition": _disposition(row.filename, download=download),
        },
    )


@router.patch(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
)
async def update_document(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    document_id: DocumentId,
    body: DocumentUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> DocumentRead:
    """
    File a document in one of the knowledge base's collections (``collection_id``),
    or unfile it (``null``). It stays searchable either way.
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param document_id: The document.
    :param body: The fields to change; absent ones are left alone.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The document.
    :raises HTTPException: 403 without ``knowledge_bases:manage`` in the organization; 404
        for another knowledge base's document, or none; 422 for a collection the knowledge base
        doesn't have.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, manage=True
    )
    row = await _document_of(session, knowledge_base_id, document_id)
    if "collection_id" in body.model_fields_set:
        row.collection_id = await _collection_of(
            session, knowledge_base_id, body.collection_id
        )
    await session.commit()
    return as_read(DocumentRead, row)


@router.post(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_document(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    document_id: DocumentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> DocumentRead:
    """
    Ingest a document again whose job failed, or that the worker no longer
    has: its stored file is submitted to the async worker once more,
    under its job's key and as its uploader, so the new job replaces the
    failed one. It's queued again; read it back to follow its job.
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param document_id: The document.
    :param request: The request, for the clients.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The document, its job queued.
    :raises HTTPException: 403 without ``knowledge_bases:manage`` in the organization; 404
        for another knowledge base's document, or none; 409 unless its job failed or
        is missing; 503 when uploads aren't set up, or the worker is
        unavailable.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, manage=True
    )
    row = await _document_of(session, knowledge_base_id, document_id)
    _, queue = _clients(request)
    await refresh(session, queue, [row])
    if row.phase not in RETRYABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only a document whose ingestion failed can be retried",
        )
    try:
        # A job still queued or running under the key isn't submitted again;
        # the document then follows that one.
        await queue.ingest_document(
            knowledge_base_id=knowledge_base_id,
            document_id=row.id,
            uri=row.storage_uri,
            filename=row.filename,
            media_type=row.media_type,
            uploaded_by=row.created_by,
        )
    except QueueError as error:
        logger.warning("The async worker's queue is unavailable: %s", error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The async worker is unavailable; try again",
        ) from None
    row.phase, row.error, row.chunk_count, row.finished_at = "QUEUED", "", 0, None
    await session.commit()
    logger.info(
        "%s retried ingesting %s for knowledge base %s: document %s",
        user,
        row.filename,
        knowledge_base_id,
        row.id,
    )
    return as_read(DocumentRead, row)


@router.delete(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    document_id: DocumentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Remove a document from the knowledge base: its file from the bucket, its chunks
    from what the agents search (a delete job for the async worker)
    and its record. Each step is safe to repeat, so a removal that fails
    part way leaves the record, and removing it again finishes the job.
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param document_id: The document.
    :param request: The request, for the clients.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :raises HTTPException: 403 without ``knowledge_bases:manage`` in the organization; 404
        for another knowledge base's document, or none; 502 when storage refuses; 503
        when uploads aren't set up, or storage or the worker is unavailable.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, manage=True
    )
    row = await _document_of(session, knowledge_base_id, document_id)
    store, queue = _clients(request)
    # The file first: an ingest still queued for it then finds nothing to
    # read, and can't put back the chunks the delete job removes.
    key = store.key_of(row.storage_uri)
    if key is not None:
        try:
            await store.delete(key)
        except StorageError as error:
            if error.code == "unreachable":
                logger.warning("Document storage unavailable: %s", error)
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "Document storage is unavailable; try again",
                ) from None
            logger.error("Document storage refused a removal: %s", error)
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "Document storage refused the removal"
            ) from None
    try:
        await queue.delete_document(
            knowledge_base_id=knowledge_base_id, document_id=document_id
        )
    except QueueError as error:
        logger.warning("The async worker's queue is unavailable: %s", error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The async worker is unavailable; try again",
        ) from None
    filename = row.filename
    await session.delete(row)
    await session.commit()
    logger.info(
        "%s removed %s from knowledge base %s: document %s",
        user,
        filename,
        knowledge_base_id,
        document_id,
    )
