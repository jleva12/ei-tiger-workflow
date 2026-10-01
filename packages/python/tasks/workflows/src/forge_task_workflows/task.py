"""The workflows task type: one job, ``run``, which runs one run of an
organization's workflow, start to end, through waits and restarts.

The admin API submits a run (``POST /organizations/{id}/workflows/{id}/runs``) as a
job on the ``workflows`` queue, with the workflow's document as it was when
the run started (a run is pinned to that revision: editing the workflow
never changes a run in progress), the input, and the member it acts as.
The worker runs the job as a tracked run of the task framework, labelled
with the organization, so it's among the organization's background tasks,
where people decide its approvals, restart it or abandon it.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from forge_common.model_provider import ModelProviderConfigError
from pydantic import BaseModel, Field
from pydantic_settings import SettingsError

from forge_task_workflows.checks import problems
from forge_task_workflows.config import WorkflowsSettings
from forge_task_workflows.document import Workflow
from forge_task_workflows.engine import Engine, RunInfo
from forge_task_workflows.errors import WorkflowFailed
from forge_task_workflows.expressions import as_text
from forge_task_workflows.nodes import EXECUTORS
from forge_task_workflows.services.admin import AdminClient
from forge_task_workflows.services.base import Services
from forge_task_workflows.services.llm import language_models
from forge_tasks.control import LocalJobControl, current_control
from forge_tasks.env_files import environment
from forge_tasks.runner import ok
from forge_tasks.tasks import JobResult, Schedule, TaskContext

log = logging.getLogger(__name__)

TASK_NAME = "workflows"
RUN = "run"


class RunPayload(BaseModel):
    """One run of a workflow, as the admin API submits it."""

    tenant_id: str = Field(description="The organization the workflow is the organization's")
    workflow_id: str
    revision: int = 0
    name: str = ""
    document: dict[str, Any]
    input: Any = None
    run_as: str = Field(description="The member the run acts as: its approvals, other workflows")
    run_as_name: str = ""
    depth: int = 0
    parent: str | None = None
    trigger: dict[str, Any] = Field(default_factory=dict)


class RunWorkflowJob:
    name = RUN
    payload_model = RunPayload

    def __init__(self, task: WorkflowsTask) -> None:
        self.task = task

    def lock_key(self, payload: RunPayload) -> str | None:
        return None  # a run is its own: the task framework's lease keeps one worker on it

    def describe(self, payload: RunPayload) -> str:
        return payload.name or payload.workflow_id

    async def run(self, payload: RunPayload) -> JobResult:
        # Outside the worker (the CLI, tests) a run keeps its state in memory,
        # and a wait ends it.
        control = current_control() or LocalJobControl()
        workflow = Workflow.parse(payload.document)
        found = problems(workflow)
        if found:
            raise WorkflowFailed("The workflow can't run until these are fixed: " + "; ".join(found))
        info = RunInfo(
            organization_id=payload.tenant_id,
            workflow_id=payload.workflow_id,
            workflow_name=payload.name or workflow.name,
            revision=payload.revision,
            run_as=payload.run_as,
            run_as_name=payload.run_as_name,
            depth=payload.depth,
            input=payload.input,
        )
        engine = Engine(
            workflow,
            info,
            control=control,
            services=self.task.services,
            executors=EXECUTORS,
            settings=self.task.settings,
        )
        finished = await engine.run()
        if finished["outcome"] == "failed":
            step = workflow.by_id.get(finished.get("step") or "")
            where = f" at {step.label}" if step else ""
            return JobResult.failed(
                f"The workflow ended failed{where}: {as_text(finished['result'])[:500]}",
                outcome="failed",
                result=finished["result"],
            )
        return ok(outcome="succeeded", result=finished["result"], steps=engine.state["executed"])


class WorkflowsTask:
    name = TASK_NAME
    queue = TASK_NAME

    def __init__(self, settings: WorkflowsSettings, services: Services) -> None:
        self.settings = settings
        self.services = services
        self._jobs = {RUN: RunWorkflowJob(self)}

    @property
    def jobs(self) -> dict[str, RunWorkflowJob]:
        return self._jobs

    async def ensure_schema(self) -> None:
        return None

    async def close(self) -> None:
        await self.services.http.aclose()
        if self.services.admin is not None:
            await self.services.admin.aclose()


def build_services(ctx: TaskContext, settings: WorkflowsSettings) -> Services:
    admin = None
    if settings.admin_url and settings.admin_token is not None:
        admin = AdminClient.create(
            settings.admin_url, settings.admin_token.get_secret_value(), timeout=settings.admin_timeout
        )
    llm, unavailable = None, None
    try:
        llm = language_models(
            config_path=settings.model_provider_config,
            environment=environment() if settings.model_provider_config else {},
            google_api_key=settings.google_api_key.get_secret_value() if settings.google_api_key else None,
            default_model=settings.agent_model,
            timeout=settings.agent_timeout,
        )
    except (ModelProviderConfigError, SettingsError) as exc:
        unavailable = f"its models can't be read: {exc}"
        log.error("workflows: agent steps will fail: %s", unavailable)

    return Services(
        settings=settings,
        http=httpx.AsyncClient(timeout=30, follow_redirects=False),
        admin=admin,
        llm=llm,
        llm_unavailable=unavailable,
    )


class WorkflowsTaskFactory:
    name = TASK_NAME
    queue = TASK_NAME
    schedules: list[Schedule] = []
    settings_model = WorkflowsSettings

    def build(self, ctx: TaskContext) -> WorkflowsTask:
        settings: WorkflowsSettings = ctx.options or WorkflowsSettings()
        services = ctx.extras.get("workflows.services") or build_services(ctx, settings)
        if services.admin is None:
            log.warning("workflows: no admin API (HYBRID_WORKFLOWS__ADMIN_URL/_TOKEN): run-workflow steps will fail")
        if services.llm is None and not services.llm_unavailable:
            log.warning(
                "workflows: no models (HYBRID_WORKFLOWS__MODEL_PROVIDER_CONFIG, or HYBRID_WORKFLOWS__GOOGLE_API_KEY): "
                "agent steps will fail"
            )
        return WorkflowsTask(settings, services)
