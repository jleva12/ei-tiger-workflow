"""
A system design knowledge base's components and groups: the parts of its
system that aren't its applications (code repositories), which people add
on its system map (a database, a queue or topic, a cache, storage, an
external system, a service without a repository here, a client), and the
named parts of the system drawn around some of its applications and
components (a domain, a team's services, a network), each saying what it is
and what it's for. Agents searching the knowledge base are told both.

Where they are on the map and which group each application and component
is in are the map's (``knowledge_connections``, ``…/map``); a component
may be put in a group as it's added.

Reading needs ``organizations:read`` in the organization; adding, changing
and removing need ``knowledge_bases:manage``.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import (
    Audited,
    Name,
    NodeId,
    Session,
    as_read,
    commit_or_conflict,
)
from forge_admin.auth.access import CurrentUser, Enforcer
from forge_admin.knowledge.access import knowledge_base_of
from forge_admin.models import KnowledgeBaseComponent, KnowledgeBaseGroup
from forge_admin.models.knowledge import SYSTEM, ComponentKind

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge bases"])

BASE = "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}"
COMPONENTS = f"{BASE}/components"
GROUPS = f"{BASE}/groups"
NO_COMPONENT = "The knowledge base has no such component"
NO_GROUP = "The knowledge base has no such group"
Technology = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
About = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]


# ----------------------------------------------------------------- components


class ComponentRead(Audited):
    id: str
    knowledge_base_id: str
    kind: ComponentKind
    name: str
    # What it runs on, e.g. PostgreSQL 16 or Kafka; may be empty.
    technology: str
    # What it holds or does; may be empty.
    description: str


class ComponentCreate(BaseModel):
    kind: ComponentKind
    name: Name
    technology: Technology = ""
    description: About = ""
    # The knowledge base's group to put it in, optionally.
    group_id: Annotated[str, StringConstraints(max_length=36)] | None = None


class ComponentUpdate(BaseModel):
    kind: ComponentKind | None = None
    name: Name | None = None
    technology: Technology | None = None
    description: About | None = None


async def _component(
    session: AsyncSession, knowledge_base_id: str, component_id: str
) -> KnowledgeBaseComponent:
    """:raises HTTPException: 404 for another knowledge base's component."""
    found = await session.get(KnowledgeBaseComponent, component_id)
    if found is None or found.knowledge_base_id != knowledge_base_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_COMPONENT)
    return found


async def _group(
    session: AsyncSession, knowledge_base_id: str, group_id: str
) -> KnowledgeBaseGroup:
    """:raises HTTPException: 404 for another knowledge base's group."""
    found = await session.get(KnowledgeBaseGroup, group_id)
    if found is None or found.knowledge_base_id != knowledge_base_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_GROUP)
    return found


@router.get(COMPONENTS)
async def list_components(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[ComponentRead]:
    """
    The knowledge base's components, by name.
    \f
    :raises HTTPException: 403 without ``organizations:read``; 404 for
        another organization's knowledge base, or none; 409 for a RAG one.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, kind=SYSTEM
    )
    rows = await session.scalars(
        select(KnowledgeBaseComponent)
        .where(KnowledgeBaseComponent.knowledge_base_id == knowledge_base_id)
        .order_by(KnowledgeBaseComponent.name, KnowledgeBaseComponent.id)
    )
    return [as_read(ComponentRead, row) for row in rows]


@router.post(COMPONENTS, status_code=status.HTTP_201_CREATED)
async def add_component(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    body: ComponentCreate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ComponentRead:
    """
    Add a part of the system that isn't one of its applications: a
    database, a queue or topic, a cache, storage, an external system, a
    service without a repository here, or a client. Connect applications
    and other components to it on the map.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another organization's knowledge base, or none, or a group it
        doesn't have; 409 when it has a component by that name, or for a
        RAG knowledge base.
    """
    await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    if body.group_id is not None:
        await _group(session, knowledge_base_id, body.group_id)
    row = KnowledgeBaseComponent(
        knowledge_base_id=knowledge_base_id,
        kind=body.kind,
        name=body.name,
        technology=body.technology,
        description=body.description,
        group_id=body.group_id,
    )
    session.add(row)
    await commit_or_conflict(
        session, f"The knowledge base has a component named {body.name!r}"
    )
    logger.info(
        "%s added %s component %s to knowledge base %s",
        user,
        row.kind,
        row.id,
        knowledge_base_id,
    )
    return as_read(ComponentRead, row)


@router.patch(f"{COMPONENTS}/{{component_id}}")
async def update_component(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    component_id: NodeId,
    body: ComponentUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ComponentRead:
    """
    Change what a component is, its name, what it runs on or its
    description. Its connections stay.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another knowledge base's component; 409 when another has the name.
    """
    await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    row = await _component(session, knowledge_base_id, component_id)
    for field in ("kind", "name", "technology", "description"):
        value = getattr(body, field)
        if value is not None:
            setattr(row, field, value)
    await commit_or_conflict(
        session, f"The knowledge base has a component named {row.name!r}"
    )
    return as_read(ComponentRead, row)


@router.delete(f"{COMPONENTS}/{{component_id}}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_component(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    component_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Remove a component from the map, with its connections.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another knowledge base's component.
    """
    await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    row = await _component(session, knowledge_base_id, component_id)
    # Its connections go with it; none has code links, which join two
    # applications, so the code graph needn't hear.
    await session.delete(row)
    await session.commit()
    logger.info(
        "%s removed component %s from knowledge base %s",
        user,
        component_id,
        knowledge_base_id,
    )


# --------------------------------------------------------------------- groups


class GroupRead(Audited):
    id: str
    knowledge_base_id: str
    name: str
    # What it is and what it's for; may be empty.
    description: str


class GroupCreate(BaseModel):
    name: Name
    description: About = ""


class GroupUpdate(BaseModel):
    name: Name | None = None
    description: About | None = None


@router.get(GROUPS)
async def list_groups(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[GroupRead]:
    """
    The knowledge base's groups, by name. What's in each is the map's.
    \f
    :raises HTTPException: 403 without ``organizations:read``; 404 for
        another organization's knowledge base, or none; 409 for a RAG one.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, kind=SYSTEM
    )
    rows = await session.scalars(
        select(KnowledgeBaseGroup)
        .where(KnowledgeBaseGroup.knowledge_base_id == knowledge_base_id)
        .order_by(KnowledgeBaseGroup.name, KnowledgeBaseGroup.id)
    )
    return [as_read(GroupRead, row) for row in rows]


@router.post(GROUPS, status_code=status.HTTP_201_CREATED)
async def add_group(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    body: GroupCreate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> GroupRead:
    """
    Add a group to the map, saying what it is and what it's for; put
    applications and components in it on the map.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another organization's knowledge base, or none; 409 when it has a
        group by that name, or for a RAG knowledge base.
    """
    await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    row = KnowledgeBaseGroup(
        knowledge_base_id=knowledge_base_id,
        name=body.name,
        description=body.description,
    )
    session.add(row)
    await commit_or_conflict(
        session, f"The knowledge base has a group named {body.name!r}"
    )
    logger.info(
        "%s added group %s to knowledge base %s", user, row.id, knowledge_base_id
    )
    return as_read(GroupRead, row)


@router.patch(f"{GROUPS}/{{group_id}}")
async def update_group(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    group_id: NodeId,
    body: GroupUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> GroupRead:
    """
    Rename a group, or say again what it is and what it's for.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another knowledge base's group; 409 when another has the name.
    """
    await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    row = await _group(session, knowledge_base_id, group_id)
    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    await commit_or_conflict(
        session, f"The knowledge base has a group named {row.name!r}"
    )
    return as_read(GroupRead, row)


@router.delete(f"{GROUPS}/{{group_id}}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_group(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    group_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Remove a group. What was in it stays on the map, in no group.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another knowledge base's group.
    """
    await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    row = await _group(session, knowledge_base_id, group_id)
    await session.delete(row)
    await session.commit()
    logger.info(
        "%s removed group %s from knowledge base %s",
        user,
        group_id,
        knowledge_base_id,
    )
