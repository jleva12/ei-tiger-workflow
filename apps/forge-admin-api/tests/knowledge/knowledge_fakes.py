"""Fakes of the knowledge bases' services, standing in on ``app.state`` for
``forge_admin.knowledge.queue.KnowledgeQueue`` (``knowledge_queue``),
``forge_admin.knowledge.storage.DocumentStore`` (``documents``) and
``forge_admin.knowledge.search.KnowledgeSearch`` (``knowledge_search``)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import IO, Any

from saq.job import Status

from forge_admin.knowledge.queue import QueueError, document_job_key
from forge_admin.knowledge.search import Passage, SearchError
from forge_admin.knowledge.storage import StorageError


def job(
    status: Status, *, result: dict[str, Any] | None = None, error: str | None = None
) -> dict[str, Any]:
    """:return: A job as ``KnowledgeQueue.job`` reads it back."""
    return {"status": status, "attempts": 1, "result": result, "error": error}


class FakeQueue:
    """Takes submissions as the async worker's documents queue does, records
    them, and answers reads with each job's state: queued until told
    otherwise."""

    def __init__(self) -> None:
        # The ingest submissions, as the routes made them.
        self.documents: list[dict[str, Any]] = []
        # The documents whose chunks it was asked to remove, as submitted.
        self.deletions: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any] | None] = {}
        # Every key read back, in order.
        self.reads: list[str] = []
        # Set to make every call fail as an unreachable Redis does.
        self.refuse = False

    async def ingest_document(self, **submission: Any) -> str:
        self._check()
        self.documents.append(submission)
        key = document_job_key(
            submission["knowledge_base_id"], submission["document_id"]
        )
        self.jobs[key] = job(Status.QUEUED)
        return key

    async def delete_document(self, **submission: Any) -> str:
        self._check()
        self.deletions.append(submission)
        return (
            f"documents.delete:{submission['knowledge_base_id']}:"
            f"{submission['document_id']}:{len(self.deletions)}"
        )

    async def job(self, key: str) -> dict[str, Any] | None:
        self._check()
        self.reads.append(key)
        return self.jobs.get(key)

    def finish(
        self,
        key: str,
        *,
        result: dict[str, Any] | None = None,
        status: Status = Status.COMPLETE,
        error: str | None = None,
    ) -> None:
        """Make a job finished (or in any state), as the worker reports it."""
        self.jobs[key] = job(status, result=result, error=error)

    def forget(self, key: str) -> None:
        """Make the worker no longer have a job, as when it expired."""
        self.jobs[key] = None

    async def aclose(self) -> None:
        pass

    def _check(self) -> None:
        if self.refuse:
            raise QueueError("Error 111 connecting to redis:6379")


@dataclass
class FakeStoredFile:
    """A stored file being read, as ``DocumentStore.open`` answers it."""

    size: int
    body: bytes
    chunk_size: int = 4
    closed: bool = False

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            for start in range(0, len(self.body), self.chunk_size):
                yield self.body[start : start + self.chunk_size]
        finally:
            self.closed = True

    def close(self) -> None:
        self.closed = True


class FakeStore:
    """Stores objects in memory, as the documents bucket would."""

    bucket = "forge-documents"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        # Set to make every call fail this way.
        self.refuse: StorageError | None = None
        self.opened: list[FakeStoredFile] = []

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def key_of(self, uri: str) -> str | None:
        prefix = f"s3://{self.bucket}/"
        return uri.removeprefix(prefix) if uri.startswith(prefix) else None

    async def put(self, key: str, body: IO[bytes], *, media_type: str) -> None:
        if self.refuse is not None:
            raise self.refuse
        body.seek(0)
        self.objects[key] = (body.read(), media_type)

    async def open(self, key: str) -> FakeStoredFile:
        if self.refuse is not None:
            raise self.refuse
        if key not in self.objects:
            raise StorageError("NoSuchKey", "The specified key does not exist.")
        content = self.objects[key][0]
        stored = FakeStoredFile(size=len(content), body=content)
        self.opened.append(stored)
        return stored

    async def delete(self, key: str) -> None:
        if self.refuse is not None:
            raise self.refuse
        self.objects.pop(key, None)

    def close(self) -> None:
        pass


#: The model the fake search embeds questions with.
MODEL = "text-embedding-3-large@1024"


@dataclass
class FakeSearch:
    """Answers every search with the same passages (or raises), recording
    what it was asked."""

    passages: list[Passage] = field(default_factory=list)
    error: SearchError | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    model_id: str = MODEL
    # The worker's records of documents, by (knowledge base, document).
    known: dict[tuple[str, str], Any] = field(default_factory=dict)
    # The documents removed from search, and whether the store is down.
    forgotten: list[tuple[str, str]] = field(default_factory=list)
    down: bool = False

    async def records(self, documents: Any) -> dict[tuple[str, str], Any]:
        if self.down:
            raise SearchError("connection refused")
        return {key: self.known.get(key) for key in documents}

    async def forget(self, knowledge_base_id: str, document_id: str) -> bool:
        if self.down:
            return False
        self.forgotten.append((knowledge_base_id, document_id))
        return True

    async def search(
        self,
        knowledge_base_ids: list[str],
        query: str,
        *,
        limit: int = 8,
        document_ids: list[str] | None = None,
    ) -> list[Passage]:
        self.calls.append(
            {
                "knowledge_base_ids": knowledge_base_ids,
                "query": query,
                "limit": limit,
                "document_ids": document_ids,
            }
        )
        if self.error is not None:
            raise self.error
        return self.passages[:limit]

    async def aclose(self) -> None:
        pass
