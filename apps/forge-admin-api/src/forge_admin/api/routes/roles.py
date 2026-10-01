"""Create, list, edit and delete roles, including the permissions they grant.

Anyone signed in can read them; changing them requires the roles:*
permissions on the site. A role's key starts with the level it is assigned
at, e.g. ``org:approver``.
"""

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select

from forge_admin.api.routes.common import (
    Audited,
    Name,
    Session,
    as_read,
    commit_or_conflict,
)
from forge_admin.api.routes.permissions import PermissionRead
from forge_admin.auth.access import SITE, CurrentUser, Enforcer, authorize, current_user
from forge_admin.auth.authorization import (
    ROLE_KEY_PATTERN,
    assignment_counts,
    delete_role_rules,
    granted_permissions,
    set_role_permissions,
)
from forge_admin.db.audit import utc_now
from forge_admin.models import Permission, Role

router = APIRouter(prefix="/roles", tags=["roles"])

KEY_TAKEN = "A role with this key exists"
# "<level>:<name>", e.g. "org:member"; see ROLE_KEY_PATTERN.
RoleKey = Annotated[
    str,
    StringConstraints(pattern=ROLE_KEY_PATTERN, max_length=100),
    Field(examples=["org:approver"]),
]


class RoleCreate(BaseModel):
    key: RoleKey
    name: Name
    description: str = ""
    permission_ids: list[int] = []


class RoleUpdate(BaseModel):
    name: Name | None = None
    description: str | None = None
    # Replaces the role's permissions when present.
    permission_ids: list[int] | None = None


class RoleRead(Audited):
    key: str
    # Where the role can be assigned: site or org.
    level: str
    name: str
    description: str
    permissions: list[PermissionRead]
    # How many times it is assigned, across every scope.
    member_count: int


async def _get(session: Session, key: str) -> Role:
    role = await session.get(Role, key)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Role not found")
    return role


async def _permissions(session: Session, ids: Sequence[int]) -> list[Permission]:
    found = list(
        await session.scalars(select(Permission).where(Permission.id.in_(ids)))
    )
    unknown = sorted(set(ids) - {p.id for p in found})
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown permission IDs: {unknown}"
        )
    return found


async def _read(session: Session, roles: Sequence[Role]) -> list[RoleRead]:
    keys = [role.key for role in roles]
    grants = await granted_permissions(session, keys)
    counts = await assignment_counts(session, keys)
    return [
        as_read(
            RoleRead,
            role,
            permissions=[PermissionRead.model_validate(p) for p in grants[role.key]],
            member_count=counts[role.key],
        )
        for role in roles
    ]


@router.get("", dependencies=[Depends(current_user)])
async def list_roles(session: Session) -> list[RoleRead]:
    """
    List every role with its permissions.
    \f
    :param session: The request's database session.
    :return: Roles ordered by key.
    """
    roles = list(await session.scalars(select(Role).order_by(Role.key)))
    return await _read(session, roles)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleCreate, user: CurrentUser, session: Session, enforcer: Enforcer
) -> RoleRead:
    """
    Create a role and grant it the given permissions.
    \f
    :param body: The role's key, name, description and permission IDs.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The new role.
    """
    await authorize(session, enforcer, user, "roles:create", SITE)
    if await session.get(Role, body.key) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, KEY_TAKEN)
    permissions = await _permissions(session, body.permission_ids)
    role = Role(key=body.key, name=body.name, description=body.description)
    session.add(role)
    await set_role_permissions(session, role.key, permissions)
    await commit_or_conflict(session, KEY_TAKEN)
    [created] = await _read(session, [role])
    return created


@router.get("/{key}", dependencies=[Depends(current_user)])
async def get_role(key: str, session: Session) -> RoleRead:
    """
    Get one role with its permissions.
    \f
    :param key: The role key.
    :param session: The request's database session.
    :return: The role.
    """
    [role] = await _read(session, [await _get(session, key)])
    return role


@router.patch("/{key}")
async def update_role(
    key: str, body: RoleUpdate, user: CurrentUser, session: Session, enforcer: Enforcer
) -> RoleRead:
    """
    Edit a role's name, description or permissions. The key never changes.
    \f
    :param key: The role key.
    :param body: The fields to change; ``permission_ids`` replaces the grants.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The updated role.
    """
    await authorize(session, enforcer, user, "roles:update", SITE)
    role = await _get(session, key)
    if body.name is not None:
        role.name = body.name
    if body.description is not None:
        role.description = body.description
    if body.permission_ids is not None:
        await set_role_permissions(
            session, key, await _permissions(session, body.permission_ids)
        )
        # Grants live in casbin_rule; stamp the role too, so its audit columns
        # record who last changed what it grants.
        role.updated_at = utc_now()
    await session.commit()
    [updated] = await _read(session, [role])
    return updated


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    key: str, user: CurrentUser, session: Session, enforcer: Enforcer
) -> None:
    """
    Delete a role, its grants and every assignment of it.
    \f
    :param key: The role key.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    """
    await authorize(session, enforcer, user, "roles:delete", SITE)
    role = await _get(session, key)
    await delete_role_rules(session, key)
    await session.delete(role)
    await session.commit()
