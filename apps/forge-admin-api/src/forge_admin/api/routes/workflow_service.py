"""
The workflow service routes: what the async worker's workflows task asks of
this API while it runs an organization's workflow (packages/python/tasks/workflows,
``services/admin.py``), always as the member the run acts as.

Only this API knows an organization's workflows, so a run's run-workflow
steps come here rather than holding any token themselves. The worker
authenticates with FORGE_ADMIN_WORKFLOWS_TOKEN (FORGE_WORKFLOWS_TOKEN in
.env.common) and names the organization and the member in every call; each
route then checks the member, as it would for them: they must still hold
``workflows:run`` in the organization. A member removed from the
organization fails the run's next such step.

These routes are mounted at the root, outside the API prefix, and answer
only to the token; keep ``/internal`` off any public ingress.
"""

import hmac
import logging
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import Session
from forge_admin.api.routes.workflows import (
    RUN,
    WorkflowId,
    find_workflow,
    name_of,
    run_queue,
)
from forge_admin.auth.access import UUID_PATTERN, Enforcer, Level, Scope, authorize
from forge_admin.embedding import EmbeddingError
from forge_admin.workflow_runs import RunInputError, check_input, start_run

logger = logging.getLogger(__name__)

PREFIX = "/internal/workflows"
# How deep runs may start runs; the worker's own limit is lower.
MAX_DEPTH = 20

OrganizationId = Annotated[str, StringConstraints(pattern=rf"^{UUID_PATTERN}$")]
UserId = Annotated[str, StringConstraints(min_length=1, max_length=255)]


async def worker(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> None:
    """
    FastAPI dependency: the caller is the async worker, with the workflows
    token, compared in constant time.

    :raises HTTPException: 401 without it, or when this API has none set
        (the worker then fails the step, saying the token is wrong).
    """
    expected = request.app.state.settings.workflows_token
    scheme, _, token = (authorization or "").partition(" ")
    if (
        expected is None
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(
            token.strip().encode(), expected.get_secret_value().encode()
        )
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Send FORGE_ADMIN_WORKFLOWS_TOKEN as Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(
    prefix=PREFIX,
    tags=["workflow service"],
    dependencies=[Depends(worker)],
    include_in_schema=False,
)


class Acting(BaseModel):
    """The organization a run is the organization's, and the member it acts as."""

    model_config = ConfigDict(extra="forbid")

    organization_id: OrganizationId
    user_id: UserId


class ChildRun(Acting):
    input: Any = None
    #: The run and step that start it: ``<run>/<step key>``.
    parent: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    depth: int = Field(ge=1, le=MAX_DEPTH)


async def _acting(session: AsyncSession, enforcer: Any, body: Acting) -> None:
    """
    Check the member may still run the organization's workflows.

    :raises HTTPException: 403 saying they lack ``workflows:run``; 404 for
        no such organization.
    """
    scope = Scope(Level.ORG, body.organization_id)
    try:
        await authorize(session, enforcer, body.user_id, RUN, scope)
    except HTTPException as error:
        if error.status_code == status.HTTP_403_FORBIDDEN:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "The member this run acts as can't run workflows in the "
                f"organization any more: it requires {RUN}",
            ) from None
        raise


# ---------------------------------------------------------------- workflows


@router.post("/workflows/{workflow_id}/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_child_run(
    workflow_id: WorkflowId,
    body: ChildRun,
    request: Request,
    session: Session,
    enforcer: Enforcer,
) -> dict[str, Any]:
    """
    Start another of the organization's workflows from a run's step, as the
    same member. The same step asking again is the same run.

    :return: ``{"queue", "key", "workflow_name"}``.
    :raises HTTPException: 403 without ``workflows:run``; 404 when the
        organization has no such workflow; 422 for input that doesn't fit its start step.
    """
    await _acting(session, enforcer, body)
    record = await find_workflow(request, body.organization_id, workflow_id)
    try:
        check_input(record["document"], body.input)
    except RunInputError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    try:
        started = await start_run(
            run_queue(request),
            record,
            input=body.input,
            run_as=body.user_id,
            run_as_name=await name_of(session, body.user_id),
            trigger={"type": "workflow", "parent": body.parent},
            parent=body.parent,
            depth=body.depth,
        )
    except EmbeddingError as error:
        logger.warning("Couldn't submit a run of workflow %s: %s", workflow_id, error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The async worker's queue isn't answering",
        ) from None
    return {**started, "workflow_name": str(record["document"].get("name") or "")}
