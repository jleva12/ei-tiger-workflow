"""The async worker's documents queue: submitting knowledge base documents'
ingest and delete jobs, and reading them back.

The worker serves the ``documents`` queue with one job function, ``run_job``,
which runs a spec of the documents task (``forge_task_documents``):
``{"task_type": "documents", "kind": "ingest" | "delete", "payload"}``. Its
tenant is the knowledge base: every chunk it embeds carries the knowledge
base's ID, and a search names it. The worker applies its own retries, timeout
and heartbeat to every job, so a submission names only the spec and a key; a
key that is already queued or running isn't submitted again. This client only
relays; the routes decide who may submit what.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Self
from uuid import uuid4

from redis import asyncio as aioredis
from redis.exceptions import RedisError
from saq.job import Status
from saq.queue.redis import RedisQueue

from forge_admin.config import Settings

DOCUMENTS = "documents"
FUNCTION = "run_job"
# How long the worker keeps a finished job for reading it back, in seconds.
# The admin keeps what it needs of the outcome once it has read it.
RESULT_TTL = 7 * 24 * 60 * 60
# Seconds a submission or a read may take.
TIMEOUT = 5.0
# How much of a failure's explanation to keep.
ERROR_LENGTH = 1000


def document_job_key(knowledge_base_id: str, document_id: str) -> str:
    """:return: The key of a document's ingest job."""
    return f"documents.ingest:{knowledge_base_id}:{document_id}"


class QueueError(Exception):
    """The async worker's documents queue could not be reached."""


@dataclass(frozen=True)
class Outcome:
    """
    A job as last read.

    :ivar phase: ``QUEUED``, ``RUNNING``, ``SUCCEEDED``, ``FAILED``, or
        ``MISSING`` when the worker no longer has the job.
    :ivar error: Why it failed.
    :ivar detail: What the job reported, e.g. ``chunk_count``.
    """

    phase: str
    error: str = ""
    detail: dict[str, Any] | None = None

    @property
    def finished(self) -> bool:
        return self.phase in ("SUCCEEDED", "FAILED", "MISSING")


def outcome(job: dict[str, Any] | None) -> Outcome:
    """
    :param job: A job as :meth:`KnowledgeQueue.job` reads it; None when missing.
    :return: Its outcome.
    """
    if job is None:
        return Outcome("MISSING")
    state = job["status"]
    if state in (Status.NEW, Status.QUEUED):
        return Outcome("QUEUED")
    if state in (Status.ACTIVE, Status.ABORTING):
        return Outcome("RUNNING")
    if state == Status.ABORTED:
        return Outcome("FAILED", "aborted")
    if state == Status.FAILED:
        # SAQ keeps the traceback; its last line says what went wrong.
        lines = [line for line in (job.get("error") or "").splitlines() if line.strip()]
        return Outcome("FAILED", (lines[-1] if lines else "failed")[:ERROR_LENGTH])
    # Complete: the job's own result says whether the work succeeded.
    raw_result = job.get("result")
    result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    raw_detail = result.get("detail")
    detail: dict[str, Any] = raw_detail if isinstance(raw_detail, dict) else {}
    if result.get("status") == "failed":
        return Outcome(
            "FAILED", str(result.get("error") or "failed")[:ERROR_LENGTH], detail
        )
    return Outcome("SUCCEEDED", detail=detail)


class KnowledgeQueue:
    """
    Submits knowledge base documents' jobs to the async worker and reads them
    back.

    :param queue: The worker's ``documents`` queue (a SAQ queue).
    """

    def __init__(self, queue: Any) -> None:
        self._queue = queue

    @classmethod
    def from_settings(cls, settings: Settings) -> Self | None:
        """
        :param settings: Settings with ``embedding_redis_url``.
        :return: A client, or None when the async worker isn't set up.
        """
        if settings.embedding_redis_url is None:
            return None
        redis = aioredis.from_url(
            settings.embedding_redis_url,
            socket_connect_timeout=TIMEOUT,
            socket_timeout=TIMEOUT,
        )
        return cls(RedisQueue(redis, name=DOCUMENTS))

    async def ingest_document(
        self,
        *,
        knowledge_base_id: str,
        document_id: str,
        uri: str,
        filename: str,
        media_type: str,
        uploaded_by: str,
        force: bool = False,
    ) -> str:
        """
        Submit a stored file to be parsed, chunked and embedded.

        :param knowledge_base_id: Whose documents it joins: the worker's tenant.
        :param document_id: The document; the worker keys its record by it.
        :param uri: Where the worker reads it, ``s3://<bucket>/<key>``.
        :param filename: Its name, whose extension picks the parser.
        :param media_type: Its media type, as uploaded.
        :param uploaded_by: Who uploaded it, kept with its chunks.
        :param force: Ingest it again even when the worker has it as it is
            (re-indexing: with the models, parsers and chunking it has now).
        :return: The job's key.
        :raises QueueError: The queue could not be reached.
        """
        key = document_job_key(knowledge_base_id, document_id)
        payload = {
            "tenant_id": knowledge_base_id,
            "doc_id": document_id,
            "uri": uri,
            "filename": filename,
            "media_type": media_type or None,
            "metadata": {"uploaded_by": uploaded_by},
        }
        if force:
            payload["force"] = True
        await self._submit(key, "ingest", payload)
        return key

    async def delete_document(self, *, knowledge_base_id: str, document_id: str) -> str:
        """
        Submit removing a document's chunks from the knowledge base. The
        worker tombstones its record first, so an ingest still running for it
        is fenced out.

        :param knowledge_base_id: The knowledge base.
        :param document_id: The document.
        :return: The job's key.
        :raises QueueError: The queue could not be reached.
        """
        # A key per submission: SAQ skips a key already queued, and a second
        # removal must run even when an earlier one is still kept.
        key = f"documents.delete:{knowledge_base_id}:{document_id}:{uuid4().hex[:8]}"
        payload = {"tenant_id": knowledge_base_id, "doc_id": document_id}
        await self._submit(key, "delete", payload)
        return key

    async def job(self, key: str) -> dict[str, Any] | None:
        """
        :param key: The job's key.
        :return: ``{"status", "attempts", "result", "error"}``; None when the
            worker no longer has the job.
        :raises QueueError: The queue could not be reached.
        """
        try:
            async with asyncio.timeout(TIMEOUT):
                job = await self._queue.job(key)
        except (RedisError, OSError, TimeoutError) as error:
            raise QueueError(str(error) or type(error).__name__) from None
        if job is None:
            return None
        return {
            "status": job.status,
            "attempts": job.attempts,
            "result": job.result,
            "error": job.error,
        }

    async def aclose(self) -> None:
        # Closing a connection to a Redis that went away.
        with contextlib.suppress(RedisError, OSError):
            await self._queue.disconnect()

    async def _submit(self, key: str, kind: str, payload: dict[str, Any]) -> None:
        spec = {"task_type": DOCUMENTS, "kind": kind, "payload": payload}
        try:
            async with asyncio.timeout(TIMEOUT):
                await self._queue.enqueue(FUNCTION, key=key, ttl=RESULT_TTL, spec=spec)
        except (RedisError, OSError, TimeoutError) as error:
            raise QueueError(str(error) or type(error).__name__) from None
