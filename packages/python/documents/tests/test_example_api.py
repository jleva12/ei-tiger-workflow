"""The documents example API (examples/documents_api.py), its jobs held and
then run as a worker would."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from forge_tasks.runner import InlineJobQueue  # noqa: E402

from .conftest import documents_runtime  # noqa: E402


class HoldingQueue(InlineJobQueue):
    """Holds jobs until the test releases them (like a worker picking them up)."""

    def __init__(self) -> None:
        super().__init__()
        self.held: list = []

    async def enqueue(self, spec, *, countdown=None):
        self.held.append(spec)

    async def release(self) -> None:
        while self.held:
            spec = self.held.pop(0)
            assert self.runner is not None
            await self.runner.run(spec)  # follow-ups land back in self.held


def test_documents_endpoints(files, tmp_path):
    from examples import documents_api as api

    queue = HoldingQueue()
    rt = documents_runtime(queue=queue)
    api.app.state.runtime = rt

    def fake_upload(tenant, doc_id, filename, content_type, data):
        path = tmp_path / f"{doc_id}-{filename}"
        path.write_bytes(data)
        return str(path)

    api.app.dependency_overrides[api.get_uploader] = lambda: fake_upload

    with TestClient(api.app) as client:
        h = {"X-Tenant-Id": "t1"}
        # documents: upload -> pending -> worker -> ready -> searchable
        r = client.post("/documents", headers=h, files={"file": ("runbook.md", files["runbook.md"], "text/markdown")})
        doc_id = r.json()["doc_id"]
        assert client.get(f"/documents/{doc_id}", headers=h).json()["status"] == "pending"
        assert client.get(f"/documents/{doc_id}", headers={"X-Tenant-Id": "other"}).status_code == 404
        client.portal.call(queue.release)
        assert client.get(f"/documents/{doc_id}", headers=h).json()["status"] == "ready"
        hits = client.post("/search", headers=h, json={"text": "BILL-1042", "top_k": 3}).json()["hits"]
        assert "BILL-1042" in hits[0]["chunk"]["text"]
    api.app.dependency_overrides.clear()
    del api.app.state.runtime
