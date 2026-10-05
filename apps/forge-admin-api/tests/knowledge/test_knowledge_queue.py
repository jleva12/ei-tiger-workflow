"""The async worker's documents queue: what a job's state means for its
document (``outcome``), and what ``KnowledgeQueue`` submits to SAQ and reads
back, against a stand-in for the SAQ queue."""

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from saq.job import Status

from forge_admin.config import Settings
from forge_admin.knowledge import queue as knowledge_queue
from forge_admin.knowledge.queue import (
    ERROR_LENGTH,
    FUNCTION,
    RESULT_TTL,
    KnowledgeQueue,
    Outcome,
    QueueError,
    document_job_key,
    outcome,
)

KB, DOC = "kb-1", "doc-1"


def job(status: Status, **fields: Any) -> dict[str, Any]:
    return {"status": status, "attempts": 1, "result": None, "error": None} | fields


# ---------------------------------------------------------------- outcome


@pytest.mark.parametrize(
    ("read", "expected"),
    [
        (None, Outcome("MISSING")),
        (job(Status.NEW), Outcome("QUEUED")),
        (job(Status.QUEUED), Outcome("QUEUED")),
        (job(Status.ACTIVE), Outcome("RUNNING")),
        (job(Status.ABORTING), Outcome("RUNNING")),
        (job(Status.ABORTED), Outcome("FAILED", "aborted")),
        # SAQ keeps the traceback; its last line says what went wrong.
        (
            job(
                Status.FAILED,
                error="Traceback (most recent call last):\n"
                '  File "x.py", line 1\n'
                "KeyError: 'unexpected'\n\n  \n",
            ),
            Outcome("FAILED", "KeyError: 'unexpected'"),
        ),
        (job(Status.FAILED), Outcome("FAILED", "failed")),
        (job(Status.FAILED, error="  \n"), Outcome("FAILED", "failed")),
        # Complete: the job's own result says whether the work succeeded.
        (
            job(
                Status.COMPLETE,
                result={
                    "status": "failed",
                    "error": "UnsupportedFormatError: no parser",
                    "detail": {"stage": "parse"},
                },
            ),
            Outcome("FAILED", "UnsupportedFormatError: no parser", {"stage": "parse"}),
        ),
        (
            job(Status.COMPLETE, result={"status": "failed"}),
            Outcome("FAILED", "failed", {}),
        ),
        (
            job(
                Status.COMPLETE,
                result={
                    "status": "ok",
                    "detail": {"status": "ready", "chunk_count": 3},
                },
            ),
            Outcome("SUCCEEDED", detail={"status": "ready", "chunk_count": 3}),
        ),
        (job(Status.COMPLETE), Outcome("SUCCEEDED", detail={})),
        (
            job(Status.COMPLETE, result={"status": "ok", "detail": "3 chunks"}),
            Outcome("SUCCEEDED", detail={}),
        ),
        (job(Status.COMPLETE, result=["odd"]), Outcome("SUCCEEDED", detail={})),
    ],
)
def test_a_jobs_state_is_its_documents_phase(
    read: dict[str, Any] | None, expected: Outcome
) -> None:
    assert outcome(read) == expected


def test_a_long_failure_is_cut_short() -> None:
    traceback = outcome(job(Status.FAILED, error="x" * (ERROR_LENGTH + 50)))
    assert traceback.error == "x" * ERROR_LENGTH
    reported = outcome(
        job(Status.COMPLETE, result={"status": "failed", "error": "y" * 5000})
    )
    assert reported.error == "y" * ERROR_LENGTH


@pytest.mark.parametrize(
    ("phase", "finished"),
    [
        ("QUEUED", False),
        ("RUNNING", False),
        ("SUCCEEDED", True),
        ("FAILED", True),
        ("MISSING", True),
    ],
)
def test_only_final_phases_are_finished(phase: str, finished: bool) -> None:
    assert Outcome(phase).finished is finished


# ----------------------------------------------------------------- queue


@dataclass
class Saq:
    """Stands in for SAQ's queue: what was enqueued, the jobs it has, and an
    error to raise (or a delay) on every call."""

    jobs: dict[str, Any] = field(default_factory=dict)
    enqueued: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    error: Exception | None = None
    delay: float = 0.0
    disconnected: bool = False

    async def enqueue(self, function: str, **options: Any) -> Any:
        await self._act()
        self.enqueued.append((function, options))
        return SimpleNamespace(key=options["key"])

    async def job(self, key: str) -> Any:
        await self._act()
        return self.jobs.get(key)

    async def disconnect(self) -> None:
        self.disconnected = True
        if self.error is not None:
            raise self.error

    async def _act(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error


def test_an_ingest_is_submitted_as_a_documents_job() -> None:
    saq = Saq()
    key = asyncio.run(
        KnowledgeQueue(saq).ingest_document(
            knowledge_base_id=KB,
            document_id=DOC,
            uri="s3://forge-documents/forge/org-o/kb-kb-1/documents/doc-1/a.md",
            filename="a.md",
            media_type="text/markdown",
            uploaded_by="member-1",
        )
    )
    assert key == document_job_key(KB, DOC) == f"documents.ingest:{KB}:{DOC}"
    assert saq.enqueued == [
        (
            FUNCTION,
            {
                "key": key,
                "ttl": RESULT_TTL,
                "spec": {
                    "task_type": "documents",
                    "kind": "ingest",
                    "payload": {
                        # The knowledge base is the worker's tenant.
                        "tenant_id": KB,
                        "doc_id": DOC,
                        "uri": (
                            "s3://forge-documents/forge/org-o/kb-kb-1/"
                            "documents/doc-1/a.md"
                        ),
                        "filename": "a.md",
                        "media_type": "text/markdown",
                        "metadata": {"uploaded_by": "member-1"},
                    },
                },
            },
        )
    ]
    assert FUNCTION == "run_job"


def test_an_ingest_without_a_media_type_sends_none() -> None:
    saq = Saq()
    asyncio.run(
        KnowledgeQueue(saq).ingest_document(
            knowledge_base_id=KB,
            document_id=DOC,
            uri="s3://b/k",
            filename="notes",
            media_type="",
            uploaded_by="member-1",
        )
    )
    ((_, options),) = saq.enqueued
    assert options["spec"]["payload"]["media_type"] is None


def test_each_removal_is_its_own_delete_job() -> None:
    saq = Saq()
    client = KnowledgeQueue(saq)

    async def twice() -> list[str]:
        return [
            await client.delete_document(knowledge_base_id=KB, document_id=DOC)
            for _ in range(2)
        ]

    keys = asyncio.run(twice())
    # A key per submission: SAQ would skip one already queued.
    assert keys[0] != keys[1]
    assert all(key.startswith(f"documents.delete:{KB}:{DOC}:") for key in keys)
    assert [options["key"] for _, options in saq.enqueued] == keys
    for function, options in saq.enqueued:
        assert function == FUNCTION
        assert options["ttl"] == RESULT_TTL
        assert options["spec"] == {
            "task_type": "documents",
            "kind": "delete",
            "payload": {"tenant_id": KB, "doc_id": DOC},
        }


def test_a_job_is_read_back_as_its_state() -> None:
    key = document_job_key(KB, DOC)
    saq = Saq(
        jobs={
            key: SimpleNamespace(
                status=Status.COMPLETE,
                attempts=2,
                result={"status": "ok", "detail": {"chunk_count": 3}},
                error=None,
            )
        }
    )
    client = KnowledgeQueue(saq)
    assert asyncio.run(client.job(key)) == {
        "status": Status.COMPLETE,
        "attempts": 2,
        "result": {"status": "ok", "detail": {"chunk_count": 3}},
        "error": None,
    }
    assert asyncio.run(client.job("documents.ingest:kb-1:gone")) is None


@pytest.mark.parametrize(
    "error",
    [
        RedisConnectionError("Error 111 connecting to redis:6379"),
        RedisError(),
        OSError("Network is unreachable"),
        ConnectionRefusedError(),
    ],
)
def test_an_unreachable_queue_is_a_queue_error(error: Exception) -> None:
    client = KnowledgeQueue(Saq(error=error))
    with pytest.raises(QueueError) as raised:
        asyncio.run(
            client.ingest_document(
                knowledge_base_id=KB,
                document_id=DOC,
                uri="s3://b/k",
                filename="a.md",
                media_type="",
                uploaded_by="member-1",
            )
        )
    # Says what went wrong, or at least what kind of error it was.
    assert str(raised.value) == (str(error) or type(error).__name__)
    with pytest.raises(QueueError):
        asyncio.run(client.delete_document(knowledge_base_id=KB, document_id=DOC))
    with pytest.raises(QueueError):
        asyncio.run(client.job(document_job_key(KB, DOC)))


def test_a_queue_that_doesnt_answer_in_time_is_a_queue_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(knowledge_queue, "TIMEOUT", 0.01)
    client = KnowledgeQueue(Saq(delay=1.0))
    with pytest.raises(QueueError, match="TimeoutError"):
        asyncio.run(client.delete_document(knowledge_base_id=KB, document_id=DOC))
    with pytest.raises(QueueError, match="TimeoutError"):
        asyncio.run(client.job(document_job_key(KB, DOC)))


def test_other_errors_arent_taken_for_an_unreachable_queue() -> None:
    client = KnowledgeQueue(Saq(error=ValueError("bad spec")))
    with pytest.raises(ValueError, match="bad spec"):
        asyncio.run(client.delete_document(knowledge_base_id=KB, document_id=DOC))


def test_closing_a_queue_that_went_away_is_quiet() -> None:
    saq = Saq(error=RedisConnectionError("gone"))
    asyncio.run(KnowledgeQueue(saq).aclose())
    assert saq.disconnected


def test_without_a_redis_url_there_is_no_queue(settings: Settings) -> None:
    unset = settings.model_copy(update={"embedding_redis_url": None})
    assert KnowledgeQueue.from_settings(unset) is None
