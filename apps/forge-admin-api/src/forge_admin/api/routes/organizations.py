"""Organizations: set up by site administrators, where the work happens."""

import logging

from fastapi import APIRouter, Request, status
from pymongo.errors import PyMongoError
from sqlalchemy import select

from forge_admin.api.routes.common import (
    Audited,
    NodeCreate,
    NodeId,
    NodeUpdate,
    Session,
    apply_update,
    as_read,
    commit_or_conflict,
)
from forge_admin.auth.access import (
    SITE,
    CurrentUser,
    Enforcer,
    Level,
    Scope,
    allows,
    assignment_pattern,
    authorize,
)
from forge_admin.auth.authorization import remove_assignments_in
from forge_admin.models import CasbinRule, Organization, Role
from forge_admin.models.hierarchy import new_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/organizations", tags=["organizations"])

NAME_TAKEN = "An organization with this name exists"
# The role whoever creates an organization gets in it, so it's theirs to run.
CREATOR_ROLE = "org:admin"


class OrganizationRead(Audited):
    id: str
    name: str
    description: str
    # Its Casbin domain, for enforcing with the shared model elsewhere.
    domain: str


def _read(organization: Organization) -> OrganizationRead:
    return as_read(OrganizationRead, organization, domain=f"org:{organization.id}")


@router.get("")
async def list_organizations(
    user: CurrentUser, session: Session, enforcer: Enforcer
) -> list[OrganizationRead]:
    """
    List the organizations the caller can view.
    \f
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: Organizations ordered by name.
    """
    await enforcer.load_policy()
    organizations = await session.scalars(
        select(Organization).order_by(Organization.name)
    )
    return [
        _read(org)
        for org in organizations
        if allows(enforcer, user, "organizations:read", f"org:{org.id}")
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: NodeCreate, user: CurrentUser, session: Session, enforcer: Enforcer
) -> OrganizationRead:
    """
    Create an organization. Requires ``organizations:create`` on the site.
    Its creator becomes its administrator (``org:admin``), a member who opens
    its workspace, in the same transaction.
    \f
    :param body: The name and description.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The new organization.
    """
    await authorize(session, enforcer, user, "organizations:create", SITE)
    # Unless the role was deleted, which leaves the organization memberless.
    # Looked up first: with the organization added, the query would flush it,
    # and a taken name would fail there rather than as a 409 below.
    creator_role = await session.get(Role, CREATOR_ROLE)
    organization = Organization(
        id=new_id(), name=body.name, description=body.description
    )
    session.add(organization)
    if creator_role is not None:
        session.add(
            CasbinRule(
                ptype="g",
                v0=user,
                v1=CREATOR_ROLE,
                v2=assignment_pattern(f"org:{organization.id}"),
            )
        )
    await commit_or_conflict(session, NAME_TAKEN)
    return _read(organization)


@router.get("/{organization_id}")
async def get_organization(
    organization_id: NodeId, user: CurrentUser, session: Session, enforcer: Enforcer
) -> OrganizationRead:
    """
    Get one organization.
    \f
    :param organization_id: The organization.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The organization.
    """
    scope = Scope(Level.ORG, organization_id)
    await authorize(session, enforcer, user, "organizations:read", scope)
    organization = await session.get(Organization, organization_id)
    assert organization is not None  # authorize found it
    return _read(organization)


@router.patch("/{organization_id}")
async def update_organization(
    organization_id: NodeId,
    body: NodeUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> OrganizationRead:
    """
    Rename or describe an organization.
    \f
    :param organization_id: The organization.
    :param body: The fields to change.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The updated organization.
    """
    scope = Scope(Level.ORG, organization_id)
    await authorize(session, enforcer, user, "organizations:update", scope)
    organization = await session.get(Organization, organization_id)
    assert organization is not None
    apply_update(organization, body)
    await commit_or_conflict(session, NAME_TAKEN)
    return _read(organization)


@router.delete("/{organization_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Delete an organization and every role assigned in it. Its agents (its ADK
    workflows) leave MongoDB; an organization that's gone can't have them
    replaced, so failing to remove them is only logged.
    \f
    :param organization_id: The organization.
    :param request: The request, for the agent store.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    """
    scope = Scope(Level.ORG, organization_id)
    domain = await authorize(session, enforcer, user, "organizations:delete", scope)
    await remove_assignments_in(session, assignment_pattern(domain))
    await session.delete(await session.get(Organization, organization_id))
    await session.commit()
    store = request.app.state.organization_agents
    if store is None:
        return
    try:
        await store.delete_organization(organization_id)
    except PyMongoError as error:
        logger.warning(
            "Organization %s is deleted, but its agents are still in MongoDB: %s",
            organization_id,
            error,
        )
