"""The calling user: who they are, and their access, which the web console loads."""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from forge_admin.api.routes.common import Session
from forge_admin.api.routes.users import UserRead
from forge_admin.auth.access import (
    SCOPE_PATTERN,
    CurrentUser,
    Enforcer,
    Scope,
    domain_of,
    groups_of_caller,
    organization_memberships,
)
from forge_admin.auth.authorization import assignments_of, load_access
from forge_admin.models import User

router = APIRouter(prefix="/me", tags=["me"])

ScopeQuery = Annotated[
    str,
    Query(
        pattern=SCOPE_PATTERN,
        description="site or org:<id>",
    ),
]


class Me(BaseModel):
    subject: str
    # Their row in users; null for a subject that isn't a user, e.g. a service.
    user: UserRead | None


class Access(BaseModel):
    subject: str
    scope: str
    # The scope's Casbin domain.
    domain: str
    # Roles held in the scope or above it.
    roles: list[str]
    # Casbin p lines granting their permissions: [role, resource, action].
    policies: list[list[str]]


class Membership(BaseModel):
    scope: str
    role: str


class MyOrganization(BaseModel):
    id: str
    name: str
    description: str
    # The caller's roles in the organization, e.g. ``org:member``.
    roles: list[str]


@router.get("")
async def get_me(user: CurrentUser, session: Session) -> Me:
    """
    Return who the caller is: their subject ID and, if they're a user, their
    profile.
    \f
    :param user: The caller.
    :param session: The request's database session.
    :return: The subject and user.
    """
    record = await session.get(User, user)
    return Me(subject=user, user=UserRead.model_validate(record) if record else None)


@router.get("/access")
async def get_my_access(
    user: CurrentUser, session: Session, enforcer: Enforcer, scope: ScopeQuery = "site"
) -> Access:
    """
    Return the calling user's effective roles and permissions in a scope, as
    the web console's ``fromCasbin`` reads them: their roles', and those
    linked to the company groups their token names.
    \f
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :param scope: The scope; the site by default.
    :return: The roles and permission policy lines.
    """
    domain = await domain_of(session, Scope.parse(scope))
    roles, policies = await load_access(enforcer, user, domain, groups_of_caller(user))
    return Access(
        subject=user, scope=scope, domain=domain, roles=roles, policies=policies
    )


@router.get("/memberships")
async def get_my_memberships(user: CurrentUser, session: Session) -> list[Membership]:
    """
    List every role the calling user holds, and where, e.g. to list their
    organizations.
    \f
    :param user: The caller.
    :param session: The request's database session.
    :return: The user's role assignments.
    """
    return [
        Membership(scope=str(Scope.from_pattern(rule.v2 or "")), role=rule.v1 or "")
        for rule in await assignments_of(session, user)
    ]


@router.get("/organizations")
async def get_my_organizations(
    user: CurrentUser, session: Session
) -> list[MyOrganization]:
    """
    List the organizations the caller is a member of: those they hold a role
    in. A site-wide role doesn't make them a member of one; only members work
    in an organization.
    \f
    :param user: The caller.
    :param session: The request's database session.
    :return: Organizations ordered by name, each with the caller's roles in it.
    """
    return [
        MyOrganization(
            id=membership.organization.id,
            name=membership.organization.name,
            description=membership.organization.description,
            roles=membership.roles,
        )
        for membership in await organization_memberships(session, user)
    ]
