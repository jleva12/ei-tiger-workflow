"""
An OpenAPI spec as an agent's OpenAPI tool reads it, for the builders to
list its operations (each a tool the agent may call) and the credentials it
says it takes: fetched as the hosted runtime fetches one (never from this
deployment's own network; with the tool's credential, where it may go) or
as pasted, and read with ADK's parser (``forge_agent_runtime.openapi``), so
the operations are named as the agent's tools are.

Needs ``agents:manage`` in the organization: it's what building an agent or a
workflow takes, and the spec is fetched as the organization.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from forge_agent_runtime import RuntimeServices
from forge_agent_runtime.openapi import SpecError, describe_spec, fetch_spec, load_spec
from forge_agent_runtime.services import RequestAuth
from forge_agent_runtime.tools import MissingSetting, fill_references, http_client
from forge_mcp_servers import ApiCredentials, CredentialUnavailable, sends_to
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin import env_files
from forge_admin.api.routes.common import NodeId, Session
from forge_admin.api_credentials import TOOL_ENVIRONMENT_NOTE, tool_environment
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.config import Settings
from forge_admin.models import ApiCredential

router = APIRouter(tags=["OpenAPI specs"])

MANAGE = "agents:manage"


class SpecSource(BaseModel):
    """Where an OpenAPI tool's spec comes from, as its settings say."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["url", "inline"] = "url"
    url: str = Field(default="", max_length=2048)
    #: JSON or YAML.
    spec: str = Field(default="", max_length=10_000_000)
    #: One of the organization's credentials, by ID: sent with the request
    #: for the spec when it may go there.
    credential: str = Field(default="", max_length=64)


class OperationRead(BaseModel):
    #: The tool's name, as the agent calls it and the tool's operations name it.
    name: str
    operation_id: str
    method: str
    path: str
    summary: str
    description: str


class SecurityRead(BaseModel):
    name: str
    type: str
    location: str
    parameter: str
    scheme: str


class SpecRead(BaseModel):
    title: str
    version: str
    servers: list[str]
    operations: list[OperationRead]
    #: The credentials the spec says its API takes.
    security: list[SecurityRead]


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)


@router.post("/organizations/{organization_id}/openapi/operations")
async def read_openapi_spec(
    organization_id: NodeId,
    body: SpecSource,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> SpecRead:
    """
    What a spec offers an agent: its operations as tools, by the names the
    agent calls them, and the credentials its API takes.
    \f
    :raises HTTPException: 403 without agents:manage in the organization; 422
        when the spec can't be fetched or read.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    settings: Settings = request.app.state.settings
    if body.source == "inline":
        text = body.spec.strip()
        if not text:
            raise _unprocessable("Paste the spec: JSON or YAML.")
    else:
        try:
            url = fill_references(
                body.url.strip(),
                tool_environment(
                    settings.agent_tool_environment, env_files.environment()
                ),
                where="The spec URL",
                note=TOOL_ENVIRONMENT_NOTE,
            )
        except MissingSetting as error:
            raise _unprocessable(str(error)) from None
        if not url:
            raise _unprocessable("Give the spec's URL.")
        services = RuntimeServices(
            environment={},
            allow_private=settings.agent_tools_allow_private,
            allowed_hosts=tuple(settings.agent_tools_allowed_hosts),
        )
        auth = await _credential_for(
            request, session, organization_id, body.credential, url
        )
        async with http_client(
            transport=getattr(request.app.state, "spec_transport", None)
        ) as http:
            try:
                text = await fetch_spec(url, services=services, http=http, auth=auth)
            except SpecError as error:
                raise _unprocessable(str(error)) from None
    try:
        found = await describe_spec(load_spec(text))
    except SpecError as error:
        raise _unprocessable(str(error)) from None
    return SpecRead.model_validate(
        {
            "title": found.title,
            "version": found.version,
            "servers": found.servers,
            "operations": [vars(operation) for operation in found.operations],
            "security": [vars(scheme) for scheme in found.security],
        }
    )


async def _credential_for(
    request: Request,
    session: AsyncSession,
    organization_id: str,
    credential_id: str,
    url: str,
) -> RequestAuth | None:
    """What fetching the spec sends: the tool's credential, when it may go
    there; a renewed sign-in is kept on it."""
    credentials: ApiCredentials | None = getattr(
        request.app.state, "api_credentials", None
    )
    if not credential_id or credentials is None:
        return None
    credential = await session.get(ApiCredential, credential_id, with_for_update=True)
    if credential is None or credential.organization_id != organization_id:
        raise _unprocessable("That credential was deleted; pick another.")
    if not sends_to(url, credential.urls):
        return None
    try:
        found = await credentials.for_request(credential, url)
    except CredentialUnavailable as error:
        raise _unprocessable(str(error)) from None
    finally:
        await session.commit()
    return RequestAuth(headers=found.headers, params=found.params)
