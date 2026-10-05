"""
An organization's chat agents (``forge.chat_agent/v1``): built in the web
console's Agents builder, saved as drafts as they're edited, and published
as numbered versions that never change.

Reading needs ``organizations:read`` in the organization; anything else needs
``agents:manage`` (its admins and members have it by default).

- A new agent is a draft of version 1. Saving the draft names the revision it
  was made from, and answers 409 when someone saved it since.
- Publishing checks the draft builds, freezes it as its version, and leaves
  the agent with no draft. ``POST …/versions`` starts the next version's
  draft; ``DELETE …/draft`` discards it.
- Published versions are read and exported, never changed: no route here
  changes one. The runtime (``/runtime``) runs them by ID.
"""

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from forge_agent_runtime import (
    AgentExecutor,
    BuildError,
    DocumentError,
    build_app,
    parse_document,
)
from forge_agent_runtime.starter.options import StarterOptions
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError

from forge_admin.api.routes.common import NodeId, Session, name_of
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.chat_agents.bundle import bundle
from forge_admin.chat_agents.standalone import (
    model_provider_yaml,
    standalone_document,
    starter_runtime,
)
from forge_admin.chat_agents.store import (
    ID_PATTERN,
    ChatAgentConflict,
    ChatAgentError,
    ChatAgentState,
    ChatAgentStore,
    ChatAgentTaken,
    status_of,
)
from forge_admin.db.audit import UtcDateTime

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat agents"])

READ = "organizations:read"
MANAGE = "agents:manage"
NOT_FOUND = "The organization has no such agent"
CONFLICT = (
    "Someone else saved this agent since your last save; load their version "
    "or save yours over it"
)
UNAVAILABLE = "The agents' database isn't answering; try again shortly"
ID_ATTEMPTS = 3
BASE = "/organizations/{organization_id}/chat-agents"
ONE = BASE + "/{agent_id}"

ChatAgentId = Annotated[str, Path(pattern=ID_PATTERN)]
VersionNumber = Annotated[int, Path(ge=1)]


class ChatAgentRead(BaseModel):
    """A chat agent: the document the builder shows, and where it is between draft and published."""

    id: str
    organization_id: str
    #: Goes up with every change; a draft save names the one it was made from.
    revision: int
    #: Its draft, or its latest published version when it has no draft.
    document: dict[str, Any]
    #: draft (never published), published, or published+draft (a new version in the works).
    status: str
    has_draft: bool
    #: The version the draft will be published as; null without a draft.
    draft_version: int | None
    #: The latest published version, which ``ca_…`` runs; null before the first.
    published_version: int | None
    published_at: UtcDateTime | None
    created_at: UtcDateTime
    created_by: str
    updated_at: UtcDateTime
    updated_by: str
    updated_by_name: str | None


class VersionRead(BaseModel):
    """One published version."""

    version: int
    published_at: UtcDateTime
    published_by: str
    published_by_name: str | None


class ChatAgentDetail(ChatAgentRead):
    #: Its published versions, newest first.
    versions: list[VersionRead]


class VersionDetail(VersionRead):
    #: The version's document, ``version`` set.
    document: dict[str, Any]


class ChatAgentCreate(BaseModel):
    """A new agent. The API makes its ID; a client never chooses one."""

    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any]


class DraftSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any]
    revision: int = Field(ge=1)


class Publish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The draft's revision being published: one saved since is refused.
    revision: int = Field(ge=1)


class NewVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The published version to start from; the latest when omitted.
    from_version: int | None = Field(default=None, ge=1)


class Duplicate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The copy's name; "<name> (copy)" when omitted.
    name: str | None = Field(default=None, max_length=200)


def chat_agent_store(request: Request) -> ChatAgentStore:
    """:raises HTTPException: 503 when MongoDB isn't set up."""
    store: ChatAgentStore | None = getattr(request.app.state, "chat_agents", None)
    if store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Agents aren't set up: set FORGE_ADMIN_MONGO_URI",
        )
    return store


def _read(record: dict[str, Any]) -> ChatAgentRead:
    return ChatAgentRead.model_validate(
        {
            **record,
            "id": record["_id"],
            "status": status_of(record),
            "has_draft": bool(record.get("has_draft")),
        }
    )


def _unavailable(error: PyMongoError) -> HTTPException:
    logger.warning("The chat agents' MongoDB failed: %s", error)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE)


def _state(error: ChatAgentState) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, {"code": error.code, "msg": str(error)}
    )


async def _found(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return record


@router.get(BASE)
async def list_chat_agents(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[ChatAgentRead]:
    """The organization's chat agents, most recently changed first."""
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    try:
        records = await chat_agent_store(request).list(organization_id)
    except PyMongoError as error:
        raise _unavailable(error) from None
    return [_read(record) for record in records]


@router.post(BASE, status_code=status.HTTP_201_CREATED)
async def create_chat_agent(
    organization_id: NodeId,
    body: ChatAgentCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ChatAgentRead:
    """
    Make a chat agent from a document: a draft of version 1, with an ID of its own.
    \f
    :raises HTTPException: 403 without agents:manage; 422 for a document that
        isn't a chat agent; 503 when MongoDB isn't set up or answering.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = chat_agent_store(request)
    name = await name_of(session, user)
    max_bytes = request.app.state.settings.agents_max_bytes
    for attempt in range(1, ID_ATTEMPTS + 1):
        try:
            record = await store.create_agent(
                organization_id,
                body.document,
                by=user,
                by_name=name,
                max_bytes=max_bytes,
            )
            break
        except ChatAgentTaken:
            if attempt == ID_ATTEMPTS:
                raise
        except ChatAgentError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
            ) from None
        except PyMongoError as error:
            raise _unavailable(error) from None
    logger.info(
        "%s created chat agent %s in organization %s",
        user,
        record["_id"],
        organization_id,
    )
    return _read(record)


@router.get(ONE)
async def get_chat_agent(
    organization_id: NodeId,
    agent_id: ChatAgentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ChatAgentDetail:
    """A chat agent with its published versions."""
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    store = chat_agent_store(request)
    try:
        record = await _found(await store.get(organization_id, agent_id))
        versions = await store.versions(organization_id, agent_id)
    except PyMongoError as error:
        raise _unavailable(error) from None
    return ChatAgentDetail.model_validate(
        {**_read(record).model_dump(), "versions": versions}
    )


@router.put(ONE + "/draft")
async def save_draft(
    organization_id: NodeId,
    agent_id: ChatAgentId,
    body: DraftSave,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ChatAgentRead:
    """
    Save the agent's draft, made from ``revision``.
    \f
    :raises HTTPException: 404; 409 when saved since (or ``NO_DRAFT``: it's
        published, with no draft); 422 for a document that isn't a chat agent.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = chat_agent_store(request)
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
    except ChatAgentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except ChatAgentState as error:
        raise _state(error) from None
    except ChatAgentError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    return _read(await _found(record))


@router.post(ONE + "/publish")
async def publish_chat_agent(
    organization_id: NodeId,
    agent_id: ChatAgentId,
    body: Publish,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ChatAgentRead:
    """
    Publish the draft as its version, once it builds. It can't change afterwards.
    \f
    :raises HTTPException: 404; 409 when saved since, or there's no draft
        (``NO_DRAFT``); 422 with why when it doesn't build.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = chat_agent_store(request)
    try:
        current = await _found(await store.get(organization_id, agent_id))
    except PyMongoError as error:
        raise _unavailable(error) from None
    if current.get("has_draft"):
        await check_builds(request, current["document"])
    name = await name_of(session, user)
    try:
        record = await store.publish(
            organization_id, agent_id, body.revision, by=user, by_name=name
        )
    except ChatAgentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except ChatAgentState as error:
        raise _state(error) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    record = await _found(record)
    logger.info(
        "%s published chat agent %s version %s",
        user,
        agent_id,
        record["published_version"],
    )
    return _read(record)


async def check_builds(request: Request, document: dict[str, Any]) -> None:
    """:raises HTTPException: 422 with why the document can't run."""
    executor: AgentExecutor | None = getattr(request.app.state, "agent_executor", None)
    try:
        parsed = parse_document(document)
    except DocumentError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"msg": "It can't be published yet", "problems": error.problems},
        ) from None
    if executor is None:
        return
    try:
        await build_app(
            parsed,
            models=executor.models,
            services=executor.services,
            http=executor.http,
        )
    except BuildError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"msg": "It can't be published yet", "problems": [str(error)]},
        ) from None


@router.post(ONE + "/versions")
async def start_version(
    organization_id: NodeId,
    agent_id: ChatAgentId,
    body: NewVersion,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ChatAgentRead:
    """
    Start the next version: a draft copied from a published version (the latest by default).
    \f
    :raises HTTPException: 404 (or no such version); 409 ``DRAFT_EXISTS`` or ``NOT_PUBLISHED``.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    name = await name_of(session, user)
    try:
        record = await chat_agent_store(request).start_version(
            organization_id,
            agent_id,
            from_version=body.from_version,
            by=user,
            by_name=name,
        )
    except ChatAgentState as error:
        raise _state(error) from None
    except ChatAgentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    return _read(await _found(record))


@router.delete(ONE + "/draft")
async def discard_draft(
    organization_id: NodeId,
    agent_id: ChatAgentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ChatAgentRead:
    """
    Discard the draft: the agent is its latest published version again.
    \f
    :raises HTTPException: 404; 409 ``NO_DRAFT``, or ``NOT_PUBLISHED`` (delete the agent instead).
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    name = await name_of(session, user)
    try:
        record = await chat_agent_store(request).discard_draft(
            organization_id, agent_id, by=user, by_name=name
        )
    except ChatAgentState as error:
        raise _state(error) from None
    except ChatAgentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    return _read(await _found(record))


@router.get(ONE + "/versions/{version}")
async def get_version(
    organization_id: NodeId,
    agent_id: ChatAgentId,
    version: VersionNumber,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> VersionDetail:
    """One published version, its document included."""
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    try:
        found = await chat_agent_store(request).version(
            organization_id, agent_id, version
        )
    except PyMongoError as error:
        raise _unavailable(error) from None
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The agent has no such version")
    return VersionDetail.model_validate(found)


@router.get(ONE + "/versions/{version}/export")
async def export_version(
    organization_id: NodeId,
    agent_id: ChatAgentId,
    version: VersionNumber,
    request: Request,
    response: Response,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> dict[str, Any]:
    """
    A published version as a file to run anywhere (``forge-agent serve``):
    its document with the saved agents it uses bundled in ``dependencies``
    (their latest published versions). ``X-Forge-Export-Notes`` says what it
    uses that only Forge's runtime has (workflows, knowledge bases, the
    organization's MCP servers).
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    store = chat_agent_store(request)
    try:
        found = await store.version(organization_id, agent_id, version)
        if found is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "The agent has no such version"
            )
        document, notes = await bundle(store, organization_id, found["document"])
    except PyMongoError as error:
        raise _unavailable(error) from None
    if notes:
        response.headers["X-Forge-Export-Notes"] = "; ".join(notes)
    return document


class StandaloneRequest(BaseModel):
    """What a standalone project is made from."""

    model_config = ConfigDict(extra="forbid")

    #: A published version, or the draft.
    version: int | Literal["draft"]
    #: The project's name; the agent's by default.
    name: str | None = Field(default=None, max_length=100)
    #: What it's made of: where it keeps things, how it replies, its UI, access and owners.
    options: StarterOptions = Field(default_factory=StarterOptions)


class StandalonePreview(BaseModel):
    """A standalone project before it's downloaded: what's in it, and what to know."""

    name: str
    version: int | str
    #: Its files' paths, from its folder.
    files: list[str]
    #: What only Forge runs, what can't be bundled, and that it's a draft.
    notes: list[str]
    #: Where it gets the runtime: wheels (in vendor/) or pypi:<version>.
    runtime: str
    #: What its .env needs filled in: the models' keys, the tools' secrets, API keys.
    env: list[str]


async def _standalone(
    request: Request,
    organization_id: str,
    agent_id: str,
    version: int | str,
    name: str | None,
    options: StarterOptions,
) -> Any:
    """The project, from the version (or the draft) with its saved agents bundled in."""
    from forge_agent_runtime.starter import generate_project

    store = chat_agent_store(request)
    settings = request.app.state.settings
    try:
        document = await standalone_document(store, organization_id, agent_id, version)
        if document is None:
            missing = "draft" if version == "draft" else f"version {version}"
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"The agent has no {missing}"
            )
        bundled, notes = await bundle(store, organization_id, document, hosted=False)
    except PyMongoError as error:
        raise _unavailable(error) from None
    try:
        project = generate_project(
            bundled,
            name=name,
            model_provider_yaml=model_provider_yaml(settings),
            runtime=starter_runtime(settings),
            options=options,
        )
    except DocumentError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"msg": "It can't be made into a project yet", "problems": error.problems},
        ) from None
    except FileNotFoundError as error:
        logger.warning("Standalone projects can't be made: %s", error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Standalone projects aren't set up: the runtime's wheels aren't built "
            "(make starter-wheels, or FORGE_ADMIN_STARTER_WHEELS)",
        ) from None
    # The starter notes the draft and what only Forge's runtime has; the
    # bundle, the saved agents it couldn't bring along.
    project.notes = [*project.notes, *notes]
    return project


@router.post(ONE + "/standalone/preview")
async def preview_standalone(
    organization_id: NodeId,
    agent_id: ChatAgentId,
    body: StandaloneRequest,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> StandalonePreview:
    """
    What a standalone project of the agent would hold, made as the body says:
    its files, notes and the settings its .env needs, for the builder's
    Generate standalone agent dialog.
    \f
    :raises HTTPException: 404 for a version (or draft) it doesn't have; 422
        when it can't be built into a project, or the options can't go
        together; 503 without the runtime's wheels.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    project = await _standalone(
        request, organization_id, agent_id, body.version, body.name, body.options
    )
    return StandalonePreview(
        name=project.name,
        version=body.version,
        files=sorted(project.files),
        notes=project.notes,
        runtime=request.app.state.settings.starter_runtime,
        env=project.env,
    )


@router.post(ONE + "/standalone")
async def download_standalone(
    organization_id: NodeId,
    agent_id: ChatAgentId,
    body: StandaloneRequest,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> Response:
    """
    The agent as a project of its own, a zip: a Python server
    (forge-agent-runtime's AgentServer) and a React UI with the assistant,
    ready to run once its .env is filled in. Nothing in it calls Forge.
    \f
    :raises HTTPException: as the preview does.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    project = await _standalone(
        request, organization_id, agent_id, body.version, body.name, body.options
    )
    logger.info(
        "%s generated a standalone project of chat agent %s (%s)",
        user,
        agent_id,
        body.version,
    )
    return Response(
        project.zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project.name}.zip"'},
    )


@router.post(ONE + "/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_chat_agent(
    organization_id: NodeId,
    agent_id: ChatAgentId,
    body: Duplicate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ChatAgentRead:
    """A new agent (an ID of its own) whose draft is a copy of this one's current document."""
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = chat_agent_store(request)
    try:
        source = await _found(await store.get(organization_id, agent_id))
    except PyMongoError as error:
        raise _unavailable(error) from None
    document = dict(source["document"])
    document["name"] = body.name or f"{document.get('name') or 'Agent'} (copy)"
    return await create_chat_agent(
        organization_id,
        ChatAgentCreate(document=document),
        request,
        user,
        session,
        enforcer,
    )


@router.delete(ONE, status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_agent(
    organization_id: NodeId,
    agent_id: ChatAgentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """Delete the agent: its versions stop running, and its ID is never used again."""
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    try:
        deleted = await chat_agent_store(request).delete(
            organization_id, agent_id, by=user
        )
    except PyMongoError as error:
        raise _unavailable(error) from None
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    logger.info("%s deleted chat agent %s", user, agent_id)
