"""The documents task's jobs as the worker runs them: through the runner, by
JobSpec, with their lock keys and error contract."""

from __future__ import annotations

from forge_tasks.tasks import JobSpec, JobStatus

from .conftest import documents_runtime


async def test_ingest_and_delete_jobs(files, tmp_path):
    rt = documents_runtime()
    try:
        good = tmp_path / "runbook.md"
        good.write_bytes(files["runbook.md"])
        spec = JobSpec(
            task_type="documents", kind="ingest", payload={"tenant_id": "t1", "doc_id": "d1", "uri": str(good)}
        )
        assert rt.runner.lock_key(spec) == "documents:t1:d1"  # the worker's Redis lock
        assert await rt.runner.describe(spec) == "runbook.md"
        out = await rt.runner.run(spec)
        assert out.status is JobStatus.OK and out.detail["chunk_count"] > 0

        bad = tmp_path / "photo.png"
        bad.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        png = spec.model_copy(update={"payload": {"tenant_id": "t1", "doc_id": "d2", "uri": str(bad)}})
        out = await rt.runner.run(png)
        assert out.status is JobStatus.FAILED and "no parser" in (out.error or "")  # permanent: no retry

        missing = spec.model_copy(
            update={"payload": {"tenant_id": "t1", "doc_id": "d3", "uri": str(tmp_path / "missing.docx")}}
        )
        assert (await rt.runner.run(missing)).status is JobStatus.FAILED

        delete = JobSpec(task_type="documents", kind="delete", payload={"tenant_id": "t1", "doc_id": "d1"})
        assert await rt.runner.describe(delete) == "runbook.md"  # the stored record's name
        assert (await rt.runner.run(delete)).detail["chunks_removed"] > 0
    finally:
        await rt.close()
