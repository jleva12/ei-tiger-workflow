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
  ``/runtime`` serves them (``forge_agent_runtime.server``). Its turns'
  replies stream or arrive whole as the page is set to (``streaming``),
  whatever its chat asks.

- ``POST /runtime/pages/{token}/visitors``: a visitor, and the token saying
  so (``forge_admin.chat_agents.visitors``): the page asks as it opens,
  sending the token it has, if any, to go on as the same visitor.

The link is the credential: the only way into an organization's agents
without an API key or a sign-in. It answers only for its agent's latest
published version (``ca_x``, or the same version by number), and only into
its visitors' conversations: each call sends the visitor's token
(``X-Forge-Visitor``), and names the user ID it gives, or is refused.
Turning the page off or giving it a new link stops the old link at once. Its
turns are the ``page`` channel in the conversation audit.

A page's visitors are nobody the organization knows, so they hold none of
its agent permissions: an agent whose chat agent node has an access rule
has no page that answers (403, saying so), and its other agents with one
(sub-agents, saved agents) tell visitors what they need instead
(``forge_admin.chat_agents.access``).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from forge_agent_runtime import AgentNotFound
from forge_agent_runtime.access import SETTING as ACCESS
from forge_agent_runtime.access import RuleError, node_rule
from forge_agent_runtime.document import AGENT_LIKE
from forge_agent_runtime.executor import AgentExecutor, LocatedAgent, ResolvedAgent
from forge_agent_runtime.models import model_window
from forge_agent_runtime.server import CallAction, create_router
from pydantic import BaseModel
from pymongo.errors import PyMongoError

from forge_admin.auth.runtime_access import require_organization
from forge_admin.chat_agents.access import ANONYMOUS, set_agent_user
from forge_admin.chat_agents.page_settings import ChatPageSettings, page_settings
from forge_admin.chat_agents.runtime import NOT_SET_UP, executor_of
from forge_admin.chat_agents.store import PAGE_TOKEN_PATTERN, ChatAgentStore
from forge_admin.chat_agents.visitors import VISITOR_HEADER, issue, verify
from forge_admin.overview.recording import PAGE, set_channel

#: What a page's chat does: read the agent, its visitor's conversations, run turns.
ACTIONS: frozenset[CallAction] = frozenset({"agent", "sessions", "run"})

NO_PAGE = "There's no chat page at this link: it was turned off, or given a new link"
NOT_ITS_AGENT = "This chat page talks only to its agent's latest published version"
NOT_A_VISITOR = (
    "A chat page's conversations are each its own visitor's: "
    "use the user ID its token gives"
)
NO_VISITOR = (
    "A chat page's visitor sends the token it was given (X-Forge-Visitor): "
    "POST …/visitors for one"
)
NOT_ALLOWED = "A chat page only chats"
UNAVAILABLE = "The agents' database isn't answering; try again shortly"
LIMITED = (
    "This agent is only for those its organization lets use it, so its chat "
    "page can't be used: ask whoever shared it"
)

PageToken = Annotated[str, Path(pattern=PAGE_TOKEN_PATTERN)]


class ModelWindow(BaseModel):
    """
    The model an agent starts a conversation on, and how much it reads:
    chats measure their context meter against it until the agent server has
    counted the conversation.
    """

    #: ``provider/model``.
    id: str
    name: str
    context_window: int
    #: What's kept of the window for the reply.
    max_tokens: int


def entry_model(executor: AgentExecutor, agent: ResolvedAgent) -> ModelWindow:
    """:return: The model an agent's chat agent node starts on, and its window."""
    return ModelWindow(**model_window(executor.models, agent.document.entry.config))


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
    #: Its agents (the chat agent, its sub-agents and intake agents) by the
    #: names ADK calls them, as the canvas names them: the chat labels their replies, and
    #: shows a call handing one a request as asking it.
    agents: dict[str, str] = {}
    #: Its workflow tools by the names the model calls them, as the canvas
    #: names them: the chat shows a call of one as the run it starts, step by step.
    workflows: dict[str, str] = {}
    #: The model its agent starts on: the context meter's window before the first reply.
    model: ModelWindow | None = None


def agent_names(document: Any) -> dict[str, str]:
    """
    A chat agent's agents (the chat agent, its sub-agents and intake agents)
    by the names ADK calls them, as the canvas names them: chats label their
    replies, and show a call handing one a request as asking it.
    """
    return {
        node.adk_name: node.name
        for node in document.nodes
        if node.kind in AGENT_LIKE and node.adk_name
    }


def workflow_names(document: Any) -> dict[str, str]:
    """
    A chat agent's workflow tools by the names the model calls them, as the
    canvas names them: chats show a call of one as the run it starts.
    """
    return {
        node.adk_name: node.name
        for node in document.nodes
        if node.kind == "adk_workflow" and node.adk_name
    }


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
    await require_organization(request, record.get("organization_id"))
    return record


async def check_open(
    request: Request, record: dict[str, Any], agent: ResolvedAgent
) -> None:
    """
    Check that a page's agent may be started by anyone: a page's visitors
    are nobody the organization knows. Its other agents' rules are checked
    as they run, for visitors holding nothing.

    :raises HTTPException: 403 when its chat agent node has an access rule,
        or is held to one (its record's, from before nodes had rules).
    """
    document = agent.document
    entry = document.entry
    try:
        rule = node_rule(entry.config, document.required_rules.get(entry.id))
    except RuleError:
        rule = entry.config.get(ACCESS)
    if rule is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, LIMITED)
    set_agent_user(ANONYMOUS)


async def admit_page(
    request: Request,
    agent: LocatedAgent,
    *,
    action: CallAction,
) -> None:
    """
    Whether a request through a chat page may call an agent, by its ID
    alone: the page's own, never its draft, and only to chat.

    :raises HTTPException: 404 for another agent or its draft, or a link
        that doesn't answer; 403 for anything but chatting.
    """
    record = await page_of(request, request.path_params["token"])
    if agent.ref.agent_id != record["_id"] or agent.ref.version == "draft":
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_ITS_AGENT)
    if action not in ACTIONS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_ALLOWED)
    request.state.chat_page = record


async def authorize_page(
    request: Request,
    agent: ResolvedAgent,
    *,
    action: CallAction,
    user_id: str | None,
) -> None:
    """
    Whether a request through a chat page may call an agent, once found:
    the page's own, at its latest published version, which anyone may start.

    :raises HTTPException: 404 for another version; 403 when its chat agent
        node has an access rule.
    """
    record = getattr(request.state, "chat_page", None) or await page_of(
        request, request.path_params["token"]
    )
    if (
        agent.document.id != record["_id"]
        or agent.version != record["published_version"]
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_ITS_AGENT)
    await check_open(request, record, agent)
    if action == "run":
        set_channel(PAGE)


async def page_streaming(request: Request, agent: ResolvedAgent) -> bool:
    """
    :return: Whether a turn through a chat page streams its replies: as the
        page is set to, whatever its chat asks.
    """
    record = getattr(request.state, "chat_page", None) or await page_of(
        request, request.path_params["token"]
    )
    return bool(page_settings(record.get("chat_page"))["streaming"])


async def visitor_of(request: Request, user_id: str) -> str:
    """
    Whose conversations a call through a chat page reaches (its ``owner``):
    the visitor its token says, who names themself.

    :raises HTTPException: 401 without a token the API signed for the
        page's agent; 403 for a user ID that isn't the token's.
    """
    record = getattr(request.state, "chat_page", None) or await page_of(
        request, request.path_params["token"]
    )
    visitor = verify(
        request.app.state.settings,
        str(record["_id"]),
        request.headers.get(VISITOR_HEADER),
    )
    if visitor is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, NO_VISITOR)
    if user_id != visitor:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_A_VISITOR)
    return visitor


router = APIRouter(prefix="/pages/{token}", dependencies=[Depends(page_token)])


@router.get("")
async def get_chat_page(request: Request, token: PageToken) -> ChatPageInfo:
    """
    What a chat page shows: its agent's name and description, as its latest
    published version has them, and the app name its chat calls.
    \f
    :raises HTTPException: 404 when no page answers at the link; 403 when
        its agent is limited to some of the organization's agent permissions.
    """
    record = await page_of(request, token)
    try:
        agent = await executor_of(request).resolve(record["_id"])
    except AgentNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_PAGE) from error
    await check_open(request, record, agent)
    page = record.get("chat_page") or {}
    return ChatPageInfo(
        app_name=agent.document.id,
        name=agent.document.name,
        description=agent.document.description,
        version=int(record["published_version"]),
        **page_settings(page),
        agents=agent_names(agent.document),
        workflows=workflow_names(agent.document),
        model=entry_model(executor_of(request), agent),
    )


class VisitorInfo(BaseModel):
    """A chat page's visitor: their user ID, and the token its chat sends (``X-Forge-Visitor``)."""

    visitor_id: str
    token: str
    #: When the token stops working, in seconds since the epoch: the page asks again before.
    expires_at: int


@router.post("/visitors")
async def visit_chat_page(request: Request, token: PageToken) -> VisitorInfo:
    """
    A visitor of the chat page: the one its token says, given a new token,
    when the page sends one that's still good; else a new visitor.
    \f
    :raises HTTPException: 404 when no page answers at the link.
    """
    record = await page_of(request, token)
    settings = request.app.state.settings
    agent_id = str(record["_id"])
    known = verify(settings, agent_id, request.headers.get(VISITOR_HEADER))
    visitor = issue(settings, agent_id, known)
    return VisitorInfo(
        visitor_id=visitor.id, token=visitor.token, expires_at=visitor.expires_at
    )


router.include_router(
    create_router(
        executor_of,
        admit=admit_page,
        authorize=authorize_page,
        owner=visitor_of,
        streaming=page_streaming,
    )
)
