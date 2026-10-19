"""Who an organization's agent permissions are linked to: the company's
groups, and the organization's own API keys.

A permission can be linked to any number of groups and keys, and each of
those to any number of permissions: each link is a line for the subject
``group:<name>`` (``authorization.group_subject``) or ``apikey:<id>`` in the
organization's domain. Every group linked to anything there also has a row
in ``agent_group_links``, which lists them; a group left linked to nothing
loses its row. A key is the organization's own row in ``api_keys``.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.agent_access.policy import grant_lines, team_domain
from forge_admin.auth.api_keys import key_id_of, key_subject
from forge_admin.auth.authorization import (
    API_KEY_SUBJECT_PREFIX,
    GROUP_PREFIX,
    group_subject,
)
from forge_admin.models import AgentCasbinRule, AgentGroupLink, AgentPermission


@dataclass
class Linked:
    """What an agent permission is linked to."""

    #: The company groups' names, sorted.
    groups: list[str] = field(default_factory=list)
    #: The API keys' IDs, sorted.
    api_keys: list[str] = field(default_factory=list)


async def links_by_subject(
    session: AsyncSession, organization_id: str
) -> dict[str, AgentGroupLink]:
    """:return: The organization's linked groups, by their subject."""
    links = await session.scalars(
        select(AgentGroupLink).where(AgentGroupLink.organization_id == organization_id)
    )
    return {group_subject(link.group_name): link for link in links}


async def links_by_permission(
    session: AsyncSession, organization_id: str
) -> dict[tuple[str, str], Linked]:
    """
    :return: Each linked agent permission's ``(resource, action)`` mapped to
        the groups and keys linked to it.
    """
    groups = await links_by_subject(session, organization_id)
    found: dict[tuple[str, str], Linked] = {}
    for rule in await grant_lines(session, organization_id, GROUP_PREFIX):
        link = groups.get(rule.v0 or "")
        if link is not None:
            grant = (rule.v2 or "", rule.v3 or "")
            found.setdefault(grant, Linked()).groups.append(link.group_name)
    for rule in await grant_lines(session, organization_id, API_KEY_SUBJECT_PREFIX):
        key_id = key_id_of(rule.v0)
        if key_id is not None:
            grant = (rule.v2 or "", rule.v3 or "")
            found.setdefault(grant, Linked()).api_keys.append(key_id)
    for linked in found.values():
        linked.groups.sort()
        linked.api_keys.sort()
    return found


async def _set_subjects(
    session: AsyncSession,
    permission: AgentPermission,
    prefix: str,
    wanted: Mapping[str, str],
) -> dict[str, str]:
    """
    Link a permission to exactly these subjects of one kind (a prefix's):
    those it's linked to and shouldn't be are unlinked, the new ones linked.

    :param wanted: The subjects, mapped to what they stand for (a group's name).
    :return: Those newly linked.
    """
    organization_id = permission.organization_id
    added = dict(wanted)
    grant = (permission.resource, permission.action)
    for rule in await grant_lines(session, organization_id, prefix):
        if (rule.v2, rule.v3) != grant:
            continue
        if rule.v0 in added:
            del added[rule.v0 or ""]
        else:
            await session.delete(rule)
    domain = team_domain(organization_id)
    session.add_all(
        AgentCasbinRule(
            ptype="p",
            v0=subject,
            v1=domain,
            v2=permission.resource,
            v3=permission.action,
        )
        for subject in sorted(added)
    )
    return added


async def set_permission_groups(
    session: AsyncSession, permission: AgentPermission, names: Iterable[str]
) -> None:
    """
    Link an agent permission to exactly these company groups (recording the
    new ones), forgetting groups left linked to nothing.

    :param names: The groups' names, as the directory spells them.
    """
    organization_id = permission.organization_id
    added = await _set_subjects(
        session,
        permission,
        GROUP_PREFIX,
        {group_subject(name): name for name in names},
    )
    known = await links_by_subject(session, organization_id)
    session.add_all(
        AgentGroupLink(organization_id=organization_id, group_name=name)
        for subject, name in sorted(added.items())
        if subject not in known
    )
    await session.flush()
    await drop_unlinked_groups(session, organization_id)


async def set_permission_keys(
    session: AsyncSession, permission: AgentPermission, key_ids: Iterable[str]
) -> None:
    """
    Link an agent permission to exactly these API keys (the organization's,
    checked by the caller).
    """
    await _set_subjects(
        session,
        permission,
        API_KEY_SUBJECT_PREFIX,
        {key_subject(key_id): key_id for key_id in key_ids},
    )
    await session.flush()


async def drop_unlinked_groups(session: AsyncSession, organization_id: str) -> None:
    """Forget the organization's groups no permission is linked to any more."""
    await session.flush()
    linked = {
        rule.v0 for rule in await grant_lines(session, organization_id, GROUP_PREFIX)
    }
    for subject, link in (await links_by_subject(session, organization_id)).items():
        if subject not in linked:
            await session.delete(link)


async def delete_group_rules(
    session: AsyncSession, organization_id: str, name: str
) -> None:
    """Unlink a group from every agent permission of the organization."""
    await session.execute(
        delete(AgentCasbinRule).where(
            AgentCasbinRule.ptype == "p",
            AgentCasbinRule.v0 == group_subject(name),
            AgentCasbinRule.v1 == team_domain(organization_id),
        )
    )
