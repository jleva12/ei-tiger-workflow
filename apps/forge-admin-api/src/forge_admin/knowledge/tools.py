"""Knowledge bases as chat agents' tools: what the hosted runtime's
``knowledge_base`` nodes search (``forge_agent_runtime.services.KnowledgeBases``).

An agent names its organization's knowledge bases by ID; another
organization's, or one deleted since, isn't found, and the agent doesn't
build. A search finds the passages of the knowledge bases' documents that
best match the model's query, each with the document it's from, so the agent
can say where an answer comes from.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.knowledge.search import KnowledgeSearch
from forge_admin.models import KnowledgeBase, KnowledgeDocument


class OrganizationKnowledgeBases:
    """
    Describes and searches an organization's knowledge bases for its agents.

    :param sessions: Makes the admin database's sessions.
    :param search: The search over what the async worker embedded; None when
        it isn't set up, and every search says so.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        search: KnowledgeSearch | None,
    ) -> None:
        self._sessions = sessions
        self._search = search

    async def describe(
        self, knowledge_base_ids: Sequence[str], *, organization_id: str | None
    ) -> list[tuple[str, str]]:
        """
        :return: Each knowledge base's name and description, in order.
        :raises LookupError: The organization has no such knowledge base.
        """
        found = await self._find(knowledge_base_ids, organization_id)
        return [(found[i].name, found[i].description) for i in knowledge_base_ids]

    async def search(
        self,
        knowledge_base_ids: Sequence[str],
        query: str,
        *,
        organization_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        :return: The passages of these knowledge bases, and only these, that
            best match the query, ranked together, best first: each naming
            the knowledge base and the document it's from.
        :raises LookupError: The organization has no such knowledge base.
        :raises RuntimeError: Searching isn't set up.
        """
        found = await self._find(knowledge_base_ids, organization_id)
        if self._search is None:
            raise RuntimeError(
                "Knowledge base search isn't set up on this server (FORGE_ADMIN_MONGO_URI)."
            )
        passages = await self._search.search(
            list(knowledge_base_ids), query, limit=limit
        )
        async with self._sessions() as session:
            names = {
                document_id: filename
                for document_id, filename in (
                    await session.execute(
                        select(KnowledgeDocument.id, KnowledgeDocument.filename).where(
                            KnowledgeDocument.id.in_({p.document_id for p in passages})
                        )
                    )
                )
            }
        return [
            passage.for_agent(
                knowledge_base=found[passage.knowledge_base_id].name,
                document=names[passage.document_id],
            )
            # A document removed since its chunks were found is left out.
            for passage in passages
            if passage.document_id in names
        ]

    async def _find(
        self, knowledge_base_ids: Sequence[str], organization_id: str | None
    ) -> dict[str, KnowledgeBase]:
        if organization_id is None:
            raise LookupError(
                "Only an organization's agents can search its knowledge bases"
            )
        async with self._sessions() as session:
            found = {
                kb.id: kb
                for kb in await session.scalars(
                    select(KnowledgeBase).where(
                        KnowledgeBase.id.in_(list(knowledge_base_ids)),
                        KnowledgeBase.organization_id == organization_id,
                    )
                )
            }
        missing = [i for i in knowledge_base_ids if i not in found]
        if missing:
            raise LookupError(
                f"The organization has no knowledge base {missing[0]}: pick another"
            )
        return found
