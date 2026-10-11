"""Organizations' API credentials, for the hosted runtime's agents: their HTTP
and OpenAPI tools' requests signed with the credential each names, as this
API keeps it (:class:`OrmCredentialStore`), only where the credential may go
(``forge_mcp_servers.api_credentials``).

And what the hosted runtime's tools may read of this process's environment:
``${NAME}`` in a tool's URL or headers is filled in only for the names the
deployment lists (``agent_tool_environment``), never for its own secrets.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from forge_agent_runtime import RequestAuth
from forge_mcp_servers import ApiCredentials, request_credentials
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.models import ApiCredential

#: What a ``${NAME}`` the hosted runtime doesn't have adds to its error.
TOOL_ENVIRONMENT_NOTE = (
    ": hosted runs fill in only the names FORGE_ADMIN_AGENT_TOOL_ENVIRONMENT lists. "
    "Use one of the organization's credentials instead"
)


def tool_environment(
    names: list[str], environment: Mapping[str, str]
) -> dict[str, str]:
    """:return: The variables of ``environment`` that ``names`` lists: what tools read."""
    return {name: environment[name] for name in names if name in environment}


class OrmCredentialStore:
    """Credentials as this API keeps them: a row locked while open, committed as it closes."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @asynccontextmanager
    async def open(self, credential_id: str) -> AsyncIterator[ApiCredential | None]:
        async with self.sessions() as session:
            credential = await session.get(
                ApiCredential, credential_id, with_for_update=True
            )
            yield credential
            await session.commit()


class OrganizationCredentials:
    """An agent's tools' credentials (the runtime's ``CredentialProvider``):
    its organization's, a renewed OAuth grant kept on the credential."""

    def __init__(
        self, credentials: ApiCredentials, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        self.credentials = credentials
        self.store = OrmCredentialStore(sessions)

    async def __call__(
        self, credential_id: str, *, organization_id: str | None, url: str
    ) -> RequestAuth:
        found = await request_credentials(
            self.credentials,
            self.store,
            credential_id=credential_id,
            organization_id=organization_id,
            url=url,
        )
        return RequestAuth(headers=found.headers, params=found.params)
