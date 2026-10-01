"""Building workflows and running them in tests, with fake services."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from forge_task_workflows.config import WorkflowsSettings
from forge_task_workflows.services.admin import AdminClient
from forge_task_workflows.services.base import Services
from forge_task_workflows.services.llm import LanguageModels, gemini_config
from forge_task_workflows.task import RunPayload, WorkflowsTask
from forge_tasks.control import LocalJobControl, controlling
from forge_tasks.tasks import JobResult

ORGANIZATION = "3f6c0000-0000-4000-8000-000000000001"
MEMBER = "member-1"


def workflow(nodes: list[tuple[str, str, dict[str, Any]]], edges: list[tuple[str, str, str]]) -> dict[str, Any]:
    """A forge.workflow/v1 document: nodes (id, kind, config), edges (source, output, target)."""
    return {
        "format": "forge.workflow/v1",
        "id": "wf_test",
        "name": "Test workflow",
        "organization_id": ORGANIZATION,
        "entry": nodes[0][0],
        "nodes": [
            {"id": node_id, "kind": kind, "name": node_id.replace("_", " ").title(), "config": config, "outputs": []}
            for node_id, kind, config in nodes
        ],
        "edges": [{"id": f"{s}:{o}->{t}", "source": s, "source_output": o, "target": t} for s, o, t in edges],
    }


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@dataclass
class FakeAdmin:
    """The admin API's workflow routes, answering from dicts."""

    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    started: list[dict[str, Any]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/internal/workflows")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path.startswith("/workflows/") and path.endswith("/runs"):
            self.started.append(body)
            return httpx.Response(
                202, json={"queue": "workflows", "key": f"child:{body['parent']}", "workflow_name": "Child"}
            )
        return httpx.Response(404, json={"detail": "no such route"})


class FakeGemini:
    """A model answering from a script, behind the real ProviderModels: each
    item a list of parts (``{"text": ...}`` or ``{"function_call": {...}}``),
    or JSON text for the final JSON answer. It records each request, and
    each call ProviderModels made it for (the model and thinking level)."""

    def __init__(self, script: list[Any], config: Any = None) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.calls: list[Any] = []
        self.config = config

    def build(self, call: Any) -> Any:
        self.calls.append(call)
        return self

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
        from google.adk.models.llm_response import LlmResponse
        from google.genai import types

        self.requests.append(
            {"model": llm_request.model, "contents": list(llm_request.contents), "config": llm_request.config}
        )
        step = self.script.pop(0)
        parts = [types.Part(text=step)] if isinstance(step, str) else [types.Part.model_validate(p) for p in step]
        yield LlmResponse(content=types.Content(role="model", parts=parts))

    def models(self) -> LanguageModels:
        from forge_common.adk.models import ProviderModels

        config = self.config or gemini_config("key", "gemini-test")
        return LanguageModels(ProviderModels(config, build=self.build), timeout=10)


def services(
    *,
    clock: Clock | None = None,
    http: Callable[[httpx.Request], httpx.Response] | None = None,
    admin: FakeAdmin | None = None,
    gemini: FakeGemini | None = None,
    **settings: Any,
) -> Services:
    clock = clock or Clock()
    return Services(
        settings=WorkflowsSettings(http_allowed_hosts=["api.example.com"], **settings),
        http=httpx.AsyncClient(transport=httpx.MockTransport(http or (lambda r: httpx.Response(404)))),
        admin=AdminClient(
            httpx.AsyncClient(base_url="http://admin/internal/workflows", transport=httpx.MockTransport(admin.handle))
        )
        if admin
        else None,
        llm=gemini.models() if gemini else None,
        clock=clock,
        sleep=clock.sleep,
    )


async def run(
    document: dict[str, Any],
    *,
    input: Any = None,
    control: LocalJobControl | None = None,
    svc: Services | None = None,
) -> JobResult:
    """Run the workflow once (to its end, or to a wait, which raises)."""
    svc = svc or services()
    task = WorkflowsTask(svc.settings, svc)
    payload = RunPayload(
        tenant_id=ORGANIZATION,
        workflow_id="wf_test",
        name="Test workflow",
        document=document,
        input=input,
        run_as=MEMBER,
    )
    with controlling(control or LocalJobControl()):
        return await task.jobs["run"].run(payload)
