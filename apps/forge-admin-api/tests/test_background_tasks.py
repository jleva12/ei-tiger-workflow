"""The background tasks client: what it sends to the async worker's API, and
how its refusals read for the caller."""

import asyncio
import json
from typing import Any

import httpx2 as httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from forge_admin.api.routes.background_tasks import NO_SUCH_TASK, refusal
from forge_admin.background_tasks import BackgroundTasks, BackgroundTasksError
from forge_admin.config import Settings

ADMIN = {"id": "admin-1", "display_name": "Ada Admin"}


def run(call: Any) -> Any:
    return asyncio.run(call)


def client(handler: Any) -> tuple[BackgroundTasks, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    async def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        await request.aread()
        return handler(request)

    http = httpx.AsyncClient(
        base_url="http://async-worker-api:8094/v1",
        headers={"Authorization": "Bearer worker-token"},
        transport=httpx.MockTransport(record),
    )
    return BackgroundTasks(http), seen


def test_it_is_off_until_set_up(settings: Settings) -> None:
    assert BackgroundTasks.from_settings(settings) is None
    url = {"async_worker_url": "http://async-worker-api:8094"}
    assert BackgroundTasks.from_settings(settings.model_copy(update=url)) is None
    ready = settings.model_copy(update={**url, "async_worker_token": SecretStr("t")})
    assert BackgroundTasks.from_settings(ready) is not None


def test_it_lists_an_organizations_tasks_with_its_filters() -> None:
    tasks, seen = client(lambda r: httpx.Response(200, json={"items": [], "total": 0}))
    page = run(
        tasks.tasks(
            tenant="org-1",
            task_types=["workflows", "other"],
            exclude_task_types=["workflows"],
            statuses=["FAILED"],
            limit=20,
            offset=40,
        )
    )
    assert page == {"items": [], "total": 0}
    [request] = seen
    assert (request.method, request.url.path) == ("GET", "/v1/tasks")
    assert request.url.params.multi_items() == [
        ("tenant", "org-1"),
        ("limit", "20"),
        ("offset", "40"),
        ("task_type", "workflows"),
        ("task_type", "other"),
        ("exclude_task_type", "workflows"),
        ("status", "FAILED"),
    ]
    assert request.headers["Authorization"] == "Bearer worker-token"


def test_actions_name_who_acts() -> None:
    tasks, seen = client(
        lambda r: httpx.Response(202, json={"queue": "workflows", "key": "k"})
    )
    assert run(tasks.resubmit("i/1", ADMIN)) == {"queue": "workflows", "key": "k"}
    run(tasks.restart("i1", ADMIN))
    run(tasks.abandon("i1", ADMIN))
    assert [(r.method, r.url.raw_path.decode()) for r in seen] == [
        ("POST", "/v1/tasks/i%2F1/resubmit"),
        ("POST", "/v1/tasks/i1/restart"),
        ("POST", "/v1/tasks/i1/abandon"),
    ]
    assert all(json.loads(r.content) == {"actor": ADMIN} for r in seen)


def test_refusals_carry_the_apis_explanation() -> None:
    tasks, _ = client(
        lambda r: httpx.Response(
            409, json={"detail": "A completed task can't be restarted"}
        )
    )
    with pytest.raises(BackgroundTasksError) as refused:
        run(tasks.restart("i1", ADMIN))
    assert (refused.value.status, refused.value.message) == (
        409,
        "A completed task can't be restarted",
    )

    tasks, _ = client(lambda r: httpx.Response(200, content=b"[]"))
    with pytest.raises(BackgroundTasksError, match="not a JSON object"):
        run(tasks.task("i1"))

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    tasks, _ = client(down)
    with pytest.raises(BackgroundTasksError) as unreachable:
        run(tasks.task("i1"))
    assert unreachable.value.status == 0


@pytest.mark.parametrize(
    ("status", "answer", "detail"),
    [
        (0, 503, "The async worker is unavailable; try again"),
        (500, 503, "The async worker is unavailable; try again"),
        (401, 502, "The async worker refused Forge"),
        (404, 404, NO_SUCH_TASK),
        (409, 409, "A running task can't be resubmitted"),
        (422, 502, "The async worker answered unexpectedly"),
    ],
)
def test_refusals_read_for_the_caller(status: int, answer: int, detail: str) -> None:
    error = refusal(BackgroundTasksError(status, "A running task can't be resubmitted"))
    assert isinstance(error, HTTPException)
    assert (error.status_code, error.detail) == (answer, detail)
