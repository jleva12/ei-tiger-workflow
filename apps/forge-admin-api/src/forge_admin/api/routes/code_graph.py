"""
What a credential may read of the code graph, for the code graph's MCP server
(apps/forge-codegraph-mcp): it asks here about every credential it's sent
(passing it on as it came) and serves only the repositories answered.

A credential reads one organization's repositories: an organization's API
key its own organization's; a sign-in token the organization it was minted
for, which its ``org_id`` claim names (``forge-admin-token --organization``).
Either needs ``repositories:read`` there. The graph keeps one repository per
GitHub URL, whichever organizations ingest it; an organization reads those
of its code repositories' URLs.
"""

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from forge_admin.api.routes.common import Session
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.models import CodeRepository

router = APIRouter(tags=["code graph"])

READ = "repositories:read"

NO_ORGANIZATION = (
    "This credential names no organization: send an organization's API key, "
    "or a token minted for one (forge-admin-token --organization)"
)


class GraphRepository(BaseModel):
    """One of the organization's repositories."""

    #: ``https://github.com/<owner>/<name>``, lowercase: the graph's name for
    #: it, from which its ID in the graph follows.
    url: str
    owner: str
    name: str
    branch: str


class CodeGraphAccess(BaseModel):
    """Whose credential it is, and which repositories it reads."""

    #: The caller: a user's ID, or ``apikey:<id>``.
    subject: str
    organization_id: str
    repositories: list[GraphRepository]


@router.get("/code-graph/access")
async def code_graph_access(
    request: Request, session: Session, enforcer: Enforcer, user: CurrentUser
) -> CodeGraphAccess:
    """
    The repositories the calling credential reads: its organization's.

    :raises HTTPException: 401 without a valid credential; 403 for one that
        names no organization, or without ``repositories:read`` in it; 404
        when its organization is gone.
    """
    organization = getattr(request.state, "organization_id", None)
    if organization is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NO_ORGANIZATION)
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization))
    rows = await session.scalars(
        select(CodeRepository)
        .where(CodeRepository.organization_id == organization)
        .order_by(CodeRepository.url)
    )
    return CodeGraphAccess(
        subject=user,
        organization_id=organization,
        repositories=[
            GraphRepository(url=r.url, owner=r.owner, name=r.name, branch=r.branch)
            for r in rows
        ],
    )
