"""
An organization's agents: the ``forge.agent/v1`` documents its members build
in the web console's agent builder, saved as they're edited and shared by the
whole organization.

Reading needs ``organizations:read`` in the organization; making, saving and
deleting need ``agents:manage`` (its admins and members have it by default).
Every agent is kept with its organization, and is only ever found through it.

A save names the revision it was made from and answers 409 when someone
saved the agent since; the web console then offers their version or saving
over it.

These are the organization's agents, not the assistant's (``agents.py``).
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError

from forge_admin.agent_documents import (
    ID_PATTERN,
    AgentConflict,
    AgentError,
    AgentIdTaken,
    AgentStore,
    checked_document,
    new_agent_id,
)
from forge_admin.api.routes.common import NodeId, Session, name_of
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.db.audit import UtcDateTime
from forge_admin.document_store import now

logger = logging.getLogger(__name__)

router = APIRouter(tags=["organization agents"])

MANAGE = "agents:manage"
NOT_FOUND = "The organization has no such agent"
CONFLICT = (
    "Someone else saved this agent since your last save; load their version "
    "or save yours over it"
)
UNAVAILABLE = "The agents' database isn't answering; try again shortly"
# New IDs tried before giving up.
ID_ATTEMPTS = 3

AgentId = Annotated[str, Path(pattern=ID_PATTERN)]


class AgentRead(BaseModel):
    """An organization's agent: its document and who saved it when."""

    id: str
    organization_id: str
    #: Goes up by one with every save; a save names the one it was made from.
    revision: int
    #: The forge.agent/v1 document.
    document: dict[str, Any]
    created_at: UtcDateTime
    created_by: str
    updated_at: UtcDateTime
    updated_by: str
    #: Who saved it last, by name as they were then.
    updated_by_name: str


class AgentCreate(BaseModel):
    """A new agent. The API makes its ID; a client never chooses one."""

    model_config = ConfigDict(extra="forbid")

    #: The document; its ID, organization and times are set by the API.
    document: dict[str, Any]


class AgentUpdate(BaseModel):
    """The next version of an agent."""

    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any]
    #: The revision this version was made from.
    revision: int = Field(ge=1)


def agent_store(request: Request) -> AgentStore:
    """
    :param request: The request.
    :return: The agents' store.
    :raises HTTPException: 503 when MongoDB isn't set up.
    """
    store: AgentStore | None = request.app.state.organization_agents
    if store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Agents aren't set up: set FORGE_ADMIN_MONGO_URI",
        )
    return store


def _read(record: dict[str, Any]) -> AgentRead:
    return AgentRead.model_validate({**record, "id": record["_id"]})


def _checked(
    request: Request, document: dict[str, Any], **fields: Any
) -> dict[str, Any]:
    try:
        return checked_document(
            document, max_bytes=request.app.state.settings.agents_max_bytes, **fields
        )
    except AgentError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None


def _unavailable(error: PyMongoError) -> HTTPException:
    logger.warning("The agents' MongoDB failed: %s", error)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE)


@router.get("/organizations/{organization_id}/agents")
async def list_agents(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[AgentRead]:
    """
    List an organization's agents, most recently changed first, each with its
    whole document.
    \f
    :param organization_id: The organization.
    :param request: The request.
    :param user: The signed-in user.
    :param session: The database session.
    :param enforcer: The Casbin enforcer.
    :return: The agents.
    :raises HTTPException: 403 without organizations:read in the organization; 503 when
        MongoDB isn't set up or isn't answering.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    store = agent_store(request)
    try:
        return [_read(record) for record in await store.list(organization_id)]
    except PyMongoError as error:
        raise _unavailable(error) from None


@router.post(
    "/organizations/{organization_id}/agents", status_code=status.HTTP_201_CREATED
)
async def create_agent(
    organization_id: NodeId,
    body: AgentCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AgentRead:
    """
    Make an agent in the organization, from a document.
    \f
    :param organization_id: The organization.
    :param body: The document.
    :param request: The request.
    :param user: The signed-in user.
    :param session: The database session.
    :param enforcer: The Casbin enforcer.
    :return: The agent, at revision 1.
    :raises HTTPException: 403 without agents:manage in the organization; 422
        for a document that isn't an agent; 503 when MongoDB isn't set up
        or isn't answering.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = agent_store(request)
    made = now()
    name = await name_of(session, user)

    def record_for(agent_id: str) -> dict[str, Any]:
        document = _checked(
            request,
            body.document,
            agent_id=agent_id,
            organization_id=organization_id,
            created_at=made,
            updated_at=made,
        )
        return {
            "_id": agent_id,
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
    record = record_for(new_agent_id())
    for attempt in range(1, ID_ATTEMPTS + 1):
        try:
            await store.create(record)
            break
        except AgentIdTaken:
            if attempt == ID_ATTEMPTS:
                raise
            record = record_for(new_agent_id())
        except PyMongoError as error:
            raise _unavailable(error) from None
    logger.info(
        "%s created agent %s in organization %s",
        user,
        record["_id"],
        organization_id,
    )
    return _read(record)


@router.get("/organizations/{organization_id}/agents/{agent_id}")
async def get_agent(
    organization_id: NodeId,
    agent_id: AgentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AgentRead:
    """
    Read one of the organization's agents.
    \f
    :param organization_id: The organization.
    :param agent_id: The agent.
    :param request: The request.
    :param user: The signed-in user.
    :param session: The database session.
    :param enforcer: The Casbin enforcer.
    :return: The agent.
    :raises HTTPException: 403 without organizations:read in the organization; 404 when the
        organization has no such agent; 503 when MongoDB isn't set up or isn't
        answering.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    store = agent_store(request)
    try:
        record = await store.get(organization_id, agent_id)
    except PyMongoError as error:
        raise _unavailable(error) from None
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return _read(record)


@router.put("/organizations/{organization_id}/agents/{agent_id}")
async def save_agent(
    organization_id: NodeId,
    agent_id: AgentId,
    body: AgentUpdate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AgentRead:
    """
    Save the next version of an agent, made from the revision named.
    \f
    :param organization_id: The organization.
    :param agent_id: The agent.
    :param body: The document and the revision it was made from.
    :param request: The request.
    :param user: The signed-in user.
    :param session: The database session.
    :param enforcer: The Casbin enforcer.
    :return: The agent, at its next revision.
    :raises HTTPException: 403 without agents:manage in the organization; 404
        when the organization has no such agent; 409 when it was saved since that
        revision; 422 for a document that isn't an agent; 503 when MongoDB
        isn't set up or isn't answering.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = agent_store(request)
    try:
        current = await store.get(organization_id, agent_id)
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
        agent_id=agent_id,
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
        saved = await store.replace(organization_id, agent_id, body.revision, changes)
    except AgentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    if saved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return _read(saved)


@router.delete(
    "/organizations/{organization_id}/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_agent(
    organization_id: NodeId,
    agent_id: AgentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> Response:
    """
    Delete one of the organization's agents. Its ID is never used again.
    \f
    :param organization_id: The organization.
    :param agent_id: The agent.
    :param request: The request.
    :param user: The signed-in user.
    :param session: The database session.
    :param enforcer: The Casbin enforcer.
    :return: An empty response.
    :raises HTTPException: 403 without agents:manage in the organization; 404
        when the organization has no such agent; 503 when MongoDB isn't set up or
        isn't answering.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = agent_store(request)
    try:
        deleted = await store.delete(organization_id, agent_id, by=user)
    except PyMongoError as error:
        raise _unavailable(error) from None
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    logger.info(
        "%s deleted agent %s in organization %s", user, agent_id, organization_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
