"""Chat pages: a published chat agent shared as a full-page chat at a link of
its own (``/chat/<token>`` in the web console), for people outside Forge to
talk to it.

Mounted at ``{api_prefix}/runtime``, beside the runtime's other routes:

- ``GET /runtime/pages/{token}``: what the page shows: the agent's name and
  description, the page's settings (its words and prompts, what it leaves
  out, whether it suggests follow-up questions, its look: ``page_settings``),
  its agents' and workflow tools' names, and the app name its chat calls.
- ADK's run API under ``/runtime/pages/{token}``: ``POST …/run_sse``, and a
  visitor's conversations under ``…/apps/{app}/users/{user}/sessions``, as
  ``/runtime`` serves them (``forge_agent_runtime.server``).

The link is the credential. It answers whether or not the runtime is public,
and only for its agent's latest published version (``ca_x``, or the same
version by number), only into visitors' conversations: user IDs
``visitor_…``, which the page makes at random and keeps in the visitor's
browser. Turning the page off or giving it a new link stops the old link
at once. Its turns are the ``page`` channel in the conversation audit.
"""

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from forge_agent_runtime import AgentNotFound
from forge_agent_runtime.document import AGENT_LIKE
from forge_agent_runtime.executor import ResolvedAgent
from forge_agent_runtime.server import CallAction, create_router
from pymongo.errors import PyMongoError

from forge_admin.chat_agents.page_settings import ChatPageSettings, page_settings
from forge_admin.chat_agents.runtime import NOT_SET_UP, executor_of
from forge_admin.chat_agents.store import PAGE_TOKEN_PATTERN, ChatAgentStore
from forge_admin.overview.recording import PAGE, set_channel

#: A visitor's user ID, which the page makes: ``visitor_`` and random letters and digits.
VISITOR = re.compile(r"^visitor_[A-Za-z0-9]{16,64}$")
#: What a page's chat does: read the agent, its visitor's conversations, run turns.
ACTIONS: frozenset[CallAction] = frozenset({"agent", "sessions", "run"})

NO_PAGE = "There's no chat page at this link: it was turned off, or given a new link"
NOT_ITS_AGENT = "This chat page talks only to its agent's latest published version"
NOT_A_VISITOR = "A chat page's conversations are its visitors': user IDs visitor_…"
NOT_ALLOWED = "A chat page only chats"
UNAVAILABLE = "The agents' database isn't answering; try again shortly"

PageToken = Annotated[str, Path(pattern=PAGE_TOKEN_PATTERN)]


class ChatPageInfo(ChatPageSettings):
    """
    What a chat page shows, and what its chat calls: its agent, and the
    page's settings (``page_settings``: its words, what it leaves out, its look).
    """

    #: The ``appName`` its chat sends: the agent's ID (its latest published version).
    app_name: str
    name: str
    description: str
    #: The published version it runs now.
    version: int
    #: Its agents (the chat agent and its sub-agents) by the names ADK calls
    #: them, as the canvas names them: the chat labels their replies, and
    #: shows a call handing one a request as asking it.
    agents: dict[str, str] = {}
    #: Its workflow tools by the names the model calls them, as the canvas
    #: names them: the chat shows a call of one as the run it starts, step by step.
    workflows: dict[str, str] = {}


def page_token(token: PageToken) -> str:
    """The link's token, checked for its shape: every route here has one."""
    return token


async def page_of(request: Request, token: str) -> dict[str, Any]:
    """
    :return: The agent whose chat page answers at the link.
    :raises HTTPException: 404 when none does, or the agent was never
        published; 503 when chat agents aren't set up or MongoDB isn't answering.
    """
    store: ChatAgentStore | None = getattr(request.app.state, "chat_agents", None)
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)
    try:
        record = await store.find_chat_page(token)
    except PyMongoError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
    if record is None or record.get("published_version") is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_PAGE)
    return record


async def authorize_page(
    request: Request,
    agent: ResolvedAgent,
    *,
    action: CallAction,
    user_id: str | None,
) -> None:
    """
    Whether a request through a chat page may call an agent: the page's own,
    at its latest published version, into a visitor's conversations.

    :raises HTTPException: 404 for another agent or version, or a link that
        doesn't answer; 403 for anything but chatting, or a user ID that
        isn't a visitor's.
    """
    record = await page_of(request, request.path_params["token"])
    if (
        agent.document.id != record["_id"]
        or agent.version != record["published_version"]
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_ITS_AGENT)
    if action not in ACTIONS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_ALLOWED)
    if user_id is not None and VISITOR.fullmatch(user_id) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_A_VISITOR)
    if action == "run":
        set_channel(PAGE)


router = APIRouter(prefix="/pages/{token}", dependencies=[Depends(page_token)])


@router.get("")
async def get_chat_page(request: Request, token: PageToken) -> ChatPageInfo:
    """
    What a chat page shows: its agent's name and description, as its latest
    published version has them, and the app name its chat calls.
    \f
    :raises HTTPException: 404 when no page answers at the link.
    """
    record = await page_of(request, token)
    try:
        agent = await executor_of(request).resolve(record["_id"])
    except AgentNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_PAGE) from error
    page = record.get("chat_page") or {}
    return ChatPageInfo(
        app_name=agent.document.id,
        name=agent.document.name,
        description=agent.document.description,
        version=int(record["published_version"]),
        **page_settings(page),
        agents={
            node.adk_name: node.name
            for node in agent.document.nodes
            if node.kind in AGENT_LIKE and node.adk_name
        },
        workflows={
            node.adk_name: node.name
            for node in agent.document.nodes
            if node.kind == "adk_workflow" and node.adk_name
        },
    )


router.include_router(create_router(executor_of, authorize=authorize_page))
