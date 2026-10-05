"""The company's groups linked to permissions, seen from the permissions.

A permission can be linked to any number of groups, and a group to any
number of permissions: each link is a ``p`` line for the subject
``group:<name>`` (``authorization.group_subject``), and every group linked
to anything has a row in ``authz_group_links``. A group left linked to
nothing loses its row.
"""

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.auth.authorization import GROUP_PREFIX, group_subject
from forge_admin.models import CasbinRule, Permission, PermissionGroupLink


async def _links_by_subject(session: AsyncSession) -> dict[str, PermissionGroupLink]:
    links = await session.scalars(select(PermissionGroupLink))
    return {group_subject(link.group_name): link for link in links}


async def _group_rules(session: AsyncSession) -> list[CasbinRule]:
    rules = await session.scalars(
        select(CasbinRule).where(
            CasbinRule.ptype == "p", CasbinRule.v0.startswith(GROUP_PREFIX)
        )
    )
    return list(rules)


async def groups_by_permission(
    session: AsyncSession,
) -> dict[tuple[str, str], list[str]]:
    """
    :return: Each linked permission's ``(resource, action)`` mapped to the
        names of the groups linked to it, sorted.
    """
    links = await _links_by_subject(session)
    found: dict[tuple[str, str], list[str]] = {}
    for rule in await _group_rules(session):
        link = links.get(rule.v0 or "")
        if link is not None:
            found.setdefault((rule.v1 or "", rule.v2 or ""), []).append(link.group_name)
    return {grant: sorted(names) for grant, names in found.items()}


async def set_permission_groups(
    session: AsyncSession, permission: Permission, names: Iterable[str]
) -> None:
    """
    Link a permission to exactly these groups: those it's linked to and
    shouldn't be are unlinked, and the new ones linked (and recorded).

    :param session: The request's database session.
    :param permission: The permission.
    :param names: The groups' names, as the directory spells them.
    """
    wanted = {group_subject(name): name for name in names}
    grant = (permission.resource, permission.action)
    for rule in await _group_rules(session):
        if (rule.v1, rule.v2) != grant:
            continue
        if rule.v0 in wanted:
            del wanted[rule.v0 or ""]
        else:
            await session.delete(rule)
    links = await _links_by_subject(session)
    for subject, name in sorted(wanted.items()):
        if subject not in links:
            session.add(PermissionGroupLink(group_name=name))
        session.add(
            CasbinRule(
                ptype="p", v0=subject, v1=permission.resource, v2=permission.action
            )
        )
    await session.flush()
    await drop_unlinked_groups(session)


async def drop_unlinked_groups(session: AsyncSession) -> None:
    """Forget the groups no permission is linked to any more."""
    await session.flush()
    linked = {rule.v0 for rule in await _group_rules(session)}
    for subject, link in (await _links_by_subject(session)).items():
        if subject not in linked:
            await session.delete(link)


async def delete_group_rules(session: AsyncSession, name: str) -> None:
    """Unlink a group from every permission."""
    await session.execute(
        delete(CasbinRule).where(
            CasbinRule.ptype == "p", CasbinRule.v0 == group_subject(name)
        )
    )
