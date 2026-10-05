"""Scopes, their Casbin domains, and authorizing requests with Casbin.

A scope is the site or an organization. Its Casbin domain is ``site`` or
``org:<id>``; a role assigned in a scope is stored under the domain followed
by ``*`` (the site's is ``*``), so a site-wide role also covers every
organization. See ``casbin_model.conf``.
"""

import re
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

import casbin
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.auth.authorization import assignments_of, get_enforcer, group_subject
from forge_admin.models import Organization

UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# "site", or "org:<uuid>".
SCOPE_PATTERN = rf"^(?:site|org:{UUID_PATTERN})$"

Enforcer = Annotated[casbin.AsyncEnforcer, Depends(get_enforcer)]


class Level(StrEnum):
    """A level of the hierarchy, from the top; also the prefix of role keys."""

    SITE = "site"
    ORG = "org"


@dataclass(frozen=True)
class Scope:
    """
    One place in the hierarchy.

    :param level: The level.
    :param id: The organization's ID; empty for the site.
    """

    level: Level
    id: str = ""

    @classmethod
    def parse(cls, text: str) -> "Scope":
        """
        Read a scope written as ``site`` or ``org:<id>``.

        :param text: E.g. ``org:3f6c...``.
        :return: The scope.
        :raises ValueError: The text is not a scope.
        """
        if not re.fullmatch(SCOPE_PATTERN, text):
            raise ValueError(f"invalid scope {text!r}")
        if text == Level.SITE:
            return SITE
        level, _, identifier = text.partition(":")
        return cls(Level(level), identifier)

    @classmethod
    def from_pattern(cls, pattern: str) -> "Scope":
        """
        Read the scope a role assignment's domain pattern names.

        :param pattern: E.g. ``org:<id>*``, or ``*`` for the site.
        :return: The scope, e.g. the organization.
        """
        path = pattern.removesuffix("*")
        return cls.parse(path) if path else SITE

    def __str__(self) -> str:
        return (
            self.level.value if self.level is Level.SITE else f"{self.level}:{self.id}"
        )


SITE = Scope(Level.SITE)
# What each level is called in messages.
NOUNS = {
    Level.SITE: "Site",
    Level.ORG: "Organization",
}


def assignment_pattern(domain: str) -> str:
    """
    The domain pattern a role assigned in a scope is stored under.

    :param domain: The scope's domain, e.g. ``org:<id>``.
    :return: The pattern covering the scope.
    """
    return "*" if domain == Level.SITE else f"{domain}*"


async def domain_of(session: AsyncSession, scope: Scope) -> str:
    """
    Return a scope's Casbin domain.

    :param session: The request's database session.
    :param scope: The scope.
    :return: E.g. ``org:<id>``, or ``site``.
    :raises HTTPException: 404 when the scope does not exist.
    """
    if scope.level is Level.SITE:
        return Level.SITE.value
    if await session.get(Organization, scope.id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"{NOUNS[scope.level]} not found"
        )
    return f"org:{scope.id}"


@dataclass(frozen=True)
class OrganizationMembership:
    """
    An organization someone is a member of, and their roles in it.

    :param organization: The organization.
    :param roles: Their roles in it, sorted, e.g. ``org:member``.
    """

    organization: Organization
    roles: list[str]


async def organization_memberships(
    session: AsyncSession, subject: str
) -> list[OrganizationMembership]:
    """
    List the organizations a subject is a member of: those they hold a role
    in. A site-wide role doesn't make them a member of an organization.

    :param session: The database session.
    :param subject: The user or service ID.
    :return: Their organizations, ordered by name.
    """
    roles: dict[str, list[str]] = {}
    for rule in await assignments_of(session, subject):
        scope = Scope.from_pattern(rule.v2 or "")
        if scope.level is Level.ORG:
            roles.setdefault(scope.id, []).append(rule.v1 or "")
    if not roles:
        return []
    organizations = await session.scalars(
        select(Organization)
        .where(Organization.id.in_(roles))
        .order_by(Organization.name)
    )
    return [
        OrganizationMembership(organization, sorted(roles[organization.id]))
        for organization in organizations
    ]


# The company groups the request's user is in, from their token (or
# FORGE_ADMIN_LOCAL_USER_GROUPS): security.authenticate sets them.
_caller: ContextVar[tuple[str | None, tuple[str, ...]]] = ContextVar(
    "forge_admin_caller", default=(None, ())
)


def set_caller(user: str | None, groups: Iterable[str]) -> None:
    """Record who's calling, and their groups, for the rest of the request."""
    _caller.set((user, tuple(groups)))


def groups_of_caller(user: str) -> tuple[str, ...]:
    """
    :param user: A user being authorized.
    :return: Their company groups, when they're the one calling; none for
        anyone else (their groups are only known from their own token).
    """
    caller, groups = _caller.get()
    return groups if caller == user else ()


def current_user(request: Request) -> str:
    """
    FastAPI dependency: the calling user's ID.

    The user ``security.authenticate`` recorded: from the request's bearer
    token or, in local development, ``FORGE_ADMIN_LOCAL_USER_ID``.

    :param request: The current request.
    :return: The user ID.
    :raises HTTPException: 401 when the request has no user.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No signed-in user")
    return str(user_id)


CurrentUser = Annotated[str, Depends(current_user)]


def allows(
    enforcer: casbin.AsyncEnforcer, user: str, permission: str, domain: str
) -> bool:
    """
    Ask the loaded enforcer whether a user holds a permission in a domain:
    through their roles, or (when they're the caller) one of their company
    groups, whose linked permissions hold everywhere.

    :param enforcer: The Casbin enforcer, with its policy loaded.
    :param user: The user ID.
    :param permission: A permission key, e.g. ``agents:run``.
    :param domain: The scope's domain.
    :return: Casbin's decision.
    """
    resource, action = permission.split(":")
    subjects = [user, *(group_subject(g) for g in groups_of_caller(user))]
    return any(enforcer.enforce(s, domain, resource, action) for s in subjects)


async def authorize(
    session: AsyncSession,
    enforcer: casbin.AsyncEnforcer,
    user: str,
    permission: str,
    scope: Scope,
) -> str:
    """
    Require a permission in a scope, reloading the policy from MySQL first.

    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :param user: The caller.
    :param permission: A permission key, e.g. ``agents:run``.
    :param scope: The scope acted on.
    :return: The scope's domain.
    :raises HTTPException: 404 when the scope does not exist, 403 without the
        permission.
    """
    domain = await domain_of(session, scope)
    await enforcer.load_policy()
    if not allows(enforcer, user, permission, domain):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires {permission}")
    return domain
