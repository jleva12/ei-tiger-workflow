"""Who may call the runtime: the organizations' agents and workflows, over
ADK's run API, A2A and the workflow API (``{api_prefix}/runtime``).

One deployment setting decides, ``agent_runtime_public``:

- Public (the default): anyone may call, with no credentials. Credentials
  that are sent must be valid, and say who's calling: A2A tasks and runs
  are then theirs, and usage is theirs.
- Not public: every call needs an organization's API key or a Forge
  sign-in, holding ``agents:run`` in the organization that owns the agent or
  workflow called; a key only ever reaches its own organization's.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, params, status
from sqlalchemy import select

from forge_admin.auth.access import Level, Scope, authorize
from forge_admin.auth.api_keys import KeyIdentity, key_id_of
from forge_admin.auth.security import authenticate, identify
from forge_admin.config import Settings
from forge_admin.models import User

RUN = "agents:run"

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
    """
    :return: The runtime routers' dependencies: identifying callers from what
        they send when it's public, else asking for a key or a sign-in.
    """
    return [Depends(identify if settings.agent_runtime_public else authenticate)]


def is_public(request: Request) -> bool:
    """:return: Whether the runtime takes calls from anyone."""
    return bool(request.app.state.settings.agent_runtime_public)


async def authorize_call(
    request: Request,
    organization_id: str | None,
    *,
    permission: str = RUN,
) -> RuntimeCaller:
    """
    Check that the caller may call something of an organization's: anyone
    may when the runtime is public; otherwise a caller holding the
    permission there (an API key only in its own organization).

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
    caller = caller_of(request)
    if is_public(request):
        return caller
    if caller.subject is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            NO_CALLER,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires {permission}")
    if caller.organization_id is not None and caller.organization_id != organization_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, OTHER_ORGANIZATION)
    async with request.app.state.sessionmaker() as session:
        await authorize(
            session,
            request.app.state.enforcer,
            caller.subject,
            permission,
            Scope(Level.ORG, organization_id),
        )
    return caller


async def check_conversation_user(
    request: Request, caller: RuntimeCaller, user_id: str
) -> None:
    """
    Check whose conversations a caller asks for, by ADK's ``userId``: when
    the runtime isn't public, a person's are their own; a key's are its end
    users', any ID but a Forge user's or another key's. Otherwise anyone
    allowed to call an agent could read everyone's conversations with it.

    :raises HTTPException: 403 for someone else's.
    """
    if is_public(request) or caller.subject is None or user_id == caller.subject:
        return
    if not caller.is_api_key or key_id_of(user_id) is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_YOURS)
    async with request.app.state.sessionmaker() as session:
        person = await session.scalar(select(User.id).where(User.id == user_id))
    if person is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_YOURS)
