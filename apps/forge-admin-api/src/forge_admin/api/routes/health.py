"""Liveness and readiness probes. They sit outside the API prefix and the docs."""

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from forge_admin.db.session import ping

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", include_in_schema=False)


@router.get("/live")
async def live() -> dict[str, str]:
    """
    Handles health check requests for the application.

    This endpoint is generally used to verify if the application is running and
    healthy. It returns a simple status in JSON format indicating the application's
    operational state. It does not query MySQL.

    :return: A dictionary containing a health status message.
    :rtype: dict[str, str]
    """
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """
    Handles the readiness probe for the service to check if the application is up
    and its dependencies are accessible.

    This endpoint performs a health check to verify if the application is ready
    to serve requests. It ensures that the database connection (MySQL) is
    reachable within a configured timeout. If the check fails (e.g., the database
    is unavailable), a "503 Service Unavailable" status is returned.

    :param request: The HTTP request object, which provides access to
                    application state and settings.
                    Type: `Request`
    :return: A `JSONResponse` object containing the readiness status.
             If the application and its dependencies are available, the response
             contains {"status": "ok"} with a 200 HTTP status. Otherwise, the
             response contains {"status": "unavailable"} with a 503 HTTP status.
    :rtype: JSONResponse
    """
    timeout = request.app.state.settings.ready_timeout
    try:
        async with asyncio.timeout(timeout):
            await ping(request.app.state.engine)
    except Exception as error:
        logger.warning("readiness probe: MySQL unavailable: %r", error)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})
