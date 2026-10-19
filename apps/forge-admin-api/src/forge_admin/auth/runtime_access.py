"""Who may call the runtime: the organizations' agents and workflows, over
ADK's run API, A2A and the workflow API (``{api_prefix}/runtime``).

Every call needs an organization's API key or a Forge sign-in, holding
``agents:run`` in the organization that owns the agent or workflow called; a
key only ever reaches its own organization's. An agent's or workflow's draft
needs ``agents:manage`` there: it's whoever edits it that tries it. A chat
page's visitors are the only callers without either: its link lets them in
(``forge_admin.chat_agents.pages``).

Callers are let in by the agent's ID and organization before anything of it
is read (:func:`admit_call`): a caller with no standing in its organization
is told there's no such agent, as for one there isn't. And a call reaches
only the caller's own conversations (:func:`conversation_owner`), whatever
user ID it names: a person's are theirs, a key's are its end users', kept
apart from every other key's.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, params, status
from forge_agent_runtime.server import owned_by
from sqlalchemy import select

from forge_admin.auth.access import Level, Scope, allows, authorize
from forge_admin.auth.api_keys import KeyIdentity, key_id_of
from forge_admin.auth.security import authenticate
from forge_admin.config import Settings
from forge_admin.models import Organization

RUN = "agents:run"
#: What trying an agent's or workflow's draft needs: editing it.
MANAGE = "agents:manage"
#: What a person needs in an organization to know of its agents at all.
MEMBER = "organizations:read"

NO_CALLER = "Send an API key or a sign-in token: Authorization: Bearer <it>"
OTHER_ORGANIZATION = "This API key belongs to another organization"
NOT_YOURS = "Use your own user ID: this is someone else's"


@dataclass(frozen=True)
class RuntimeCaller:
    """Who's calling the runtime."""

    #: ``apikey:<id>``, a user's ID, or None when anonymous.
    subject: str | None
    #: The key's name; None for people (``name_of`` names them) and anonymous.
    name: str | None = None
    #: The key's organization; None for people and anonymous.
    organization_id: str | None = None
    groups: tuple[str, ...] = ()

    @property
    def is_api_key(self) -> bool:
        return key_id_of(self.subject) is not None

    @property
    def is_anonymous(self) -> bool:
        return self.subject is None


def caller_of(request: Request) -> RuntimeCaller:
    """
    FastAPI dependency: who's calling, as ``authenticate`` or ``identify``
    recorded them.
    """
    key: KeyIdentity | None = getattr(request.state, "api_key", None)
    return RuntimeCaller(
        subject=getattr(request.state, "user_id", None),
        name=key.name if key is not None else None,
        organization_id=key.organization_id if key is not None else None,
        groups=tuple(getattr(request.state, "groups", ())),
    )


Caller = Annotated[RuntimeCaller, Depends(caller_of)]


def runtime_dependencies(settings: Settings) -> list[params.Depends]:
    """:return: The runtime routers' dependencies: a key or a sign-in, identified."""
    return [Depends(authenticate)]


async def require_organization(request: Request, organization_id: str | None) -> None:
    """Reject orphaned external records after their SQL organization is deleted."""
    if organization_id is not None:
        async with request.app.state.sessionmaker() as session:
            found = await session.scalar(
                select(Organization.id).where(Organization.id == organization_id)
            )
        if found is not None:
            return
    raise HTTPException(status.HTTP_404_NOT_FOUND, "No such organization")


def _known(request: Request) -> RuntimeCaller:
    """:raises HTTPException: 401 for a call without a key or a sign-in."""
    caller = caller_of(request)
    if caller.subject is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            NO_CALLER,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return caller


async def authorize_call(
    request: Request,
    organization_id: str | None,
    *,
    permission: str = RUN,
) -> RuntimeCaller:
    """
    Check that the caller may call something of an organization's: a caller
    holding the permission there (an API key only in its own organization).

    Opens a short session of its own, so a route that goes on to wait holds
    no connection.

    :param request: The request.
    :param organization_id: The organization the agent or workflow is
        theirs; None for one that has none.
    :param permission: What's needed there.
    :return: The caller.
    :raises HTTPException: 401 without a caller; 403 for another
        organization's key or without the permission; 404 when the
        organization is gone.
    """
    caller = _known(request)
    if organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires {permission}")
    if caller.organization_id is not None and caller.organization_id != organization_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, OTHER_ORGANIZATION)
    async with request.app.state.sessionmaker() as session:
        await authorize(
            session,
            request.app.state.enforcer,
            str(caller.subject),
            permission,
            Scope(Level.ORG, organization_id),
        )
    return caller


async def require_standing(
    request: Request, organization_id: str | None, *, missing: str = "No such agent"
) -> RuntimeCaller:
    """
    Check that the caller is someone of an organization's at all: its own
    API key, or a person holding organizations:read or agents:run there.
    Anyone else is told there's no such thing, as for one there isn't.

    :param missing: What they're told: 404.
    :raises HTTPException: 401 without a caller; 404 as said.
    """
    caller = _known(request)
    if organization_id is None:
        return caller
    if caller.organization_id is not None and caller.organization_id != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, missing)
    if not caller.is_api_key:
        enforcer = request.app.state.enforcer
        await enforcer.load_policy()
        domain, subject = f"org:{organization_id}", str(caller.subject)
        if not (
            allows(enforcer, subject, MEMBER, domain)
            or allows(enforcer, subject, RUN, domain)
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, missing)
    return caller


async def admit_call(
    request: Request,
    organization_id: str | None,
    *,
    permission: str = RUN,
    missing: str = "No such agent",
) -> RuntimeCaller:
    """
    :func:`authorize_call`, for something called by an ID a caller might
    guess: one with no standing in its organization (another organization's
    key, a person who isn't one of its members) is told there's none, as
    for one there isn't, rather than that it's another organization's
    (:func:`require_standing`).

    :param missing: What they're told: 404.
    :raises HTTPException: 404 as said; otherwise as ``authorize_call``.
    """
    await require_standing(request, organization_id, missing=missing)
    return await authorize_call(request, organization_id, permission=permission)


async def conversation_owner(request: Request, user_id: str) -> str:
    """
    Whose conversations a runtime call reaches (the run API's ``owner``),
    from who the caller is rather than from the user ID it names: a
    person's are their own, and they name themself; a key's are its end
    users', each kept under the key and the user ID the key names
    (``apikey:<id>/<user>``, ``forge_agent_runtime.server.owned_by``), so
    no other key, nor anyone else, reaches them.

    :param user_id: The user ID the call names (ADK's ``userId``).
    :return: The user ID its conversations are kept under.
    :raises HTTPException: 401 without a caller; 403 for a person naming
        anyone but themself.
    """
    caller = _known(request)
    assert caller.subject is not None
    if caller.is_api_key:
        return owned_by(caller.subject, user_id)
    if user_id != caller.subject:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_YOURS)
    return caller.subject
