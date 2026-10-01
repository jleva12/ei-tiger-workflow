"""The admin API, which acts for the member a run acts as.

It keeps the organization's workflows. The worker calls its workflow service
routes (``/internal/workflows/...``) with FORGE_WORKFLOWS_TOKEN, naming the
organization and the member; the admin checks the member may do it, as it
would for them.

A refusal (the member may no longer run workflows, the workflow doesn't exist)
fails the step; the admin being down or overloaded is retried.
"""

from __future__ import annotations

from typing import Any

import httpx

from forge_task_workflows.errors import NotSetUp, StepFailed
from forge_tasks.errors import TransientError

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class AdminClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @classmethod
    def create(
        cls, url: str, token: str, *, timeout: float, transport: httpx.AsyncBaseTransport | None = None
    ) -> AdminClient:
        client = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/internal/workflows",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )
        return cls(client)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise TransientError(f"the admin API isn't answering: {exc}") from exc
        if response.status_code in RETRYABLE:
            raise TransientError(f"the admin API answered {response.status_code}: {_detail(response)}")
        if response.status_code == 401:
            raise NotSetUp("The admin API refused the worker's token: check FORGE_WORKFLOWS_TOKEN")
        if response.status_code >= 400:
            raise StepFailed(_detail(response), status=response.status_code)
        return response.json() if response.content else None

    # ------------------------------------------------------------------ workflows

    async def start_workflow(
        self,
        *,
        organization_id: str,
        user_id: str,
        workflow_id: str,
        input: Any,
        parent: str,
        depth: int,
    ) -> dict[str, Any]:
        """Start one of the organization's workflows as the member, from this run."""
        return await self._call(
            "POST",
            f"/workflows/{workflow_id}/runs",
            json={
                "organization_id": organization_id,
                "user_id": user_id,
                "input": input,
                "parent": parent,
                "depth": depth,
            },
        )


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or f"HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail if detail is not None else body)[:500]
