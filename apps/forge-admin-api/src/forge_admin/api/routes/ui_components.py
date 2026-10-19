"""
An organization's team UI components (``forge.ui_component/v1``): its own
compositions of the base components chats draw, which its agents' Rich
answers nodes pick by key (``forge_admin.ui_components``).

Reading the organization's catalog (the base components and its published,
enabled team ones) needs ``organizations:read``; everything else needs
``ui_components:manage`` (its administrators have it by default).

- A new component is a draft of version 1. Saving the draft names the
  revision it was made from, and answers 409 when someone saved it since. Its
  key (``org.…``) is unique in the organization, archived components'
  included, and never changes.
- Publishing checks the draft whole (``check_definition``: every example
  drawn) and freezes it as its version; agents that pick it use it from
  their next turn. ``POST …/versions`` starts the next version's draft,
  copied from any published one (restoring an old one); ``DELETE …/draft``
  discards it.
- ``PATCH`` disables or enables it: out of the catalog and every build, or
  back. ``DELETE`` archives it. Publishing, disabling and a component's own
  page say which agents pick it.
- ``POST …/preview`` draws a definition with sample inputs, for the
  editor: the base tree and what's wrong, saved and run nowhere.
"""

import asyncio
import hashlib
import json
import logging
from functools import cache
from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, HTTPException, Path, Request, Response, status
from forge_agent_runtime.generative_ui import (
    LIMITS,
    ComponentError,
    Diagnostic,
    check_definition,
    compile_component,
)
from forge_agent_runtime.present import known_components, present_catalog, problems_of
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError

from forge_admin.api.routes.common import NodeId, Session, name_of
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.db.audit import UtcDateTime
from forge_admin.ui_components.store import (
    ID_PATTERN,
    UiComponentConflict,
    UiComponentError,
    UiComponentKeyTaken,
    UiComponentState,
    UiComponentStore,
    UiComponentTaken,
    status_of,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui components"])

READ = "organizations:read"
MANAGE = "ui_components:manage"
NOT_FOUND = "The organization has no such component"
CONFLICT = (
    "Someone else saved this component since your last save; load their "
    "version or save yours over it"
)
UNAVAILABLE = "The components' database isn't answering; try again shortly"
ID_ATTEMPTS = 3
BASE = "/organizations/{organization_id}/ui-components"
ONE = BASE + "/{component_id}"

ComponentId = Annotated[str, Path(pattern=ID_PATTERN)]
VersionNumber = Annotated[int, Path(ge=1)]


class DiagnosticRead(BaseModel):
    """What's wrong with a definition or its inputs, and where."""

    #: Stable, for the editor: ``binding_missing``, ``component_input_invalid``, …
    code: str
    #: ``template.children[2].value``, ``examples[0].status``; empty for the whole.
    path: str
    message: str


class CapabilitiesRead(BaseModel):
    """What a component's template has, which decides what its agents are told."""

    has_choices: bool
    has_actions: bool
    has_forms: bool


class UiComponentRead(BaseModel):
    """A team component: its definition and where it is between draft and published."""

    id: str
    organization_id: str
    #: ``org.…``: what agents pick it by; it never changes.
    key: str
    #: Goes up with every change; a draft save names the one it was made from.
    revision: int
    #: Its draft, or its latest published version when it has no draft.
    document: dict[str, Any]
    #: draft (never published), published, or published+draft.
    status: str
    has_draft: bool
    draft_version: int | None
    #: The latest published version: what agents use; null before the first.
    published_version: int | None
    published_at: UtcDateTime | None
    #: Out of the catalog and every build until enabled again.
    disabled: bool
    created_at: UtcDateTime
    created_by: str
    updated_at: UtcDateTime
    updated_by: str
    updated_by_name: str | None


class AgentUse(BaseModel):
    """An agent that picks a component in its Rich answers."""

    id: str
    name: str | None
    #: Its draft picks it.
    in_draft: bool
    #: Its latest published version, which runs, picks it.
    in_published: bool


class UiComponentListed(UiComponentRead):
    #: How many of the organization's agents pick it, in a draft or what runs.
    used_by: int


class VersionRead(BaseModel):
    """One published version."""

    version: int
    published_at: UtcDateTime
    published_by: str
    published_by_name: str | None


class VersionDetail(VersionRead):
    #: The version's definition, ``version`` set.
    document: dict[str, Any]


class UiComponentDetail(UiComponentRead):
    #: Its published versions, newest first.
    versions: list[VersionRead]
    #: The agents that pick it.
    used_by: list[AgentUse]


class UiComponentChanged(UiComponentRead):
    """A component just published, disabled or enabled, and whom that reaches."""

    #: The agents that pick it, whose next turn the change reaches; null
    #: when they couldn't be read.
    used_by: list[AgentUse] | None


class UiComponentCreate(BaseModel):
    """A new component. The API makes its ID; its key is the definition's."""

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

    #: The published version to start from (restore); the latest when omitted.
    from_version: int | None = Field(default=None, ge=1)


class UiComponentChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Out of the catalog and every build, or back.
    disabled: bool


class PreviewRequest(BaseModel):
    """A definition, saved or not, and inputs to draw it with."""

    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]
    #: Sample inputs; the definition's first example when omitted.
    inputs: dict[str, Any] | None = None


class PreviewRead(BaseModel):
    """What the definition draws the inputs as, and everything publishing would refuse."""

    #: The base tree, as chats draw it; null when it can't be drawn.
    tree: Any = None
    diagnostics: list[DiagnosticRead]
    #: Null when the definition can't be compiled.
    capabilities: CapabilitiesRead | None = None
    base_components: list[str] = Field(default_factory=list)


class BaseComponentRead(BaseModel):
    name: str
    description: str


class BaseGroupRead(BaseModel):
    id: str
    label: str
    components: list[BaseComponentRead]


class TeamComponentRead(BaseModel):
    """A published, enabled team component, as agents' pickers offer it."""

    id: str
    key: str
    display_name: str
    description: str
    #: Its inputs: what the model gives it, in ``props``.
    input_schema: dict[str, Any]
    capabilities: CapabilitiesRead
    #: Its latest published version: what agents use.
    version: int
    base_components: list[str]


class CatalogRead(BaseModel):
    """
    What the organization's agents may pick: the base components, the same
    for every organization, and its own published, enabled ones.
    """

    #: Changes whenever any of it does: the ETag.
    digest: str
    #: The base components' vocabulary, as the runtime has it.
    base_digest: str
    base: list[BaseGroupRead]
    team: list[TeamComponentRead]


def ui_component_store(request: Request) -> UiComponentStore:
    """:raises HTTPException: 503 when MongoDB isn't set up."""
    store: UiComponentStore | None = getattr(request.app.state, "ui_components", None)
    if store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Team UI components aren't set up: set FORGE_ADMIN_MONGO_URI",
        )
    return store


def _read(record: dict[str, Any]) -> dict[str, Any]:
    return {
        **record,
        "id": record["_id"],
        "key": record["document"]["key"],
        "status": status_of(record),
        "has_draft": bool(record.get("has_draft")),
        "disabled": bool(record.get("disabled")),
    }


def _unavailable(error: PyMongoError) -> HTTPException:
    logger.warning("The UI components' MongoDB failed: %s", error)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE)


def _state(error: UiComponentState) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, {"code": error.code, "msg": str(error)}
    )


def _diagnostics(found: list[Diagnostic]) -> list[dict[str, str]]:
    return [
        {"code": each.code, "path": each.path, "message": each.message}
        for each in found
    ]


def _invalid(message: str, found: list[Diagnostic]) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        {"msg": message, "diagnostics": _diagnostics(found)},
    )


def _found(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return record


async def _uses(
    request: Request, organization_id: str, keys: list[str]
) -> dict[str, list[AgentUse]]:
    """
    :return: By key, the agents that pick it.
    :raises PyMongoError: The agents' store isn't answering.
    """
    agents: ChatAgentStore | None = getattr(request.app.state, "chat_agents", None)
    uses: dict[str, list[AgentUse]] = {key: [] for key in keys}
    if agents is None or not keys:
        return uses
    for agent in await agents.using_components(organization_id, keys):
        for key in keys:
            in_draft, in_published = key in agent["draft"], key in agent["published"]
            if in_draft or in_published:
                uses[key].append(
                    AgentUse(
                        id=agent["id"],
                        name=agent["name"],
                        in_draft=in_draft,
                        in_published=in_published,
                    )
                )
    return uses


async def _changed(
    request: Request, organization_id: str, record: dict[str, Any]
) -> UiComponentChanged:
    """
    A changed component with the agents its change reaches; failing to read
    them doesn't fail the change.
    """
    try:
        used: list[AgentUse] | None = (
            await _uses(request, organization_id, [record["document"]["key"]])
        )[record["document"]["key"]]
    except PyMongoError as error:
        logger.warning("The agents using %s couldn't be read: %s", record["_id"], error)
        used = None
    return UiComponentChanged.model_validate({**_read(record), "used_by": used})


@cache
def _base() -> tuple[str, list[dict[str, Any]]]:
    """The base catalog's digest and groups: the runtime package's, fixed while it runs."""
    catalog = present_catalog()
    digest = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()
    groups = [
        {
            "id": group["id"],
            "label": group["label"],
            "components": [
                {
                    "name": name,
                    "description": catalog["components"][name]["description"],
                }
                for name in group["components"]
            ],
        }
        for group in catalog["groups"]
    ]
    return digest, groups


# -- The catalog: before /{component_id}, which "catalog" isn't --


@router.get(BASE + "/catalog", response_model=CatalogRead)
async def get_catalog(
    organization_id: NodeId,
    request: Request,
    response: Response,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Any:
    """
    The components the organization's agents may pick: the base ones and its
    published, enabled team ones (their latest versions), as pickers and
    the instruction writer offer them. Its ETag is its digest.
    \f
    :raises HTTPException: 403 without organizations:read; 503 when MongoDB
        isn't set up or answering.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    try:
        live = await ui_component_store(request).live(organization_id)
    except PyMongoError as error:
        raise _unavailable(error) from None
    base_digest, groups = _base()
    team: list[TeamComponentRead] = []
    for record, document in live:
        try:
            compiled = compile_component(document)
        except ComponentError as error:
            # Published once checked; limits may have tightened since.
            logger.warning(
                "Published component %s can't be compiled: %s", record["_id"], error
            )
            continue
        team.append(
            TeamComponentRead(
                id=record["_id"],
                key=compiled.key,
                display_name=compiled.definition.display_name,
                description=compiled.description,
                input_schema=compiled.input_schema,
                capabilities=CapabilitiesRead.model_validate(
                    compiled.capabilities, from_attributes=True
                ),
                version=int(record["published_version"]),
                base_components=sorted(compiled.base_components),
            )
        )
    # Whose, and which versions: a key is another component in another
    # organization, and a published version never changes.
    identity = [(each.id, each.key, each.version) for each in team]
    digest = hashlib.sha256(
        json.dumps([base_digest, organization_id, identity]).encode()
    ).hexdigest()
    etag = f'"{digest}"'
    if if_none_match == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag}
        )
    response.headers["ETag"] = etag
    return CatalogRead(
        digest=digest,
        base_digest=base_digest,
        base=[BaseGroupRead.model_validate(g) for g in groups],
        team=team,
    )


def _preview(definition: dict[str, Any], inputs: dict[str, Any] | None) -> PreviewRead:
    diagnostics = check_definition(definition)
    try:
        compiled = compile_component(definition)
    except ComponentError:
        return PreviewRead.model_validate({"diagnostics": _diagnostics(diagnostics)})
    examples = compiled.definition.examples
    sample, where = (
        (inputs, "inputs")
        if inputs is not None
        else (examples[0] if examples else None, "examples[0]")
    )
    tree = None
    if sample is not None:
        try:
            tree = compiled.expand(sample, where=where)
        except ComponentError as error:
            # An example's are among publishing's already.
            if inputs is not None:
                diagnostics.extend(error.diagnostics)
        else:
            if inputs is not None:
                diagnostics.extend(
                    Diagnostic("tree_invalid", where, f"drawn, {problem}")
                    for problem in problems_of(tree, known_components())
                )
    return PreviewRead.model_validate(
        {
            "tree": tree,
            "diagnostics": _diagnostics(diagnostics),
            "capabilities": CapabilitiesRead.model_validate(
                compiled.capabilities, from_attributes=True
            ),
            "base_components": sorted(compiled.base_components),
        }
    )


@router.post(BASE + "/preview")
async def preview_component(
    organization_id: NodeId,
    body: PreviewRequest,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> PreviewRead:
    """
    Draw a definition with sample inputs (its first example by default) as
    chats would, and say everything publishing it would refuse. Nothing is
    saved or run; its controls' replies are the editor's to simulate.
    \f
    :raises HTTPException: 403 without ui_components:manage.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    # Patterns and drawing are bounded in time, but CPU all the same: off the event loop.
    return await asyncio.to_thread(_preview, body.definition, body.inputs)


@router.get(BASE)
async def list_components(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[UiComponentListed]:
    """The organization's components, drafts and disabled ones too, most recently changed first."""
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    try:
        records = await ui_component_store(request).list(organization_id)
        uses = await _uses(
            request, organization_id, [record["document"]["key"] for record in records]
        )
    except PyMongoError as error:
        raise _unavailable(error) from None
    return [
        UiComponentListed.model_validate(
            {**_read(record), "used_by": len(uses[record["document"]["key"]])}
        )
        for record in records
    ]


@router.post(BASE, status_code=status.HTTP_201_CREATED)
async def create_component(
    organization_id: NodeId,
    body: UiComponentCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> UiComponentRead:
    """
    Make a team component from a definition: a draft of version 1, under its key.
    \f
    :raises HTTPException: 403 without ui_components:manage; 409 when the
        organization has (or had) a component with its key; 422 for one that
        isn't a definition; 503 when MongoDB isn't set up or answering.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = ui_component_store(request)
    name = await name_of(session, user)
    for attempt in range(1, ID_ATTEMPTS + 1):
        try:
            record = await store.create_agent(
                organization_id,
                body.document,
                by=user,
                by_name=name,
                max_bytes=LIMITS.definition_bytes,
            )
            break
        except UiComponentTaken:
            if attempt == ID_ATTEMPTS:
                raise
        except UiComponentKeyTaken as error:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "KEY_TAKEN",
                    "msg": f"The organization has a component called {error} already "
                    "(archived ones keep their keys): pick another key",
                },
            ) from None
        except UiComponentError as error:
            raise _invalid(str(error), error.diagnostics) from None
        except PyMongoError as error:
            raise _unavailable(error) from None
    logger.info(
        "%s created UI component %s (%s) in organization %s",
        user,
        record["_id"],
        record["document"]["key"],
        organization_id,
    )
    return UiComponentRead.model_validate(_read(record))


@router.get(ONE)
async def get_component(
    organization_id: NodeId,
    component_id: ComponentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> UiComponentDetail:
    """A component with its published versions and the agents that pick it."""
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = ui_component_store(request)
    try:
        record = _found(await store.get(organization_id, component_id))
        versions = await store.versions(organization_id, component_id)
        key = record["document"]["key"]
        uses = await _uses(request, organization_id, [key])
    except PyMongoError as error:
        raise _unavailable(error) from None
    return UiComponentDetail.model_validate(
        {**_read(record), "versions": versions, "used_by": uses[key]}
    )


@router.put(ONE + "/draft")
async def save_draft(
    organization_id: NodeId,
    component_id: ComponentId,
    body: DraftSave,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> UiComponentRead:
    """
    Save the component's draft, made from ``revision``. Only its format is
    checked: a draft that can't be drawn yet is kept.
    \f
    :raises HTTPException: 404; 409 when saved since (or ``NO_DRAFT``: it's
        published, with no draft); 422 for one that isn't a definition, or
        changes its key.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = ui_component_store(request)
    name = await name_of(session, user)
    try:
        record = await store.save_draft(
            organization_id,
            component_id,
            body.revision,
            body.document,
            by=user,
            by_name=name,
            max_bytes=LIMITS.definition_bytes,
        )
    except UiComponentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except UiComponentState as error:
        raise _state(error) from None
    except UiComponentError as error:
        raise _invalid(str(error), error.diagnostics) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    return UiComponentRead.model_validate(_read(_found(record)))


@router.post(ONE + "/publish")
async def publish_component(
    organization_id: NodeId,
    component_id: ComponentId,
    body: Publish,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> UiComponentChanged:
    """
    Publish the draft as its version, once it checks whole: agents that pick
    the component use it from their next turn. It can't change afterwards.
    \f
    :raises HTTPException: 404; 409 when saved since, or there's no draft
        (``NO_DRAFT``); 422 with its diagnostics when it doesn't check.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = ui_component_store(request)
    try:
        current = _found(await store.get(organization_id, component_id))
    except PyMongoError as error:
        raise _unavailable(error) from None
    if current.get("has_draft") and current["revision"] == body.revision:
        found = await asyncio.to_thread(check_definition, current["document"])
        if found:
            raise _invalid("It can't be published yet", found)
    name = await name_of(session, user)
    try:
        record = await store.publish(
            organization_id, component_id, body.revision, by=user, by_name=name
        )
    except UiComponentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except UiComponentState as error:
        raise _state(error) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    record = _found(record)
    logger.info(
        "%s published UI component %s (%s) version %s",
        user,
        component_id,
        record["document"]["key"],
        record["published_version"],
    )
    return await _changed(request, organization_id, record)


@router.patch(ONE)
async def change_component(
    organization_id: NodeId,
    component_id: ComponentId,
    body: UiComponentChange,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> UiComponentChanged:
    """
    Disable the component (out of the catalog, and agents that pick it don't
    build until it's back or they pick otherwise) or enable it again. Its
    versions stay as they are.
    \f
    :raises HTTPException: 404.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = ui_component_store(request)
    name = await name_of(session, user)
    try:
        record = _found(
            await store.set_disabled(
                organization_id,
                component_id,
                disabled=body.disabled,
                by=user,
                by_name=name,
            )
        )
    except PyMongoError as error:
        raise _unavailable(error) from None
    logger.info(
        "%s %s UI component %s",
        user,
        "disabled" if body.disabled else "enabled",
        component_id,
    )
    return await _changed(request, organization_id, record)


@router.delete(ONE, status_code=status.HTTP_204_NO_CONTENT)
async def archive_component(
    organization_id: NodeId,
    component_id: ComponentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Archive the component: out of every read, its key kept, so no other
    component takes it. Agents that pick it don't build until they pick
    otherwise; conversations keep what it drew.
    \f
    :raises HTTPException: 404.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    try:
        archived = await ui_component_store(request).delete(
            organization_id, component_id, by=user
        )
    except PyMongoError as error:
        raise _unavailable(error) from None
    if not archived:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    logger.info("%s archived UI component %s", user, component_id)


@router.get(ONE + "/versions")
async def list_versions(
    organization_id: NodeId,
    component_id: ComponentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[VersionRead]:
    """The component's published versions, newest first."""
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = ui_component_store(request)
    try:
        _found(await store.get(organization_id, component_id))
        versions = await store.versions(organization_id, component_id)
    except PyMongoError as error:
        raise _unavailable(error) from None
    # The store's annotations say list in a class with a list method: it's a list.
    return [VersionRead.model_validate(each) for each in cast(list[Any], versions)]


@router.get(ONE + "/versions/{version}")
async def get_version(
    organization_id: NodeId,
    component_id: ComponentId,
    version: VersionNumber,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> VersionDetail:
    """One published version, as it was published."""
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = ui_component_store(request)
    try:
        _found(await store.get(organization_id, component_id))
        found = await store.version(organization_id, component_id, version)
    except PyMongoError as error:
        raise _unavailable(error) from None
    if found is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "The component has no such version"
        )
    return VersionDetail.model_validate(found)


@router.post(ONE + "/versions", status_code=status.HTTP_201_CREATED)
async def start_version(
    organization_id: NodeId,
    component_id: ComponentId,
    body: NewVersion,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> UiComponentRead:
    """
    Start the next version's draft, copied from a published version (the
    latest by default): editing a published component, or restoring an old
    one. Agents keep using the latest published version meanwhile.
    \f
    :raises HTTPException: 404; 409 ``DRAFT_EXISTS`` or ``NOT_PUBLISHED``.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = ui_component_store(request)
    name = await name_of(session, user)
    try:
        record = await store.start_version(
            organization_id,
            component_id,
            from_version=body.from_version,
            by=user,
            by_name=name,
        )
    except UiComponentState as error:
        raise _state(error) from None
    except UiComponentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    return UiComponentRead.model_validate(_read(_found(record)))


@router.delete(ONE + "/draft")
async def discard_draft(
    organization_id: NodeId,
    component_id: ComponentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> UiComponentRead:
    """
    Discard the draft: the component is back at its latest published version.
    \f
    :raises HTTPException: 404; 409 ``NO_DRAFT``, or ``NOT_PUBLISHED`` (archive it instead).
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    store = ui_component_store(request)
    name = await name_of(session, user)
    try:
        record = await store.discard_draft(
            organization_id, component_id, by=user, by_name=name
        )
    except UiComponentState as error:
        raise _state(error) from None
    except UiComponentConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT) from None
    except PyMongoError as error:
        raise _unavailable(error) from None
    return UiComponentRead.model_validate(_read(_found(record)))
