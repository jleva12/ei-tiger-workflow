"""
The helper agents, by name: one entry point for every helper the web
console calls from a dialog or a button (``forge_admin.helpers``, configured
in ``helpers.yaml``).

``POST /organizations/{org}/helpers/{name}`` takes the helper's input as its
body (its kit's). A ``sync`` helper answers it: what it made, its notes for
the person, and its model. A ``background`` one answers 202 with its job,
and ``GET …/helpers/{name}/jobs`` lists its jobs until it's done.

Running a helper, and dismissing its jobs, takes the permission
``helpers.yaml`` gives it in the organization; reading its jobs takes
``organizations:read``. Helpers run on the assistant's models, so they're
there when the assistant is; their calls count as the assistant's for the
organization.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Path, Request, Response, status
from fastapi.exceptions import RequestValidationError
from forge_common.adk.usage import Attribution, price_from
from google.adk.models import BaseLlm
from pydantic import BaseModel, ValidationError

from forge_admin.api.routes.common import NodeId, Session
from forge_admin.assistant.forge import APP_NAME
from forge_admin.assistant.runtime import AgentRuntime
from forge_admin.auth.access import (
    UUID_PATTERN,
    CurrentUser,
    Enforcer,
    Level,
    Scope,
    authorize,
)
from forge_admin.db.audit import UtcDateTime
from forge_admin.helpers import jobs
from forge_admin.helpers.executor import Helper, Helpers, perform
from forge_admin.helpers.kit import HelperError, HelperRun, Refused
from forge_admin.models import HelperJob
from forge_admin.overview.recording import ASSISTANT

logger = logging.getLogger(__name__)

router = APIRouter(tags=["helper agents"])

READ = "organizations:read"
NOT_SET_UP = (
    "The helper agents run on the assistant's models, which aren't set up here."
)

HelperName = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
JobId = Annotated[str, Path(pattern=rf"^{UUID_PATTERN}$")]


class HelperAnswer(BaseModel):
    """A sync helper's answer."""

    helper: str
    #: What it made: its kit's result.
    result: dict[str, Any]
    #: For the person: something to check, attach or decide.
    notes: list[str]
    #: The model it ran on (``provider/model``).
    model: str


class HelperJobRead(BaseModel):
    """A background helper's job."""

    id: str
    organization_id: str
    helper: str
    #: What it was asked: its kit's input.
    input: dict[str, Any]
    #: ``running``, ``done`` or ``failed``.
    status: str
    #: What it made, once done.
    result: dict[str, Any] | None
    notes: list[str]
    error: str | None
    model: str | None
    created_at: UtcDateTime
    created_by: str
    updated_at: UtcDateTime


def _job(row: HelperJob) -> HelperJobRead:
    return HelperJobRead.model_validate(
        {name: getattr(row, name) for name in HelperJobRead.model_fields}
    )


def _helper(request: Request, name: str) -> Helper:
    """:raises HTTPException: 404 when there's no helper by that name."""
    helpers: Helpers | None = getattr(request.app.state, "helpers", None)
    helper = helpers.get(name) if helpers is not None else None
    if helper is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"There's no helper called {name}"
        )
    return helper


def assistant_model(request: Request) -> BaseLlm:
    """
    The assistant's models, or the model its agent runs on (a test's).

    :raises HTTPException: 503 when the assistant isn't set up.
    """
    runtime: AgentRuntime | None = getattr(request.app.state, "agents", None)
    if runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)
    if runtime.unavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, runtime.unavailable)
    models = runtime.models.get(APP_NAME)
    if models is not None:
        return models
    runner = runtime.runners.get(APP_NAME)
    model = getattr(runner.agent, "model", None) if runner is not None else None
    if isinstance(model, BaseLlm):
        return model
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)


@router.post(
    "/organizations/{organization_id}/helpers/{name}",
    responses={202: {"model": HelperJobRead}},
)
async def call_helper(
    organization_id: NodeId,
    name: HelperName,
    body: Annotated[dict[str, Any], Body()],
    request: Request,
    response: Response,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> HelperAnswer | HelperJobRead:
    """
    Have a helper agent do its work: the body is its input. A sync helper
    answers with what it made; a background one with its job (202).
    \f
    :raises HTTPException: 404 when there's no such helper; 403 without its
        permission; 422 for input it doesn't take; 503 when the assistant
        isn't set up; 502 when the model fails or answers nothing usable;
        504 when it takes too long.
    """
    helper = _helper(request, name)
    spec = helper.spec
    await authorize(
        session, enforcer, user, spec.permission, Scope(Level.ORG, organization_id)
    )
    try:
        given = helper.kit.input.model_validate(body)
    except ValidationError as error:
        raise RequestValidationError(
            [
                {**problem, "loc": ("body", *problem["loc"])}
                for problem in error.errors(include_url=False)
            ]
        ) from None
    model = assistant_model(request)
    state = request.app.state
    run = HelperRun(name, organization_id, user, state)
    try:
        message = await helper.kit.prepare(run, given, session)
    except Refused as refused:
        raise HTTPException(refused.status_code, refused.detail) from None
    runtime: AgentRuntime = state.agents
    usage: dict[str, Any] = {
        # It's the assistant's work, done for this organization.
        "attribution": Attribution(
            organization_id, "assistant", ASSISTANT, "Assistant", user_id=user
        ),
        "usage": runtime.usage.record if runtime.usage is not None else None,
        "price": price_from(model),
    }
    if spec.run == "background":
        job = await jobs.start(
            session, helper, run, given, message=message, model=model, **usage
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return _job(job)
    # Nothing's written: the session isn't needed while the model works.
    await session.close()
    try:
        finished = await perform(
            helper, run, given, message=message, model=model, **usage
        )
    except HelperError as error:
        raise HTTPException(error.status_code, str(error)) from None
    logger.info(
        "%s had the %s helper work in organization %s (%s)",
        user,
        name,
        organization_id,
        finished.model,
    )
    return HelperAnswer(
        helper=name, result=finished.result, notes=finished.notes, model=finished.model
    )


@router.get("/organizations/{organization_id}/helpers/{name}/jobs")
async def list_helper_jobs(
    organization_id: NodeId,
    name: HelperName,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[HelperJobRead]:
    """
    A background helper's jobs in the organization from the last week,
    newest first. One that should have finished long ago stopped with the
    process it ran in, and is said to have failed.
    \f
    :raises HTTPException: 404 when there's no such helper; 403 without
        organizations:read.
    """
    helper = _helper(request, name)
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    return [_job(row) for row in await jobs.listed(session, organization_id, helper)]


@router.delete(
    "/organizations/{organization_id}/helpers/{name}/jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_helper_job(
    organization_id: NodeId,
    name: HelperName,
    job_id: JobId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> Response:
    """
    Dismiss a job from the list. One still running goes on, and what it
    makes is kept when it's done.
    \f
    :raises HTTPException: 404 when there's no such helper or job; 403
        without the helper's permission.
    """
    helper = _helper(request, name)
    await authorize(
        session,
        enforcer,
        user,
        helper.spec.permission,
        Scope(Level.ORG, organization_id),
    )
    if not await jobs.dismiss(session, organization_id, helper, job_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "The organization has no such job"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
