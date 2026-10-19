"""Service metadata, mounted below the API prefix."""

from importlib.metadata import version

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(tags=["service"])


class ServiceInfo(BaseModel):
    name: str
    version: str
    #: The most seconds an agent waits for a workflow it called (a Workflow
    #: tool's "Wait up to" at most).
    agent_workflow_wait_max: float


@router.get("/info", summary="Service information")
async def info(request: Request) -> ServiceInfo:
    """
    Return the application name and version, and how long agents may wait
    for workflows they call.
    \f
    :param request: The current request, used to read the settings.
    :return: The service name, version and workflow wait cap.
    """
    settings = request.app.state.settings
    return ServiceInfo(
        name=settings.name,
        version=version("forge-admin"),
        agent_workflow_wait_max=settings.agent_workflow_wait_max,
    )
