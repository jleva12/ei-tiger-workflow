"""Service metadata, mounted below the API prefix."""

from importlib.metadata import version

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(tags=["service"])


class ServiceInfo(BaseModel):
    name: str
    version: str
    #: Whether anyone may call the runtime (agents and workflows) without an
    #: API key or a sign-in.
    agent_runtime_public: bool


@router.get("/info", summary="Service information")
async def info(request: Request) -> ServiceInfo:
    """
    Return the application name and version, and whether its runtime asks
    callers for an API key or a sign-in.
    \f
    :param request: The current request, used to read the settings.
    :return: The service name, version and runtime access.
    """
    settings = request.app.state.settings
    return ServiceInfo(
        name=settings.name,
        version=version("forge-admin"),
        agent_runtime_public=settings.agent_runtime_public,
    )
