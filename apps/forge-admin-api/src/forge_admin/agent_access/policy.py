"""The organizations' agent access policy: its lines, and checking it.

The model is ``agent_casbin_model.conf``, the policy the
``agent_casbin_rule`` table. Every line is a ``p`` line naming its
organization's domain, ``org:<id>``: an agent permission linked to a company
group (``group:<name>``) or to one of the organization's API keys
(``apikey:<id>``). The API writes them in the same transaction as the
permissions and group links they belong to.

Checks read one organization's lines into an enforcer of their own, so
they're always current and never see another organization's.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import casbin
from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.auth.authorization import group_subject
from forge_admin.models import AgentCasbinRule, AgentPermission

MODEL_PATH = Path(__file__).with_name("agent_casbin_model.conf")


def team_domain(organization_id: str) -> str:
    """:return: An organization's domain in the policy, ``org:<id>``."""
    return f"org:{organization_id}"


def _in(organization_id: str) -> ColumnElement[bool]:
    """The condition picking an organization's lines."""
    return (AgentCasbinRule.ptype == "p") & (
        AgentCasbinRule.v1 == team_domain(organization_id)
    )


# Grants ---------------------------------------------------------------------


async def granted_permissions(
    session: AsyncSession, organization_id: str, subjects: Sequence[str]
) -> dict[str, list[AgentPermission]]:
    """
    List the agent permissions linked to each of the given subjects (company
    groups or API keys) in an organization, in one query.

    :param session: The request's database session.
    :param organization_id: The organization.
    :param subjects: E.g. ``group:Finance``, ``apikey:<id>``.
    :return: Each subject mapped to its permissions, ordered by resource and
        action; subjects without any map to an empty list.
    """
    grants: dict[str, list[AgentPermission]] = {subject: [] for subject in subjects}
    rows = await session.execute(
        select(AgentCasbinRule.v0, AgentPermission)
        .join(
            AgentPermission,
            (AgentPermission.organization_id == organization_id)
            & (AgentCasbinRule.v2 == AgentPermission.resource)
            & (AgentCasbinRule.v3 == AgentPermission.action),
        )
        .where(_in(organization_id), AgentCasbinRule.v0.in_(subjects))
        .order_by(AgentPermission.resource, AgentPermission.action)
    )
    for subject, permission in rows:
        if subject is not None:
            grants[subject].append(permission)
    return grants


async def grant_lines(
    session: AsyncSession, organization_id: str, prefix: str
) -> list[AgentCasbinRule]:
    """
    :return: An organization's lines for subjects with a prefix, e.g. every
        company group's (``group:``) or API key's (``apikey:``).
    """
    rules = await session.scalars(
        select(AgentCasbinRule).where(
            _in(organization_id),
            AgentCasbinRule.v0.startswith(prefix, autoescape=True),
        )
    )
    return list(rules)


async def move_permission_rules(
    session: AsyncSession,
    organization_id: str,
    old: tuple[str, str],
    new: tuple[str, str],
) -> None:
    """Point every link of an agent permission at its new resource and action."""
    await session.execute(
        update(AgentCasbinRule)
        .where(
            _in(organization_id),
            AgentCasbinRule.v2 == old[0],
            AgentCasbinRule.v3 == old[1],
        )
        .values(v2=new[0], v3=new[1])
    )


async def delete_permission_rules(
    session: AsyncSession, organization_id: str, resource: str, action: str
) -> None:
    """Unlink an agent permission from every company group and API key."""
    await session.execute(
        delete(AgentCasbinRule).where(
            _in(organization_id),
            AgentCasbinRule.v2 == resource,
            AgentCasbinRule.v3 == action,
        )
    )


async def remove_subject(session: AsyncSession, subject: str) -> None:
    """Unlink a subject that is going (an API key) from every agent permission."""
    await session.execute(
        delete(AgentCasbinRule).where(
            AgentCasbinRule.ptype == "p", AgentCasbinRule.v0 == subject
        )
    )


async def remove_team_rules(session: AsyncSession, organization_id: str) -> None:
    """Remove every line of an organization that is being deleted."""
    await session.execute(delete(AgentCasbinRule).where(_in(organization_id)))


# Checking access ------------------------------------------------------------


async def team_enforcer(session: AsyncSession, organization_id: str) -> casbin.Enforcer:
    """
    :return: An enforcer holding one organization's lines, and only those.
    """
    enforcer = casbin.Enforcer(str(MODEL_PATH))
    rules = await session.scalars(select(AgentCasbinRule).where(_in(organization_id)))
    for rule in rules:
        enforcer.add_policy(
            *[v for v in (rule.v0, rule.v1, rule.v2, rule.v3) if v is not None]
        )
    return enforcer


@dataclass(frozen=True)
class AgentAccess:
    """
    What someone may do with an organization's agents.

    :param policies: The lines granting them permissions, ``[subject,
        resource, action]``: their company groups' (``group:<name>``), or,
        for an API key, its own (``apikey:<id>``).
    """

    policies: list[list[str]]

    @property
    def permissions(self) -> list[str]:
        """:return: The permissions they hold, ``resource:action``, sorted."""
        return sorted({f"{resource}:{action}" for _, resource, action in self.policies})


async def agent_access(
    session: AsyncSession,
    organization_id: str,
    subject: str,
    groups: Iterable[str] = (),
) -> AgentAccess:
    """
    Evaluate someone's agent permissions in an organization.

    :param session: The database session.
    :param organization_id: The organization.
    :param subject: A user's ID, or an API key's.
    :param groups: The company groups their sign-in names.
    :return: Their agent access there.
    """
    enforcer = await team_enforcer(session, organization_id)
    held = {subject} | {group_subject(name) for name in groups}
    policies = sorted(
        [rule[0], rule[2], rule[3]] for rule in enforcer.get_policy() if rule[0] in held
    )
    return AgentAccess(policies)


async def agent_allows(
    session: AsyncSession,
    organization_id: str,
    subject: str,
    permission: str,
    groups: Iterable[str] = (),
) -> bool:
    """
    Whether someone holds an agent permission in an organization: through a
    company group their sign-in names that's linked to it there, or, for an
    API key, being linked to it itself.

    :param session: The database session.
    :param organization_id: The organization.
    :param subject: A user's ID, or an API key's.
    :param permission: ``resource:action``, e.g. ``invoices:approve``.
    :param groups: The company groups their sign-in names.
    :return: Casbin's decision.
    """
    if not re.fullmatch(r"[^:]+:[^:]+", permission):
        raise ValueError(f"invalid permission {permission!r}")
    enforcer = await team_enforcer(session, organization_id)
    return holds_any(enforcer, organization_id, subject, [permission], groups)


def holds_any(
    enforcer: casbin.Enforcer,
    organization_id: str,
    subject: str,
    permissions: Iterable[str],
    groups: Iterable[str] = (),
) -> bool:
    """
    Whether someone holds any of these agent permissions, asking an enforcer
    of the organization's (``team_enforcer``), so several checks read its
    lines once.

    :param permissions: ``resource:action`` each.
    :return: Casbin's decision; False for no permissions.
    """
    domain = team_domain(organization_id)
    subjects = [subject, *(group_subject(name) for name in groups)]
    return any(
        enforcer.enforce(s, domain, *permission.split(":", 1))
        for permission in permissions
        for s in subjects
    )
