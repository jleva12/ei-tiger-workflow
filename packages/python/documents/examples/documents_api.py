"""Example FastAPI wiring for the documents task: upload -> S3 -> queue
ingest, document status, /search.

The API builds the documents task with a queue that submits its jobs to the
async worker by name (SAQ queue ``documents``, function ``run_job``), so jobs
run on the workers, each one a tracked run there; nothing heavy (parsers,
embeddings) runs in the API.

    uv run --project ../../../../apps/forge-async-worker uvicorn examples.documents_api:app --reload
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from forge_task_documents.models import DocumentRecord, SearchFilters, SearchQuery, SearchResponse
from forge_tasks.runtime import Runtime, build_runtime
from forge_tasks.settings import CoreSettings
from forge_tasks.tasks import JobSpec

Uploader = Callable[[str, str, str, str | None, bytes], str]  # (tenant, doc_id, filename, type, data) -> uri


class WorkerQueue:
    """Submits jobs to the async worker by name: on the SAQ queue named after
    the task type, ``run_job`` with the spec. No code from the worker needed."""

    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url
        self.queues: dict[str, Any] = {}

    async def enqueue(self, spec: JobSpec, *, countdown: float | None = None) -> None:
        from saq import Queue

        queue = self.queues.get(spec.task_type)
        if queue is None:
            queue = self.queues[spec.task_type] = Queue.from_url(self.redis_url, name=spec.task_type)
        options = {"scheduled": int(time.time() + countdown)} if countdown else {}
        await queue.enqueue("run_job", spec=spec.model_dump(mode="json"), **options)

    async def close(self) -> None:
        for queue in self.queues.values():
            await queue.disconnect()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not hasattr(app.state, "runtime"):  # tests inject their own
        settings = CoreSettings(enabled_tasks=["documents"])
        app.state.runtime = build_runtime(settings, queue=WorkerQueue(settings.redis_url))
    rt: Runtime = app.state.runtime
    await rt.ensure_schema()
    yield
    await rt.close()


app = FastAPI(title="documents-example", lifespan=lifespan)


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def get_tenant_id(x_tenant_id: Annotated[str, Header()]) -> str:
    # Replace with real auth, e.g. the Clerk org id from the verified session token.
    return x_tenant_id


def s3_uploader(tenant: str, doc_id: str, filename: str, content_type: str | None, data: bytes) -> str:
    import boto3

    bucket = os.environ["INGEST_BUCKET"]
    key = f"{tenant}/{doc_id}/{filename}"
    boto3.client("s3").put_object(
        Bucket=bucket, Key=key, Body=data, ContentType=content_type or "application/octet-stream"
    )
    return f"s3://{bucket}/{key}"


def get_uploader() -> Uploader:
    return s3_uploader


Tenant = Annotated[str, Depends(get_tenant_id)]
RT = Annotated[Runtime, Depends(get_runtime)]


@app.post("/documents", status_code=202)
async def upload_document(
    file: UploadFile, tenant: Tenant, rt: RT, upload: Annotated[Uploader, Depends(get_uploader)]
) -> dict[str, str]:
    data = await file.read()
    if len(data) > rt.documents.settings.max_file_bytes:
        raise HTTPException(413, "file too large")
    doc_id = uuid.uuid4().hex
    filename = file.filename or doc_id
    uri = upload(tenant, doc_id, filename, file.content_type, data)
    await rt.documents.storage.documents.register(
        DocumentRecord(
            doc_id=doc_id,
            tenant_id=tenant,
            filename=filename,
            media_type=file.content_type,
            source_uri=uri,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
    )
    await rt.enqueue(
        JobSpec(
            task_type="documents",
            kind="ingest",
            payload={
                "tenant_id": tenant,
                "doc_id": doc_id,
                "uri": uri,
                "filename": filename,
                "media_type": file.content_type,
            },
        )
    )
    return {"doc_id": doc_id, "status": "pending"}


@app.get("/documents/{doc_id}")
async def get_document(doc_id: str, tenant: Tenant, rt: RT) -> DocumentRecord:
    rec = await rt.documents.storage.documents.get(tenant, doc_id)
    if rec is None:
        raise HTTPException(404, "not found")
    return rec


class SearchBody(BaseModel):
    text: str = Field(min_length=1)
    filters: SearchFilters = SearchFilters()
    top_k: int = Field(default=8, ge=1, le=50)
    expand_sections: bool = False


@app.post("/search")
async def search(body: SearchBody, tenant: Tenant, rt: RT) -> SearchResponse:
    # tenant comes from auth, never from the request body
    return await rt.documents.search.search(SearchQuery(tenant_id=tenant, **body.model_dump()))
