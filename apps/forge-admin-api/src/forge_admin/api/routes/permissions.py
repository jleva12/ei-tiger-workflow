"""Create, list, edit and delete permissions, and link them to the company's
groups.

Anyone signed in can read them; changing them requires the permissions:*
permissions on the site. A permission's ``groups`` are the groups from the
company's external directory linked to it, any number: someone whose token
names one of them holds the permission on the whole site.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select

from forge_admin.api.routes.common import Audited, Session, commit_or_conflict
from forge_admin.auth.access import SITE, CurrentUser, Enforcer, authorize, current_user
from forge_admin.auth.authorization import (
    GROUP_NAME_PATTERN,
    MAX_SUBJECT,
    PERMISSION_KEY_PATTERN,
    delete_permission_rules,
    group_subject,
    move_permission_rules,
)
from forge_admin.auth.group_links import (
    drop_unlinked_groups,
    groups_by_permission,
    set_permission_groups,
)
from forge_admin.models import Permission

router = APIRouter(prefix="/permissions", tags=["permissions"])

KEY_TAKEN = "A permission with this key exists"
# "resource:action", e.g. "agents:run"; see PERMISSION_KEY_PATTERN.
PermissionKey = Annotated[
    str,
    StringConstraints(pattern=PERMISSION_KEY_PATTERN, max_length=200),
    Field(examples=["agents:run"]),
]


GroupName = Annotated[
    str,
    StringConstraints(pattern=GROUP_NAME_PATTERN),
    Field(
        description="A company group's name, as its directory spells it",
        examples=["Forge Admins"],
    ),
]
# The most groups one permission is linked to.
MAX_GROUPS = 100


class PermissionCreate(BaseModel):
    key: PermissionKey
    description: str = ""
    #: The company groups whose members get it.
    groups: list[GroupName] = Field(default=[], max_length=MAX_GROUPS)


class PermissionUpdate(BaseModel):
    key: PermissionKey | None = None
    description: str | None = None
    #: Replaces the company groups linked to it.
    groups: list[GroupName] | None = Field(default=None, max_length=MAX_GROUPS)


class PermissionRead(Audited):
    id: int
    key: str
    resource: str
    action: str
    description: str
    #: The company groups linked to it, by name.
    groups: list[str] = []


def _checked_groups(names: list[str]) -> list[str]:
    """
    :return: The names, each once.
    :raises HTTPException: 422 for one too long to keep.
    """
    for name in names:
        if len(group_subject(name)) > MAX_SUBJECT:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"The group {name!r} is too long once its commas and brackets "
                "are encoded",
            )
    return list(dict.fromkeys(names))


async def _require_free(session: Session, resource: str, action: str) -> None:
    """
    :raises HTTPException: 409 when a permission has the key: checked before
        anything's written, as linking groups writes before the commit.
    """
    taken = await session.scalar(
        select(Permission).where(
            Permission.resource == resource, Permission.action == action
        )
    )
    if taken is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, KEY_TAKEN)


async def _read(session: Session, permission: Permission) -> PermissionRead:
    groups = await groups_by_permission(session)
    return PermissionRead.model_validate(permission).model_copy(
        update={"groups": groups.get((permission.resource, permission.action), [])}
    )


async def _get(session: Session, permission_id: int) -> Permission:
    permission = await session.get(Permission, permission_id)
    if permission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permission not found")
    return permission


@router.get("", dependencies=[Depends(current_user)])
async def list_permissions(session: Session) -> list[PermissionRead]:
    """
    List every permission.
    \f
    :param session: The request's database session.
    :return: Permissions ordered by resource and action.
    """
    result = await session.scalars(
        select(Permission).order_by(Permission.resource, Permission.action)
    )
    groups = await groups_by_permission(session)
    return [
        PermissionRead.model_validate(p).model_copy(
            update={"groups": groups.get((p.resource, p.action), [])}
        )
        for p in result
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_permission(
    body: PermissionCreate, user: CurrentUser, session: Session, enforcer: Enforcer
) -> PermissionRead:
    """
    Create a permission that roles can then be granted.
    \f
    :param body: The permission key and description.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The new permission.
    """
    await authorize(session, enforcer, user, "permissions:create", SITE)
    groups = _checked_groups(body.groups)
    resource, action = body.key.split(":")
    await _require_free(session, resource, action)
    permission = Permission(
        resource=resource, action=action, description=body.description
    )
    session.add(permission)
    if groups:
        await set_permission_groups(session, permission, groups)
    await commit_or_conflict(session, KEY_TAKEN)
    return await _read(session, permission)


@router.get("/{permission_id}", dependencies=[Depends(current_user)])
async def get_permission(permission_id: int, session: Session) -> PermissionRead:
    """
    Get one permission.
    \f
    :param permission_id: The permission.
    :param session: The request's database session.
    :return: The permission.
    """
    return await _read(session, await _get(session, permission_id))


@router.patch("/{permission_id}")
async def update_permission(
    permission_id: int,
    body: PermissionUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> PermissionRead:
    """
    Edit a permission. Changing its key updates every role and group that
    holds it; ``groups`` replaces the company groups linked to it.
    \f
    :param permission_id: The permission.
    :param body: The fields to change.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The updated permission.
    """
    await authorize(session, enforcer, user, "permissions:update", SITE)
    permission = await _get(session, permission_id)
    old = (permission.resource, permission.action)
    new = old
    if body.key is not None:
        resource, action = body.key.split(":")
        new = (resource, action)
    if new != old:
        await _require_free(session, *new)
        await move_permission_rules(session, old, new)
        permission.resource, permission.action = new
    if body.description is not None:
        permission.description = body.description
    if body.groups is not None:
        await set_permission_groups(session, permission, _checked_groups(body.groups))
    await commit_or_conflict(session, KEY_TAKEN)
    return await _read(session, permission)


@router.delete("/{permission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_permission(
    permission_id: int, user: CurrentUser, session: Session, enforcer: Enforcer
) -> None:
    """
    Delete a permission and revoke it from every role.
    \f
    :param permission_id: The permission.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    """
    await authorize(session, enforcer, user, "permissions:delete", SITE)
    permission = await _get(session, permission_id)
    await delete_permission_rules(session, permission.resource, permission.action)
    await session.delete(permission)
    await drop_unlinked_groups(session)
    await session.commit()
