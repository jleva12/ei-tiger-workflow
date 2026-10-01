"""
An organization's workflows: the ``forge.workflow/v1`` documents its members
build in the web console, saved as they're edited and shared by the whole
organization.

Reading needs ``organizations:read`` in the organization; making, saving and
deleting need ``workflows:manage`` (its admins and members have it by default).
Every workflow is kept with its organization, and is only ever found through
it.

Running one needs ``workflows:run``: the run is submitted to the async
worker (``forge_admin.workflow_runs``) with the workflow as it is then, and
acts as the member who ran it. Its runs are among the organization's background
tasks, where they're followed, and their approvals decided.

A save names the revision it was made from and answers 409 when someone
saved the workflow since; the web console then offers their version or
saving over it.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError

from forge_admin.api.routes.background_tasks import background_tasks_client, refusal
from forge_admin.api.routes.common import NodeId, Session
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.background_tasks import BackgroundTasksError
from forge_admin.db.audit import UtcDateTime
from forge_admin.document_store import now
from forge_admin.embedding import Embedding, EmbeddingError
from forge_admin.models import User
from forge_admin.workflow_runs import (
    WORKFLOW_LABEL,
    RunInputError,
    check_input,
    start_run,
)
from forge_admin.workflows import (
    ID_PATTERN,
    WorkflowConflict,
    WorkflowError,
    WorkflowIdTaken,
    WorkflowStore,
    checked_document,
    new_workflow_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workflows"])

MANAGE = "workflows:manage"
RUN = "workflows:run"
NOT_FOUND = "The organization has no such workflow"
CONFLICT = (
    "Someone else saved this workflow since your last save; load their version "
    "or save yours over it"
)
UNAVAILABLE = "The workflows' database isn't answering; try again shortly"
QUEUE_UNAVAILABLE = "The async worker's queue isn't answering; try again shortly"
# New IDs tried before giving up.
ID_ATTEMPTS = 3

WorkflowId = Annotated[str, Path(pattern=ID_PATTERN)]


class WorkflowRead(BaseModel):
    """An organization's workflow: its document and who saved it when."""

    id: str
    organization_id: str
    #: Goes up by one with every save; a save names the one it was made from.
    revision: int
    #: The forge.workflow/v1 document.
    document: dict[str, Any]
    created_at: UtcDateTime
    created_by: str
    updated_at: UtcDateTime
    updated_by: str
    #: Who saved it last, by name as they were then.
    updated_by_name: str


class WorkflowCreate(BaseModel):
    """A new workflow. The API makes its ID; a client never chooses one."""

    model_config = ConfigDict(extra="forbid")

    #: The document; its ID, organization and times are set by the API.
    document: dict[str, Any]


class WorkflowUpdate(BaseModel):
    """The next version of a workflow."""

    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any]
    #: The revision this version was made from.
    revision: int = Field(ge=1)


class RunCreate(BaseModel):
    """A run of a workflow."""

    model_config = ConfigDict(extra="forbid")

    #: What the run starts with: it must fit the start step's fields.
    input: Any = None


class RunStarted(BaseModel):
    """A run on its way: the job that will run it."""

    queue: str
    key: str
    workflow_id: str
    #: The revision it runs, whatever is saved afterwards.
    revision: int


def workflow_store(request: Request) -> WorkflowStore:
    """
    :param request: The request.
    :return: The workflows' store.
    :raises HTTPException: 503 when MongoDB isn't set up.
    """
    store: WorkflowStore | None = request.app.state.workflows
    if store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Workflows aren't set up: set FORGE_ADMIN_MONGO_URI",
        )
    return store


def run_queue(request: Request) -> Embedding:
    """
    :return: The async worker's job queues, where runs are submitted.
    :raises HTTPException: 503 when they aren't set up.
    """
    embedding: Embedding | None = request.app.state.embedding
    if embedding is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Workflow runs aren't set up: set FORGE_ADMIN_EMBEDDING_REDIS_URL",
        )
    return embedding


async def find_workflow(
    request: Request, organization_id: str, workflow_id: str
) -> dict[str, Any]:
    """
    :return: The organization's workflow's record.
    :raises HTTPException: 404 when the organization has no such workflow; 503 when
        MongoDB isn't set up or isn't answering.
    """
    store = workflow_store(request)
    try:
        record = await store.get(organization_id, workflow_id)
    except PyMongoError as error:
        raise _unavailable(error) from None
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return record


def _read(record: dict[str, Any]) -> WorkflowRead:
    return WorkflowRead.model_validate({**record, "id": record["_id"]})


async def name_of(session: Session, user: str) -> str:
    """:return: A user's name, or their email, or their ID when unknown."""
    person = await session.get(User, user)
    if person is None:
        return user
    return f"{person.first_name} {person.last_name}".strip() or person.email


def _checked(
    request: Request, document: dict[str, Any], **fields: Any
) -> dict[str, Any]:
    try:
        return checked_document(
            document, max_bytes=request.app.state.settings.workflows_max_bytes, **fields
        )
    except WorkflowError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None


def _unavailable(error: PyMongoError) -> HTTPException:
    logger.warning("The workflows' MongoDB failed: %s", error)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE)


@router.get("/organizations/{organization_id}/workflows")
async def list_workflows(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[WorkflowRead]:
    """
    List an organization's workflows, most recently changed first, each with its
    whole document.
    \f
    :param organization_id: The organization.
    :param request: The request.
    :param user: The signed-in user.
    :param session: The database session.
    :param enforcer: The Casbin enforcer.
    :return: The workflows.
    :raises HTTPException: 403 without organizations:read in the organization; 503 when
        MongoDB isn't set up or isn't answering.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    store = workflow_store(request)
    try:
        return [_read(record) for record in await store.list(organization_id)]
    except PyMongoError as error:
        raise _unavailable(error) from None


@router.post(
    "/organizations/{organization_id}/workflows", status_code=status.HTTP_201_CREATED
)
async def create_workflow(
    organization_id: NodeId,
    body: WorkflowCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> WorkflowRead:
    """
    Make a workflow in the organization, from a document.
    \f
    :param organization_id: The organization.
    :param body: The document.
    :param request: The request.
    :param user: The signed-in user.
    :param session: The database session.
    :param enforcer: The Casbin enforcer.
    :return: The workflow, at revision 1.
    :raises HTTPException: 403 without workflows:manage in the organization; 422
        for a document that isn't a workflow; 503 when MongoDB isn't set up
        or isn't answering.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = workflow_store(request)
    made = now()
    name = await name_of(session, user)

    def record_for(workflow_id: str) -> dict[str, Any]:
        document = _checked(
            request,
            body.document,
            workflow_id=workflow_id,
            organization_id=organization_id,
            created_at=made,
            updated_at=made,
        )
        return {
            "_id": workflow_id,
            "organization_id": organization_id,
            "revision": 1,
            "document": document,
            "created_at": made,
            "created_by": user,
            "updated_at": made,
            "updated_by": user,
            "updated_by_name": name,
            "deleted_at": None,
        }

    # Ten random characters rarely meet an ID in use; when they do, another.
    record = record_for(new_workflow_id())
    for attempt in range(1, ID_ATTEMPTS + 1):
        try:
            await store.create(record)
            break
        except WorkflowIdTaken:
            if attempt == ID_ATTEMPTS:
                raise
            record = record_for(new_workflow_id())
        except PyMongoError as error:
            raise _unavailable(error) from None
    logger.info(
        "%s created workflow %s in organization %s",
        user,
        record["_id"],
        organization_id,
    )
    return _read(record)


@router.get("/organizations/{organization_id}/workflows/{workflow_id}")
async def get_workflow(
    organization_id: NodeId,
    workflow_id: WorkflowId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> WorkflowRead:
    """
    Read one of the organization's workflows.
    \f
    :param organization_id: The organization.
    :param workflow_id: The workflow.
    :param request: The request.
    :param user: The signed-in user.
    :param session: The database session.
    :param enforcer: The Casbin enforcer.
    :return: The workflow.
    :raises HTTPException: 403 without organizations:read in the organization; 404 when the
        organization has no such workflow; 503 when MongoDB isn't set up or isn't
        answering.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    store = workflow_store(request)
    try:
        record = await store.get(organization_id, workflow_id)
    except PyMongoError as error:
        raise _unavailable(error) from None
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return _read(record)


@router.put("/organizations/{organization_id}/workflows/{workflow_id}")
async def save_workflow(
    organization_id: NodeId,
    workflow_id: WorkflowId,
    body: WorkflowUpdate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> WorkflowRead:
    """
    Save the next version of a workflow, made from the revision named.
    \f
    :param organization_id: The organization.
    :param workflow_id: The workflow.
    :param body: The document and the revision it was made from.
    :param request: The request.
    :param user: The signed-in user.
    :param session: The database session.
    :param enforcer: The Casbin enforcer.
    :return: The workflow, at its next revision.
    :raises HTTPException: 403 without workflows:manage in the organization; 404
        when the organization has no such workflow; 409 when it was saved since that
        revision; 422 for a document that isn't a workflow; 503 when MongoDB
        isn't set up or isn't answering.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = workflow_store(request)
    try:
        current = await store.get(organization_id, workflow_id)
    except PyMongoError as error:
        raise _unavailable(error) from None
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    if current["revision"] != body.revision:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT)
    saved_at = now()
    document = _checked(
        request,
        body.document,
        workflow_id=workflow_id,
        organization_id=organization_id,
        created_at=current["created_at"],
        updated_at=saved_at,
    )
    changes = {
        "document": document,
        "updated_at": saved_at,
        "updated_by": user,
        "updated_by_name": await name_of(session, user),
    }
    try:
        saved = await store.replace(
            organization_id, workflow_id, body.revision, changes
        )
    except WorkflowConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    if saved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return _read(saved)


@router.delete(
    "/organizations/{organization_id}/workflows/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_workflow(
    organization_id: NodeId,
    workflow_id: WorkflowId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> Response:
    """
    Delete one of the organization's workflows. Its ID is never used again.
    \f
    :param organization_id: The organization.
    :param workflow_id: The workflow.
    :param request: The request.
    :param user: The signed-in user.
    :param session: The database session.
    :param enforcer: The Casbin enforcer.
    :return: An empty response.
    :raises HTTPException: 403 without workflows:manage in the organization; 404
        when the organization has no such workflow; 503 when MongoDB isn't set up or
        isn't answering.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = workflow_store(request)
    try:
        deleted = await store.delete(organization_id, workflow_id, by=user)
    except PyMongoError as error:
        raise _unavailable(error) from None
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    logger.info(
        "%s deleted workflow %s in organization %s", user, workflow_id, organization_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/organizations/{organization_id}/workflows/{workflow_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_workflow(
    organization_id: NodeId,
    workflow_id: WorkflowId,
    body: RunCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> RunStarted:
    """
    Run a workflow, as it is saved now, as the caller: the workflows its
    run-workflow steps start run as the caller too, with their permissions.
    The run is one of the organization's background tasks.
    \f
    :param organization_id: The organization.
    :param workflow_id: The workflow.
    :param body: The input.
    :param request: The request.
    :param user: The signed-in user, whom the run acts as.
    :param session: The database session.
    :param enforcer: The Casbin enforcer.
    :return: The job that will run it.
    :raises HTTPException: 403 without workflows:run in the organization; 404 when
        the organization has no such workflow; 422 for input that doesn't fit its
        start step; 503 when runs or MongoDB aren't set up or answering.
    """
    await authorize(session, enforcer, user, RUN, Scope(Level.ORG, organization_id))
    record = await find_workflow(request, organization_id, workflow_id)
    try:
        check_input(record["document"], body.input)
    except RunInputError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    queue = run_queue(request)
    try:
        started = await start_run(
            queue,
            record,
            input=body.input,
            run_as=user,
            run_as_name=await name_of(session, user),
            trigger={"type": "manual", "by": user},
        )
    except EmbeddingError as error:
        logger.warning("Couldn't submit a run of workflow %s: %s", workflow_id, error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, QUEUE_UNAVAILABLE
        ) from None
    logger.info(
        "%s ran workflow %s at revision %s in organization %s",
        user,
        workflow_id,
        record["revision"],
        organization_id,
    )
    return RunStarted(**started, workflow_id=workflow_id, revision=record["revision"])


@router.get("/organizations/{organization_id}/workflows/{workflow_id}/runs")
async def list_workflow_runs(
    organization_id: NodeId,
    workflow_id: WorkflowId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """
    List a workflow's runs, newest first: the organization's background tasks that
    ran it, each with its status and whether it waits (for a time, or for
    someone's approval).
    \f
    :return: ``{"items": [...], "total"}``, as the organization's background tasks.
    :raises HTTPException: 403 without organizations:read in the organization; 503 when
        background tasks aren't set up or the worker is unavailable.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    client = background_tasks_client(request)
    try:
        return await client.tasks(
            tenant=organization_id,
            task_types=["workflows"],
            labels={WORKFLOW_LABEL: workflow_id},
            limit=limit,
            offset=offset,
        )
    except BackgroundTasksError as error:
        raise refusal(error) from None
