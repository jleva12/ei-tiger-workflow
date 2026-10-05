"""The company's groups, from its external directory, linked to permissions.

Groups are linked from each permission (``groups`` on ``/permissions``): any
number of groups to a permission, and a group to any number. Someone whose
bearer token names a linked group (in its ``FORGE_ADMIN_JWT_GROUPS_CLAIM``
claim) holds its permissions on the whole site, besides those of their roles.

Here are the groups linked to anything, with what each is linked to, and
unlinking a group from everything. Anyone signed in can read them;
unlinking requires ``permissions:update`` on the site.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from forge_admin.api.routes.common import Audited, Session
from forge_admin.api.routes.permissions import PermissionRead
from forge_admin.auth.access import SITE, CurrentUser, Enforcer, authorize, current_user
from forge_admin.auth.authorization import granted_permissions, group_subject
from forge_admin.auth.group_links import delete_group_rules
from forge_admin.models import PermissionGroupLink

router = APIRouter(prefix="/permission-groups", tags=["permission groups"])

NOT_FOUND = "No such linked group"


class GroupLinkRead(Audited):
    id: int
    group_name: str
    description: str
    #: The permissions linked to it.
    permissions: list[PermissionRead]


@router.get("", dependencies=[Depends(current_user)])
async def list_group_links(session: Session) -> list[GroupLinkRead]:
    """
    List the groups linked to permissions, with the permissions each has.
    \f
    :param session: The request's database session.
    :return: The groups, by name.
    """
    links = list(
        await session.scalars(
            select(PermissionGroupLink).order_by(PermissionGroupLink.group_name)
        )
    )
    grants = await granted_permissions(
        session, [group_subject(link.group_name) for link in links]
    )
    return [
        GroupLinkRead.model_validate(
            {
                **{
                    name: getattr(link, name)
                    for name in GroupLinkRead.model_fields
                    if name != "permissions"
                },
                "permissions": [
                    PermissionRead.model_validate(p)
                    for p in grants[group_subject(link.group_name)]
                ],
            }
        )
        for link in links
    ]


@router.delete("/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group_link(
    link_id: int, user: CurrentUser, session: Session, enforcer: Enforcer
) -> None:
    """
    Unlink a group from every permission: its members keep only their
    roles' permissions.
    \f
    :param link_id: The group.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :raises HTTPException: 403 without permissions:update; 404 for a group
        that isn't linked.
    """
    await authorize(session, enforcer, user, "permissions:update", SITE)
    link = await session.get(PermissionGroupLink, link_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    await delete_group_rules(session, link.group_name)
    await session.delete(link)
    await session.commit()
