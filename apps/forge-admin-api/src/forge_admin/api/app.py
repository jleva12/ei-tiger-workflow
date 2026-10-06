"""The application: which routers and middleware the admin API serves.

Serve it with ``forge-admin`` (which applies migrations first) or directly
with ``uvicorn forge_admin.api.app:create_app --factory``.
"""

from fastapi import APIRouter, FastAPI
from forge_common.logging import configure_logging

from forge_admin.api.routes import (
    adk_workflow_runs,
    adk_workflows,
    api_keys,
    assistant,
    authz,
    chat_agents,
    knowledge_bases,
    knowledge_collections,
    knowledge_documents,
    mcp_servers,
    me,
    members,
    organizations,
    overview,
    permission_groups,
    permissions,
    roles,
    users,
)
from forge_admin.api.server import ApiServer
from forge_admin.config import get_settings

# Every router the admin API serves, below the API prefix.
ROUTERS = [
    organizations.router,
    overview.router,
    adk_workflows.router,
    adk_workflow_runs.router,
    chat_agents.router,
    knowledge_bases.router,
    knowledge_documents.router,
    knowledge_collections.router,
    mcp_servers.router,
    api_keys.router,
    members.router,
    roles.router,
    permissions.router,
    permission_groups.router,
    users.router,
    assistant.router,
    me.router,
    authz.router,
]

# Routers that check their callers themselves, at the root: never behind the
# API key or a user's token. None at the moment.
PUBLIC_ROUTERS: list[APIRouter] = []


def create_app() -> FastAPI:
    """
    Build the admin API from the environment.

    Add each new router to ``ROUTERS``, or to ``PUBLIC_ROUTERS`` when its
    callers aren't Forge users.

    :return: The FastAPI application.
    """
    settings = get_settings()
    # Here as well as in forge-admin: uvicorn's reloader serves the app from a
    # process of its own, which calls only this factory.
    configure_logging(settings.logging)
    return ApiServer(
        settings, routers=ROUTERS, public_routers=PUBLIC_ROUTERS
    ).create_app()
