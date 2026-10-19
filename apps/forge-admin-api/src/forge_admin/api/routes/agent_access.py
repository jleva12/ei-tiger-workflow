"""An organization's own agent permissions: who may use its agents.

Apart from the site's roles and permissions (``/roles``, ``/permissions``),
which decide what people see and do in Forge, an organization sets these up
itself: agent permissions (``resource:action``, named as it likes), each
linked to the company's groups (whoever's sign-in names one holds it) and
to the organization's own API keys (for outside apps). There are no agent
roles: the directory's groups say who's who. Its chat agents and workflows
name the permissions that let someone use them
(forge_admin.chat_agents.access). They count only in the organization.

Reading them takes ``members:read`` in the organization; changing them,
``agent_access:manage``. Anyone may read their own access (``/me``).
"""

from collections.abc import Sequence

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from forge_admin.agent_access.links import (
    delete_group_rules,
    drop_unlinked_groups,
    links_by_permission,
    set_permission_groups,
    set_permission_keys,
)
from forge_admin.agent_access.policy import (
    agent_access,
    delete_permission_rules,
    granted_permissions,
    move_permission_rules,
)
from forge_admin.api.routes.common import (
    Audited,
    NodeId,
    Session,
    as_read,
    commit_or_conflict,
)
from forge_admin.api.routes.permissions import (
    MAX_GROUPS,
    GroupName,
    PermissionKey,
    checked_groups,
)
from forge_admin.auth.access import (
    CurrentUser,
    Enforcer,
    Level,
    Scope,
    authorize,
    domain_of,
    groups_of_caller,
)
from forge_admin.auth.authorization import group_subject
from forge_admin.models import AgentGroupLink, AgentPermission, ApiKey

router = APIRouter(
    prefix="/organizations/{organization_id}/agent-access", tags=["agent access"]
)

READ = "members:read"
MANAGE = "agent_access:manage"

PERMISSION_TAKEN = "An agent permission with this key exists"
# The most API keys one permission is linked to.
MAX_KEYS = 100


class LinkedKey(BaseModel):
    """One of the organization's API keys linked to a permission."""

    id: str
    name: str


class AgentPermissionCreate(BaseModel):
    key: PermissionKey
    description: str = ""
    #: The company groups whose members get it in the organization.
    groups: list[GroupName] = Field(default=[], max_length=MAX_GROUPS)
    #: The organization's API keys that get it, by ID.
    api_keys: list[str] = Field(default=[], max_length=MAX_KEYS)


class AgentPermissionUpdate(BaseModel):
    key: PermissionKey | None = None
    description: str | None = None
    #: Replaces the company groups linked to it.
    groups: list[GroupName] | None = Field(default=None, max_length=MAX_GROUPS)
    #: Replaces the API keys linked to it.
    api_keys: list[str] | None = Field(default=None, max_length=MAX_KEYS)


class AgentPermissionRead(Audited):
    id: int
    key: str
    resource: str
    action: str
    description: str
    #: The company groups linked to it, by name.
    groups: list[str] = []
    #: The organization's API keys linked to it.
    api_keys: list[LinkedKey] = []


class AgentGroupRead(Audited):
    id: int
    group_name: str
    description: str
    #: The agent permissions linked to it.
    permissions: list[AgentPermissionRead]


class AgentAccessRead(BaseModel):
    subject: str
    organization_id: str
    #: The lines granting their permissions: [subject, resource, action],
    #: their company groups' (group:<name>), or an API key's own.
    policies: list[list[str]]
    #: What they may do there, resource:action.
    permissions: list[str]


async def _authorize(
    session: Session,
    enforcer: Enforcer,
    user: str,
    permission: str,
    organization_id: str,
) -> None:
    await authorize(
        session, enforcer, user, permission, Scope(Level.ORG, organization_id)
    )


async def _permission(
    session: Session, organization_id: str, permission_id: int
) -> AgentPermission:
    permission = await session.get(AgentPermission, permission_id)
    if permission is None or permission.organization_id != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent permission not found")
    return permission


async def _require_free(
    session: Session, organization_id: str, resource: str, action: str
) -> None:
    """
    :raises HTTPException: 409 when one of the organization's permissions has
        the key: checked before anything's written, as linking writes before
        the commit.
    """
    taken = await session.scalar(
        select(AgentPermission).where(
            AgentPermission.organization_id == organization_id,
            AgentPermission.resource == resource,
            AgentPermission.action == action,
        )
    )
    if taken is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, PERMISSION_TAKEN)


async def _checked_keys(
    session: Session, organization_id: str, ids: list[str]
) -> list[str]:
    """
    :return: The IDs, each once.
    :raises HTTPException: 422 for one that isn't one of the organization's API keys.
    """
    wanted = list(dict.fromkeys(ids))
    known = set(
        await session.scalars(
            select(ApiKey.id).where(
                ApiKey.organization_id == organization_id, ApiKey.id.in_(wanted)
            )
        )
    )
    unknown = [i for i in wanted if i not in known]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Not the organization's API keys: {unknown}",
        )
    return wanted


async def _read_permissions(
    session: Session, organization_id: str, permissions: Sequence[AgentPermission]
) -> list[AgentPermissionRead]:
    linked = await links_by_permission(session, organization_id)
    key_ids = {i for links in linked.values() for i in links.api_keys}
    names = {
        key_id: name
        for key_id, name in await session.execute(
            select(ApiKey.id, ApiKey.name).where(ApiKey.id.in_(key_ids))
        )
    }
    read = []
    for permission in permissions:
        links = linked.get((permission.resource, permission.action))
        read.append(
            AgentPermissionRead.model_validate(permission).model_copy(
                update={
                    "groups": links.groups if links else [],
                    "api_keys": [
                        LinkedKey(id=key_id, name=names.get(key_id) or key_id)
                        for key_id in (links.api_keys if links else [])
                    ],
                }
            )
        )
    return read


@router.get("/permissions")
async def list_agent_permissions(
    organization_id: NodeId, user: CurrentUser, session: Session, enforcer: Enforcer
) -> list[AgentPermissionRead]:
    """
    List the organization's agent permissions, with the company groups and
    API keys linked to each.
    \f
    :param organization_id: The organization.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: Permissions ordered by resource and action.
    """
    await _authorize(session, enforcer, user, READ, organization_id)
    found = await session.scalars(
        select(AgentPermission)
        .where(AgentPermission.organization_id == organization_id)
        .order_by(AgentPermission.resource, AgentPermission.action)
    )
    return await _read_permissions(session, organization_id, list(found))


@router.post("/permissions", status_code=status.HTTP_201_CREATED)
async def create_agent_permission(
    organization_id: NodeId,
    body: AgentPermissionCreate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AgentPermissionRead:
    """
    Create an agent permission, linked to these company groups and API keys,
    which the organization's chat agents and workflows can then ask for.
    \f
    :param organization_id: The organization.
    :param body: Its key, description, company groups and API keys.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The new permission.
    """
    await _authorize(session, enforcer, user, MANAGE, organization_id)
    groups = checked_groups(body.groups)
    keys = await _checked_keys(session, organization_id, body.api_keys)
    resource, action = body.key.split(":")
    await _require_free(session, organization_id, resource, action)
    permission = AgentPermission(
        organization_id=organization_id,
        resource=resource,
        action=action,
        description=body.description,
    )
    session.add(permission)
    if groups:
        await set_permission_groups(session, permission, groups)
    if keys:
        await set_permission_keys(session, permission, keys)
    await commit_or_conflict(session, PERMISSION_TAKEN)
    [created] = await _read_permissions(session, organization_id, [permission])
    return created


@router.patch("/permissions/{permission_id}")
async def update_agent_permission(
    organization_id: NodeId,
    permission_id: int,
    body: AgentPermissionUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AgentPermissionRead:
    """
    Edit an agent permission. Changing its key keeps what's linked to it;
    ``groups`` and ``api_keys`` replace the groups and keys linked to it.
    Chat agents and workflows name it by ID, so they keep asking for it.
    \f
    :param organization_id: The organization.
    :param permission_id: The permission.
    :param body: The fields to change.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The updated permission.
    """
    await _authorize(session, enforcer, user, MANAGE, organization_id)
    permission = await _permission(session, organization_id, permission_id)
    old = (permission.resource, permission.action)
    new = old
    if body.key is not None:
        resource, action = body.key.split(":")
        new = (resource, action)
    if new != old:
        await _require_free(session, organization_id, *new)
        await move_permission_rules(session, organization_id, old, new)
        permission.resource, permission.action = new
    if body.description is not None:
        permission.description = body.description
    if body.groups is not None:
        await set_permission_groups(session, permission, checked_groups(body.groups))
    if body.api_keys is not None:
        keys = await _checked_keys(session, organization_id, body.api_keys)
        await set_permission_keys(session, permission, keys)
    await commit_or_conflict(session, PERMISSION_TAKEN)
    [updated] = await _read_permissions(session, organization_id, [permission])
    return updated


@router.delete("/permissions/{permission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_permission(
    organization_id: NodeId,
    permission_id: int,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Delete an agent permission, unlinking it from every company group and
    API key. Chat agents and workflows that asked for it are then for those
    who build the organization's agents only.
    \f
    :param organization_id: The organization.
    :param permission_id: The permission.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    """
    await _authorize(session, enforcer, user, MANAGE, organization_id)
    permission = await _permission(session, organization_id, permission_id)
    await delete_permission_rules(
        session, organization_id, permission.resource, permission.action
    )
    await session.delete(permission)
    await drop_unlinked_groups(session, organization_id)
    await session.commit()


# Company groups ---------------------------------------------------------------


@router.get("/groups")
async def list_agent_groups(
    organization_id: NodeId, user: CurrentUser, session: Session, enforcer: Enforcer
) -> list[AgentGroupRead]:
    """
    List the company groups linked to the organization's agent permissions,
    with the permissions each is linked to.
    \f
    :param organization_id: The organization.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The groups, by name.
    """
    await _authorize(session, enforcer, user, READ, organization_id)
    links = list(
        await session.scalars(
            select(AgentGroupLink)
            .where(AgentGroupLink.organization_id == organization_id)
            .order_by(AgentGroupLink.group_name)
        )
    )
    grants = await granted_permissions(
        session, organization_id, [group_subject(link.group_name) for link in links]
    )
    return [
        as_read(
            AgentGroupRead,
            link,
            permissions=[
                AgentPermissionRead.model_validate(p)
                for p in grants[group_subject(link.group_name)]
            ],
        )
        for link in links
    ]


@router.delete("/groups/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_agent_group(
    organization_id: NodeId,
    link_id: int,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Unlink a company group from every agent permission of the organization.
    \f
    :param organization_id: The organization.
    :param link_id: The group's link.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    """
    await _authorize(session, enforcer, user, MANAGE, organization_id)
    link = await session.get(AgentGroupLink, link_id)
    if link is None or link.organization_id != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such linked group")
    await delete_group_rules(session, organization_id, link.group_name)
    await session.delete(link)
    await session.commit()


# Your own access ----------------------------------------------------------------


@router.get("/me")
async def get_my_agent_access(
    organization_id: NodeId, user: CurrentUser, session: Session
) -> AgentAccessRead:
    """
    Return the caller's agent permissions in the organization: those linked
    to the company groups their sign-in names (or, for an API key, to it).
    \f
    :param organization_id: The organization.
    :param user: The caller.
    :param session: The request's database session.
    :return: Their agent access there.
    """
    await domain_of(session, Scope(Level.ORG, organization_id))
    access = await agent_access(session, organization_id, user, groups_of_caller(user))
    return AgentAccessRead(
        subject=user,
        organization_id=organization_id,
        policies=access.policies,
        permissions=access.permissions,
    )
