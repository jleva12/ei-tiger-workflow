"""The calling user: who they are, and their access, which the web console
loads; and the agents of their organizations, which the desktop app offers."""

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from forge_agent_runtime import AgentNotFound
from pydantic import BaseModel
from pymongo.errors import PyMongoError

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
from forge_admin.chat_agents.pages import (
    ModelWindow,
    agent_names,
    entry_model,
    workflow_names,
)
from forge_admin.chat_agents.runtime import NOT_SET_UP, UNAVAILABLE, executor_of
from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.models import User

logger = logging.getLogger(__name__)

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


class MyChatAgent(BaseModel):
    """A published chat agent of one of the caller's organizations."""

    #: Its ID: the ``appName`` the runtime runs its latest published version as.
    id: str
    organization_id: str
    organization_name: str
    name: str
    description: str
    #: The published version it runs now.
    version: int
    #: Its agents by the names ADK calls them, as the canvas names them.
    agents: dict[str, str]
    #: Its workflow tools by the names the model calls them, as the canvas names them.
    workflows: dict[str, str]
    #: The model it starts on: the context meter's window before the first reply.
    model: ModelWindow


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


@router.get("/chat-agents")
async def get_my_chat_agents(
    request: Request, user: CurrentUser, session: Session
) -> list[MyChatAgent]:
    """
    List the published chat agents of the organizations the caller is a
    member of, each as its latest published version has it: what the
    desktop app offers to add, and runs as the caller through the runtime
    (their conversations are theirs: ``userId`` their subject). Whether they
    may run one is the runtime's to say, as it runs it.
    \f
    :param request: The request.
    :param user: The caller.
    :param session: The request's database session.
    :return: The agents, by organization name, then agent name.
    :raises HTTPException: 503 when chat agents aren't set up or MongoDB
        isn't answering.
    """
    store: ChatAgentStore | None = getattr(request.app.state, "chat_agents", None)
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)
    executor = executor_of(request)
    memberships = await organization_memberships(session, user)
    found: list[MyChatAgent] = []
    try:
        for membership in memberships:
            organization = membership.organization
            for record in await store.list(organization.id):
                if record.get("published_version") is None:
                    continue
                try:
                    agent = await executor.resolve(record["_id"])
                except AgentNotFound:
                    continue
                found.append(
                    MyChatAgent(
                        id=agent.document.id,
                        organization_id=organization.id,
                        organization_name=organization.name,
                        name=agent.document.name,
                        description=agent.document.description,
                        version=int(record["published_version"]),
                        agents=agent_names(agent.document),
                        workflows=workflow_names(agent.document),
                        model=entry_model(executor, agent),
                    )
                )
    except PyMongoError as error:
        logger.warning("Couldn't list the caller's chat agents: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
    return sorted(
        found, key=lambda agent: (agent.organization_name.lower(), agent.name.lower())
    )
