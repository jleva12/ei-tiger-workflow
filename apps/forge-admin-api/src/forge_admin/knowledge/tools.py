"""Knowledge bases as chat agents' tools: what the hosted runtime's
``knowledge_base`` nodes search (``forge_agent_runtime.services.KnowledgeBases``).

An agent names its organization's knowledge bases by ID; another
organization's, or one deleted since, isn't found, and the agent doesn't
build. A search finds the passages that best match the model's query: of a
RAG knowledge base's documents, each with the document it's from; of a graph
knowledge base's code, each with the repository, file and declaration
(``forge_admin.knowledge.graph``). So the agent can say where an answer
comes from. A system design knowledge base's design (its applications,
components, groups and connections, ``forge_admin.knowledge.designs``) is
read whole, or around one part, by the tool beside the search.
"""

from collections.abc import Sequence
from typing import Any

from forge_codegraph import design_summary, design_view, interleave
from forge_codegraph_query import CodeGraph, GraphError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.knowledge.designs import designs_of
from forge_admin.knowledge.graph import search_code, searchable
from forge_admin.knowledge.search import KnowledgeSearch
from forge_admin.models import KnowledgeBase, KnowledgeDocument
from forge_admin.models.knowledge import SYSTEM


class OrganizationKnowledgeBases:
    """
    Describes and searches an organization's knowledge bases for its agents.

    :param sessions: Makes the admin database's sessions.
    :param search: The search over what the async worker embedded; None when
        it isn't set up, and every search of a RAG knowledge base says so.
    :param codegraph: The code graph (``forge_codegraph_query``); None when
        it isn't set up, and every search of a system design knowledge base
        says so.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        search: KnowledgeSearch | None,
        codegraph: CodeGraph | None = None,
    ) -> None:
        self._sessions = sessions
        self._search = search
        self._codegraph = codegraph

    async def describe(
        self, knowledge_base_ids: Sequence[str], *, organization_id: str | None
    ) -> list[tuple[str, str]]:
        """
        :return: Each knowledge base's name and what it holds, in order: its
            description, and a system design knowledge base's applications,
            other parts, groups and how they connect.
        :raises LookupError: The organization has no such knowledge base.
        """
        found = await self._find(knowledge_base_ids, organization_id)
        systems = [
            i for i in dict.fromkeys(knowledge_base_ids) if found[i].kind == SYSTEM
        ]
        async with self._sessions() as session:
            designs = await designs_of(session, systems)

        def about(knowledge_base: KnowledgeBase) -> str:
            if knowledge_base.kind != SYSTEM:
                return knowledge_base.description
            return design_summary(
                knowledge_base.description, designs[knowledge_base.id]
            )

        return [(found[i].name, about(found[i])) for i in knowledge_base_ids]

    async def design(
        self,
        knowledge_base_ids: Sequence[str],
        *,
        organization_id: str | None,
        focus: str = "",
    ) -> dict[str, Any] | None:
        """
        :return: The design of the system design knowledge bases among
            these (``forge_codegraph.design_view``), whole or around the
            part ``focus`` names; None when none of them is one.
        :raises LookupError: The organization has no such knowledge base.
        """
        found = await self._find(knowledge_base_ids, organization_id)
        systems = [
            i for i in dict.fromkeys(knowledge_base_ids) if found[i].kind == SYSTEM
        ]
        if not systems:
            return None
        async with self._sessions() as session:
            designs = await designs_of(session, systems)
        return design_view(
            [(found[i].name, found[i].description, designs[i]) for i in systems],
            focus,
        )

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
            best match the query, best first: each naming the knowledge base
            and the document (or the repository's file) it's from. Documents'
            and code's are ranked separately, then taken in turn.
        :raises LookupError: The organization has no such knowledge base.
        :raises RuntimeError: Searching isn't set up, or the code graph is
            unavailable.
        """
        found = await self._find(knowledge_base_ids, organization_id)
        wanted = list(dict.fromkeys(knowledge_base_ids))
        documents = [i for i in wanted if found[i].kind != SYSTEM]
        code = [i for i in wanted if found[i].kind == SYSTEM]
        ranked: list[list[dict[str, Any]]] = []
        if documents:
            ranked.append(await self._documents(documents, found, query, limit))
        if code:
            ranked.append(await self._code(code, found, query, limit))
        return interleave(ranked, limit)

    async def _documents(
        self,
        knowledge_base_ids: list[str],
        found: dict[str, KnowledgeBase],
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if self._search is None:
            raise RuntimeError(
                "Knowledge base search isn't set up on this server (FORGE_ADMIN_MONGO_URI)."
            )
        passages = await self._search.search(knowledge_base_ids, query, limit=limit)
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

    async def _code(
        self,
        knowledge_base_ids: list[str],
        found: dict[str, KnowledgeBase],
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if self._codegraph is None:
            raise RuntimeError(
                "System design knowledge base search isn't set up on this server "
                "(FORGE_ADMIN_CODEGRAPH__SPANNER__DATABASE)."
            )
        async with self._sessions() as session:
            repositories = await searchable(session, knowledge_base_ids)
        try:
            return await search_code(
                self._codegraph,
                [(found[i], repositories[i]) for i in knowledge_base_ids],
                query,
                limit=limit,
            )
        except GraphError as error:
            raise RuntimeError(f"The code graph couldn't search: {error}.") from None

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
