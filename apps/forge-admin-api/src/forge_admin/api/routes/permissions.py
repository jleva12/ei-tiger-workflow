"""Create, list, edit and delete permissions.

Anyone signed in can read them; changing them requires the permissions:*
permissions on the site.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select

from forge_admin.api.routes.common import Audited, Session, commit_or_conflict
from forge_admin.auth.access import SITE, CurrentUser, Enforcer, authorize, current_user
from forge_admin.auth.authorization import (
    PERMISSION_KEY_PATTERN,
    delete_permission_rules,
    move_permission_rules,
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


class PermissionCreate(BaseModel):
    key: PermissionKey
    description: str = ""


class PermissionUpdate(BaseModel):
    key: PermissionKey | None = None
    description: str | None = None


class PermissionRead(Audited):
    id: int
    key: str
    resource: str
    action: str
    description: str


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
    return [PermissionRead.model_validate(p) for p in result]


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
    resource, action = body.key.split(":")
    permission = Permission(
        resource=resource, action=action, description=body.description
    )
    session.add(permission)
    await commit_or_conflict(session, KEY_TAKEN)
    return PermissionRead.model_validate(permission)


@router.get("/{permission_id}", dependencies=[Depends(current_user)])
async def get_permission(permission_id: int, session: Session) -> PermissionRead:
    """
    Get one permission.
    \f
    :param permission_id: The permission.
    :param session: The request's database session.
    :return: The permission.
    """
    return PermissionRead.model_validate(await _get(session, permission_id))


@router.patch("/{permission_id}")
async def update_permission(
    permission_id: int,
    body: PermissionUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> PermissionRead:
    """
    Edit a permission. Changing its key updates every role that holds it.
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
        await move_permission_rules(session, old, new)
        permission.resource, permission.action = new
    if body.description is not None:
        permission.description = body.description
    await commit_or_conflict(session, KEY_TAKEN)
    return PermissionRead.model_validate(permission)


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
    await session.commit()
