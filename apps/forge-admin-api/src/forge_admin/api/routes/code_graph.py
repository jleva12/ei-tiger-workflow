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

The answer also carries what the organization's system design knowledge
bases say about how those repositories connect: each connection people drew
on a system map, with its code links, and the owners (``kb:<id>``) of the
cross-repository links they wrote into the graph, so the server follows only
the organization's own.
"""

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from forge_admin.api.routes.common import Session
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.knowledge.connections import organization_map
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


class ConnectionEnd(BaseModel):
    """One of a connection's applications: one of the organization's
    repositories, by its URL (the graph's name for it)."""

    url: str
    owner: str
    name: str


class CodeEndpoint(BaseModel):
    """One end of a code link: a node of that application's code graph."""

    node_id: str
    kind: str
    name: str
    qualified_name: str
    path: str


class CodeLinkAccess(BaseModel):
    source: CodeEndpoint
    target: CodeEndpoint
    #: What connects them, e.g. ``POST /v1/orders``; may be empty.
    label: str


class ConnectionAccess(BaseModel):
    """How one application connects to another, as drawn on a system map."""

    id: str
    #: The system design knowledge base whose map it's on.
    knowledge_base_id: str
    knowledge_base: str
    #: connects_to, calls, depends_on, events or shares_data.
    kind: str
    description: str
    source: ConnectionEnd
    target: ConnectionEnd
    #: Where in the code it happens.
    code_links: list[CodeLinkAccess]


class CodeGraphAccess(BaseModel):
    """Whose credential it is, which repositories it reads, and how they
    connect."""

    #: The caller: a user's ID, or ``apikey:<id>``.
    subject: str
    organization_id: str
    repositories: list[GraphRepository]
    #: The connections on the organization's system maps.
    connections: list[ConnectionAccess] = []
    #: Who keeps the organization's cross-repository links in the graph.
    link_owners: list[str] = []


@router.get("/code-graph/access")
async def code_graph_access(
    request: Request, session: Session, enforcer: Enforcer, user: CurrentUser
) -> CodeGraphAccess:
    """
    The repositories the calling credential reads (its organization's), the
    connections between them on its system maps, and the owners of its
    cross-repository links.

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
    repositories = [
        GraphRepository(url=r.url, owner=r.owner, name=r.name, branch=r.branch)
        for r in rows
    ]
    owners, connections = await organization_map(session, organization)
    return CodeGraphAccess(
        subject=user,
        organization_id=organization,
        repositories=repositories,
        connections=[ConnectionAccess.model_validate(c) for c in connections],
        link_owners=owners,
    )
