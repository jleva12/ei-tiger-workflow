"""Service metadata, mounted below the API prefix."""

from importlib.metadata import version

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(tags=["service"])


class ServiceInfo(BaseModel):
    name: str
    version: str


@router.get("/info", summary="Service information")
async def info(request: Request) -> ServiceInfo:
    """
    Return the application name and version.
    \f
    :param request: The current request, used to read the settings.
    :return: The service name and version.
    """
    return ServiceInfo(
        name=request.app.state.settings.name, version=version("forge-admin")
    )
