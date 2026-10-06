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

An agent has versions, as chat agents do (``adk_workflows.versioned_store``):
the draft the builder saves (``PUT …/draft``), publishing it as a version
nothing changes (once it can be published: ``runs.check_publishable``),
starting the next version from a published one, discarding a draft, and
reading a published version. One saved before versions existed is a draft
of version 1 until it's first published.

These are the organization's agents, not the assistant's (``assistant.py``).
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError

from forge_admin.adk_workflows.documents import (
    ID_PATTERN,
    AgentConflict,
    AgentError,
    AgentIdTaken,
    AgentStore,
)
from forge_admin.adk_workflows.resources import RunResources
from forge_admin.adk_workflows.runs import check_publishable
from forge_admin.adk_workflows.versioned_store import VersionState, status_of
from forge_admin.adk_workflows.versions import workflow_finder
from forge_admin.api.routes.common import NodeId, Session, name_of
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.db.audit import UtcDateTime

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
VersionNumber = Annotated[int, Path(ge=1)]


class AgentRead(BaseModel):
    """
    An organization's agent: the document the builder shows, where it is
    between draft and published, and who saved it when.
    """

    id: str
    organization_id: str
    #: Goes up by one with every change; a save names the one it was made from.
    revision: int
    #: The forge.agent/v1 document: its draft, or its latest published
    #: version when it has no draft.
    document: dict[str, Any]
    #: draft (never published), published, or published+draft.
    status: str
    has_draft: bool
    #: The version the draft will be published as; null without a draft.
    draft_version: int | None
    #: The latest published version, which ``ag_…`` runs; null before the first.
    published_version: int | None
    published_at: UtcDateTime | None
    created_at: UtcDateTime
    created_by: str
    updated_at: UtcDateTime
    updated_by: str
    #: Who saved it last, by name as they were then.
    updated_by_name: str | None


class VersionRead(BaseModel):
    """One published version."""

    version: int
    published_at: UtcDateTime
    published_by: str
    published_by_name: str | None


class AgentDetail(AgentRead):
    #: Its published versions, newest first.
    versions: list[VersionRead]


class VersionDetail(VersionRead):
    #: The version's document, ``version`` set.
    document: dict[str, Any]


class AgentCreate(BaseModel):
    """A new agent. The API makes its ID; a client never chooses one."""

    model_config = ConfigDict(extra="forbid")

    #: The document; its ID, organization and times are set by the API.
    document: dict[str, Any]


class AgentUpdate(BaseModel):
    """The agent's draft, as next saved."""

    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any]
    #: The revision this save was made from.
    revision: int = Field(ge=1)


class Publish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The draft's revision being published: one saved since is refused.
    revision: int = Field(ge=1)


class NewVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The published version to start from; the latest when omitted.
    from_version: int | None = Field(default=None, ge=1)


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
    return AgentRead.model_validate(
        {
            **record,
            "id": record["_id"],
            "status": status_of(record),
            "has_draft": bool(record.get("has_draft")),
        }
    )


def _state(error: VersionState) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, {"code": error.code, "msg": str(error)}
    )


def _found(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return record


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
    name = await name_of(session, user)
    # Ten random characters rarely meet an ID in use; when they do, another.
    for attempt in range(1, ID_ATTEMPTS + 1):
        try:
            record = await store.create_agent(
                organization_id,
                body.document,
                by=user,
                by_name=name,
                max_bytes=request.app.state.settings.agents_max_bytes,
            )
            break
        except AgentIdTaken:
            if attempt == ID_ATTEMPTS:
                raise
        except AgentError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
            ) from None
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
) -> AgentDetail:
    """
    Read one of the organization's agents, with its published versions.
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
        record = _found(await store.get(organization_id, agent_id))
        versions = await store.versions(organization_id, agent_id)
    except PyMongoError as error:
        raise _unavailable(error) from None
    return AgentDetail.model_validate(
        {**_read(record).model_dump(), "versions": versions}
    )


@router.put("/organizations/{organization_id}/agents/{agent_id}/draft")
@router.put("/organizations/{organization_id}/agents/{agent_id}", deprecated=True)
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
    Save the agent's draft, made from the revision named. (``PUT …/{agent_id}``
    is the same, for consoles from before versions.)
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
        revision, or it's published with no draft (``NO_DRAFT``); 422 for a
        document that isn't an agent; 503 when MongoDB isn't set up or isn't
        answering.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = agent_store(request)
    name = await name_of(session, user)
    try:
        record = await store.save_draft(
            organization_id,
            agent_id,
            body.revision,
            body.document,
            by=user,
            by_name=name,
            max_bytes=request.app.state.settings.agents_max_bytes,
        )
    except AgentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except VersionState as error:
        raise _state(error) from None
    except AgentError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    return _read(_found(record))


@router.post("/organizations/{organization_id}/agents/{agent_id}/publish")
async def publish_agent(
    organization_id: NodeId,
    agent_id: AgentId,
    body: Publish,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AgentRead:
    """
    Publish the draft as its version, once it can be: it has a start, builds,
    and runs only what doesn't change (other workflows' and agents'
    published versions). It never changes afterwards.
    \f
    :raises HTTPException: 403 without agents:manage; 404; 409 when saved
        since, or there's no draft (``NO_DRAFT``); 422 with why it can't be
        published yet (``{"msg", "problems"}``).
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = agent_store(request)
    try:
        current = _found(await store.get(organization_id, agent_id))
        if current.get("has_draft"):
            problems = await check_publishable(
                current["document"],
                workflow_finder(store, organization_id),
                RunResources(
                    organization_id,
                    chat_agents=getattr(request.app.state, "chat_agents", None),
                    sessions=request.app.state.sessionmaker,
                ),
            )
            if problems:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    {"msg": "It can't be published yet", "problems": problems},
                )
        name = await name_of(session, user)
        record = await store.publish(
            organization_id, agent_id, body.revision, by=user, by_name=name
        )
    except AgentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except VersionState as error:
        raise _state(error) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    record = _found(record)
    logger.info(
        "%s published agent %s version %s",
        user,
        agent_id,
        record["published_version"],
    )
    return _read(record)


@router.post("/organizations/{organization_id}/agents/{agent_id}/versions")
async def start_version(
    organization_id: NodeId,
    agent_id: AgentId,
    body: NewVersion,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AgentRead:
    """
    Start the next version: a draft copied from a published version (the
    latest by default).
    \f
    :raises HTTPException: 404 (or no such version); 409 ``DRAFT_EXISTS`` or
        ``NOT_PUBLISHED``.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    name = await name_of(session, user)
    try:
        record = await agent_store(request).start_version(
            organization_id,
            agent_id,
            from_version=body.from_version,
            by=user,
            by_name=name,
        )
    except VersionState as error:
        raise _state(error) from None
    except AgentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    return _read(_found(record))


@router.delete("/organizations/{organization_id}/agents/{agent_id}/draft")
async def discard_draft(
    organization_id: NodeId,
    agent_id: AgentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AgentRead:
    """
    Discard the draft: the agent is its latest published version again.
    \f
    :raises HTTPException: 404; 409 ``NO_DRAFT``, or ``NOT_PUBLISHED`` (delete
        the agent instead).
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    name = await name_of(session, user)
    try:
        record = await agent_store(request).discard_draft(
            organization_id, agent_id, by=user, by_name=name
        )
    except VersionState as error:
        raise _state(error) from None
    except AgentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    return _read(_found(record))


@router.get("/organizations/{organization_id}/agents/{agent_id}/versions/{version}")
async def get_version(
    organization_id: NodeId,
    agent_id: AgentId,
    version: VersionNumber,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> VersionDetail:
    """One published version, its document included."""
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    try:
        found = await agent_store(request).version(organization_id, agent_id, version)
    except PyMongoError as error:
        raise _unavailable(error) from None
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The agent has no such version")
    return VersionDetail.model_validate(found)


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
