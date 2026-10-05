"""Recording what an organization's agents and assistant use, as they run here.

Workflow runs are recorded by the async worker that runs them. Here:

- :class:`AgentUsage`: the hosted runtime's chat agents. Each built agent
  carries a ``UsagePlugin`` for its organization (every model and tool call,
  its sub-agents' too), and each turn is an invocation.
- :func:`assistant_attribution`: the assistant, which every member shares:
  a turn is the organization's whose page it was asked from, when the person
  is one of its members; otherwise it isn't recorded.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from forge_agent_runtime import ResolvedAgent, RunRequest
from forge_common.adk.usage import Attribution, PriceOf, UsagePlugin
from forge_task_adk_workflows.usage_store import UsageStore
from google.adk.plugins.base_plugin import BasePlugin

#: The assistant's subject ID: there's one.
ASSISTANT = "assistant"


class AgentUsage:
    """The hosted runtime's observer (``forge_agent_runtime.RunObserver``)."""

    def __init__(self, store: UsageStore, *, price: PriceOf | None = None) -> None:
        self.store = store
        self.price = price

    @staticmethod
    def _attribution(agent: ResolvedAgent) -> Attribution | None:
        doc = agent.document
        if not doc.organization_id:
            return None
        return Attribution(doc.organization_id, "agent", doc.id, doc.name)

    def plugins(self, agent: ResolvedAgent) -> Sequence[BasePlugin]:
        attribution = self._attribution(agent)
        if attribution is None:
            return ()
        return (UsagePlugin(self.store.record, attribution, price=self.price),)

    def invocation(
        self, agent: ResolvedAgent, request: RunRequest
    ) -> AbstractAsyncContextManager[object]:
        attribution = self._attribution(agent)
        if attribution is None:
            return contextlib.nullcontext()
        return self.store.invocation(
            attribution,
            version=str(agent.version) if agent.version is not None else None,
            session_id=request.session_id,
            user_id=request.user_id,
        )


def assistant_attribution(
    page_context: Any, organizations: Sequence[Mapping[str, Any]] | None, user_id: str
) -> Attribution | None:
    """
    :param page_context: The session's ``page_context``: what the page the
        person asks from shows (``entities``, each a ``kind`` and ``id``).
    :param organizations: The person's organizations (each with an ``id``).
    :param user_id: The person.
    :return: Whom the turn's calls are for: the organization the page is in,
        when it's one of theirs; None otherwise.
    """
    if not isinstance(page_context, Mapping) or not organizations:
        return None
    entities = page_context.get("entities")
    if not isinstance(entities, list):
        return None
    theirs = {str(org.get("id")) for org in organizations if isinstance(org, Mapping)}
    for entity in entities:
        if isinstance(entity, Mapping) and entity.get("kind") == "organization":
            organization_id = entity.get("id")
            if isinstance(organization_id, str) and organization_id in theirs:
                return Attribution(
                    organization_id,
                    "assistant",
                    ASSISTANT,
                    "Assistant",
                    user_id=user_id,
                )
    return None
