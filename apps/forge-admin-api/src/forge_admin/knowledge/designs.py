"""A system design knowledge base's design, as its agents read it: its
groups, its applications (the code repositories it includes), its components
(the other parts of the system: databases, queues, external systems…) and
how they all connect, each named, in the shape ``forge_codegraph.system``
describes. The admin API's knowledge base tools, a workflow run's snapshot
and the code graph's MCP server (through ``/code-graph/access``) all read
it here.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.models import (
    CodeRepository,
    KnowledgeBase,
    KnowledgeBaseComponent,
    KnowledgeBaseConnection,
    KnowledgeBaseGroup,
    KnowledgeBaseRepository,
)
from forge_admin.models.knowledge import SYSTEM

#: A design: ``{groups, applications, components, connections}``.
Design = dict[str, list[dict[str, str]]]


def _empty() -> Design:
    return {"groups": [], "applications": [], "components": [], "connections": []}


async def designs_of(
    session: AsyncSession, knowledge_base_ids: Sequence[str]
) -> dict[str, Design]:
    """
    :return: Each knowledge base's design, by knowledge base: its groups
        ``{name, description}`` by name; its applications ``{name, url,
        group}`` (``owner/name``) by URL; its components ``{name, kind,
        technology, description, group}`` by name; and its connections
        ``{source, kind, target, description}``, the ends by name, oldest
        first. ``group`` is a group's name, or empty.
    """
    found: dict[str, Design] = {i: _empty() for i in knowledge_base_ids}
    if not knowledge_base_ids:
        return found
    wanted = list(found)
    groups = {
        g.id: g
        for g in await session.scalars(
            select(KnowledgeBaseGroup)
            .where(KnowledgeBaseGroup.knowledge_base_id.in_(wanted))
            .order_by(KnowledgeBaseGroup.name, KnowledgeBaseGroup.id)
        )
    }

    def group(group_id: str | None) -> str:
        return groups[group_id].name if group_id in groups else ""

    for g in groups.values():
        found[g.knowledge_base_id]["groups"].append(
            {"name": g.name, "description": g.description}
        )
    names: dict[str, str] = {}
    for knowledge_base_id, link, repository in await session.execute(
        select(
            KnowledgeBaseRepository.knowledge_base_id,
            KnowledgeBaseRepository,
            CodeRepository,
        )
        .join(
            CodeRepository, CodeRepository.id == KnowledgeBaseRepository.repository_id
        )
        .where(KnowledgeBaseRepository.knowledge_base_id.in_(wanted))
        .order_by(CodeRepository.url)
    ):
        name = f"{repository.owner}/{repository.name}"
        names[repository.id] = name
        found[knowledge_base_id]["applications"].append(
            {"name": name, "url": repository.url, "group": group(link.group_id)}
        )
    for component in await session.scalars(
        select(KnowledgeBaseComponent)
        .where(KnowledgeBaseComponent.knowledge_base_id.in_(wanted))
        .order_by(KnowledgeBaseComponent.name, KnowledgeBaseComponent.id)
    ):
        names[component.id] = component.name
        found[component.knowledge_base_id]["components"].append(
            {
                "name": component.name,
                "kind": component.kind,
                "technology": component.technology,
                "description": component.description,
                "group": group(component.group_id),
            }
        )
    for connection in await session.scalars(
        select(KnowledgeBaseConnection)
        .where(KnowledgeBaseConnection.knowledge_base_id.in_(wanted))
        .order_by(KnowledgeBaseConnection.created_at, KnowledgeBaseConnection.id)
    ):
        found[connection.knowledge_base_id]["connections"].append(
            {
                "source": names.get(source_of(connection) or "", "?"),
                "kind": connection.kind,
                "target": names.get(target_of(connection) or "", "?"),
                "description": connection.description,
            }
        )
    return found


def source_of(connection: KnowledgeBaseConnection) -> str | None:
    """:return: The ID of the application (repository) or component it's from."""
    return connection.source_repository_id or connection.source_component_id


def target_of(connection: KnowledgeBaseConnection) -> str | None:
    """:return: The ID of the application (repository) or component it's to."""
    return connection.target_repository_id or connection.target_component_id


async def organization_designs(
    session: AsyncSession, organization_id: str
) -> list[dict[str, Any]]:
    """:return: Each of the organization's system design knowledge bases,
    by name: ``{id, name, description}`` and its design
    (:func:`designs_of`)."""
    bases = list(
        await session.scalars(
            select(KnowledgeBase)
            .where(
                KnowledgeBase.organization_id == organization_id,
                KnowledgeBase.kind == SYSTEM,
            )
            .order_by(KnowledgeBase.name)
        )
    )
    designs = await designs_of(session, [kb.id for kb in bases])
    return [
        {"id": kb.id, "name": kb.name, "description": kb.description, **designs[kb.id]}
        for kb in bases
    ]
