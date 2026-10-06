"""Chat agents over Google's A2A protocol (JSON-RPC), beside ADK's run API
(:mod:`.server`). Needs ``forge-agent-runtime[a2a]``.

Every agent is an A2A agent of its own. Routes (under wherever the router is
mounted):

- ``GET /{app}/.well-known/agent-card.json``: its agent card: name,
  description and version, what it can do (its skills: it, and the agents it
  hands off to), where to call it, and how callers sign in when they must.
- ``POST /{app}``: JSON-RPC. A2A 1.0's methods (``SendMessage``,
  ``SendStreamingMessage``, ``GetTask``, ``ListTasks``, ``CancelTask``,
  ``SubscribeToTask``) and 0.3's (``message/send``, ``message/stream``,
  ``tasks/get``, ``tasks/cancel``, ``tasks/resubscribe``) on the same URL;
  the card describes both.

``{app}`` names the agent as the run API's ``appName`` does: ``ca_x`` (its
latest published version), ``ca_x@3`` or ``ca_x@draft``. A router for one
agent (``agent=``, as :class:`~forge_agent_runtime.AgentServer` mounts it)
serves it without the name: ``POST /`` and ``GET /.well-known/agent-card.json``.

A turn runs as a chat turn does (:meth:`AgentExecutor.run`): the same agent,
its state checked, ``{{ request.* }}`` filled in, observers told. An A2A
conversation (``contextId``) is the agent's ADK session of that ID, kept with
the chat's; its user is the caller when the server knows who's calling, else
``A2A_USER_<contextId>``, as ADK's own A2A server names them. The state the
agent's input schema declares comes as the message's ``metadata.state`` (or
the request's), as a chat sends ``stateDelta``. Each message is a task: it
completes with the reply as its artifact, fails saying why, or waits
(``input-required``) on a tool that asks for confirmation until the next
message on it answers.

Tasks are kept per agent and caller (:func:`task_store`), in memory or a
database. Listing them (``ListTasks``) needs a caller the server knows (a
sign-in or an API key): on an open server it would show everyone's.

Other kinds of agent can be served at the same URLs (:class:`A2aService`):
names with their prefix (a hosted runtime's workflows, ``ag_…``) go to their
service's card and executor, sharing the task store; the rest are chat agents.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
import uuid
import warnings
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from a2a.auth.user import User
from a2a.helpers import new_task
from a2a.server.agent_execution import AgentExecutor as A2aAgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import Event, EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.server.routes import DefaultServerCallContextBuilder
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from a2a.server.tasks import InMemoryTaskStore, TaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    APIKeySecurityScheme,
    Artifact,
    HTTPAuthSecurityScheme,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    Part,
    Role,
    SecurityRequirement,
    SecurityScheme,
    SendMessageRequest,
    StringList,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)
from a2a.utils.constants import PROTOCOL_VERSION_0_3, PROTOCOL_VERSION_1_0
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.params import Depends as DependsParam
from fastapi.responses import JSONResponse, Response
from google.adk.a2a.converters.event_converter import convert_event_to_a2a_events
from google.adk.a2a.converters.part_converter import convert_a2a_part_to_genai_part
from google.adk.a2a.converters.request_converter import convert_a2a_request_to_agent_run_request
from google.protobuf.json_format import MessageToDict

from forge_agent_runtime.build import BuildError
from forge_agent_runtime.document import HANDED_TO, ChatAgentDocument
from forge_agent_runtime.executor import AgentExecutor, AgentNotFound, RunRequest, StateRefused
from forge_agent_runtime.server import AppName, AuthorizeCall, CallAction, error_event

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

CARD_PATH = "/.well-known/agent-card.json"
#: Where the agent a call is for is in the call context's state: its ID, and the app name it was called by.
AGENT_KEY = "forge_agent"
APP_KEY = "forge_app"
#: Conversations are ADK sessions, whose IDs the chat's run API takes too; a task
#: table keeps 36 characters of one (a UUID's).
CONTEXT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,36}$")
#: The state a message sets: the message's metadata, or the request's, under this key.
STATE_KEY = "state"
#: Where the HTTP request is in the call context's state.
REQUEST_KEY = "forge_request"
#: The table a database keeps tasks in.
TASKS_TABLE = "a2a_tasks"

#: Who a request comes from, when the server knows: a user ID, or "api-key".
Caller = Callable[[Request], str | None]
Executor = AgentExecutor | Callable[[Request], AgentExecutor]
Tasks = TaskStore | Callable[[Request], TaskStore]


class _Caller(User):
    """A caller the server knows, as A2A's call context holds one."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._name


def known_caller(request: Request) -> str | None:
    """:return: The caller a sign-in found (``request.state.user_id``), or None for anyone."""
    user = getattr(request.state, "user_id", None)
    return str(user) if user else None


def owner_of(context: ServerCallContext) -> str:
    """
    Whose a task is: the agent's, and the caller's when the server knows them,
    so one agent's tasks are never found through another.
    """
    agent = str(context.state.get(AGENT_KEY) or "")
    user = context.user.user_name if context.user.is_authenticated else ""
    owner = f"{agent}/{user}"
    if len(owner) > 255:
        owner = f"{agent}/{hashlib.sha256(user.encode()).hexdigest()}"
    return owner


def task_store(where: str | AsyncEngine = "memory", *, table: str = TASKS_TABLE) -> TaskStore:
    """
    Where A2A tasks are kept, each with its agent and caller (:func:`owner_of`).

    :param where: ``memory``; a database URL (made with its async driver, as
        conversations' are); or an engine, whose database has the table (a
        URL's makes it).
    :param table: The table, in a database.
    """
    if where == "memory":
        return InMemoryTaskStore(owner_resolver=owner_of)
    from a2a.server.tasks.database_task_store import DatabaseTaskStore

    if isinstance(where, str):
        from sqlalchemy.ext.asyncio import create_async_engine

        from forge_agent_runtime.app import database_url

        engine, create = create_async_engine(database_url(where)), True
    else:
        engine, create = where, False
    store = DatabaseTaskStore(engine, create_table=create, owner_resolver=owner_of)
    store.task_model = _task_model(table)
    return store


@functools.cache
def _task_model(table: str) -> type:
    """
    The table's model, declared once: a2a-sdk declares one each time a store
    is made, and a second of the same name is refused.
    """
    from a2a.server.models import TaskModel, create_task_model

    if table == "tasks":
        return TaskModel
    with warnings.catch_warnings():
        # Each is declared as a TaskModel before it's renamed, which SQLAlchemy notes.
        warnings.filterwarnings("ignore", message=".*same class name and module name", category=Warning)
        return create_task_model(table)


class _TasksOf(TaskStore):
    """The task store of the app a call came to, for an app that makes it as it starts."""

    def __init__(self, find: Callable[[Request], TaskStore]) -> None:
        self.find = find

    def _of(self, context: ServerCallContext) -> TaskStore:
        return self.find(context.state[REQUEST_KEY])

    async def save(self, task: Task, context: ServerCallContext) -> None:
        await self._of(context).save(task, context)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        return await self._of(context).get(task_id, context)

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        return await self._of(context).list(params, context)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        await self._of(context).delete(task_id, context)


def bearer_security(
    description: str = "A key or token: Authorization: Bearer <it>",
) -> dict[str, SecurityScheme]:
    """The security schemes of an agent its callers send a bearer token to."""
    return {
        "bearer": SecurityScheme(
            http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer", description=description)
        )
    }


def api_key_security(
    *,
    bearer: str = "An API key: Authorization: Bearer <key>",
    header: str = "An API key",
) -> dict[str, SecurityScheme]:
    """
    The security schemes of an agent asking for an API key: a bearer token,
    or X-API-Key.

    :param bearer: What to send as the bearer token.
    :param header: What to send as X-API-Key.
    """
    return {
        **bearer_security(bearer),
        "apiKey": SecurityScheme(
            api_key_security_scheme=APIKeySecurityScheme(
                location="header", name="X-API-Key", description=header
            )
        ),
    }


def agent_card(
    doc: ChatAgentDocument,
    url: str,
    *,
    security: Mapping[str, SecurityScheme] | None = None,
    provider: str | None = None,
) -> AgentCard:
    """
    An agent's A2A card, from its document alone: nothing is built or
    connected to, and nothing its instructions or tools say is shown.

    :param url: Where its JSON-RPC endpoint is.
    :param security: How callers sign in, by scheme name; any one of them will do.
    :param provider: Who runs it, for the card's provider.
    """
    entry = doc.entry
    described = doc.description or str(entry.config.get("description") or "")
    skills = [
        AgentSkill(
            id=entry.adk_name or doc.id,
            name=doc.name,
            description=described or f"Chat with {doc.name}.",
            tags=["chat"],
        )
    ]
    for node in doc.nodes:
        if node.kind not in HANDED_TO:
            continue
        skills.append(
            AgentSkill(
                id=node.adk_name or node.id,
                name=node.name,
                description=str(node.config.get("description") or "")
                or f"{node.name}, an agent {doc.name} works with.",
                tags=["agent", str(node.config.get("mode") or "saved")],
            )
        )
    card = AgentCard(
        name=doc.name,
        description=described or f"{doc.name}, a Forge chat agent.",
        version=str(doc.version) if doc.version is not None else "1",
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version=PROTOCOL_VERSION_1_0),
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version=PROTOCOL_VERSION_0_3),
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=skills,
    )
    if provider:
        card.provider.organization = provider
    for name, scheme in (security or {}).items():
        card.security_schemes[name].CopyFrom(scheme)
        card.security_requirements.append(SecurityRequirement(schemes={name: StringList()}))
    return card


class _Turn:
    """What a task's turn amounts to: its state, and the message to finish it with."""

    #: The states a turn can end in, the stronger first.
    RANK = (
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_AUTH_REQUIRED,
        TaskState.TASK_STATE_INPUT_REQUIRED,
    )

    def __init__(self) -> None:
        self.state = TaskState.TASK_STATE_WORKING
        self.message: Message | None = None

    def saw(self, event: Any) -> None:
        if not isinstance(event, TaskStatusUpdateEvent):
            return
        state = event.status.state
        message = event.status.message if event.status.HasField("message") else None
        if state in self.RANK:
            if self.state not in self.RANK or self.RANK.index(state) <= self.RANK.index(self.state):
                self.state, self.message = state, message
        elif (
            state == self.state == TaskState.TASK_STATE_WORKING and message is not None and len(message.parts)
        ):
            self.message = message


class ForgeA2aExecutor(A2aAgentExecutor):
    """
    Runs A2A tasks on Forge's chat agents: the agent the call context names
    (:data:`APP_KEY`), each message as a turn of :meth:`AgentExecutor.run`,
    ADK's events as A2A's (ADK's own converters).

    :param executor: Runs the agents; or finds the executor for a call, by its context.
    """

    def __init__(self, executor: AgentExecutor | Callable[[ServerCallContext], AgentExecutor]) -> None:
        self._executor = executor

    def executor_of(self, context: ServerCallContext) -> AgentExecutor:
        return self._executor if isinstance(self._executor, AgentExecutor) else self._executor(context)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message, task_id, context_id = context.message, context.task_id, context.context_id
        if message is None or not task_id or not context_id:
            raise ValueError("An A2A task needs a message, a task ID and a context ID")
        if context.current_task is None:
            # A2A 1.0: a new task's first event is the task itself.
            await event_queue.enqueue_event(
                new_task(task_id, context_id, TaskState.TASK_STATE_SUBMITTED, history=[message])
            )
        updater = TaskUpdater(event_queue, task_id, context_id)
        call = context.call_context
        app_name = str(call.state.get(APP_KEY) or "")
        executor = self.executor_of(call)
        try:
            resolved = await executor.resolve(app_name)
            run = convert_a2a_request_to_agent_run_request(context, convert_a2a_part_to_genai_part)
            state = _state_of(context)
            user_id, session_id = str(run.user_id), str(run.session_id)
            if (
                await executor.sessions.get_session(
                    app_name=resolved.document.id, user_id=user_id, session_id=session_id
                )
                is None
            ):
                await executor.sessions.create_session(
                    app_name=resolved.document.id, user_id=user_id, session_id=session_id
                )
            await updater.start_work()
            # What ADK's converters read of an invocation: the app, user and session the events are of.
            invocation: Any = SimpleNamespace(
                app_name=resolved.document.id,
                user_id=user_id,
                session=SimpleNamespace(id=session_id),
                invocation_id=None,
                branch=None,
            )
            turn = _Turn()
            request = RunRequest(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
                new_message=run.new_message,
                streaming=False,
                state_delta=state,
            )
            async for event in executor.run(request):
                if event.partial:
                    # Streamed pieces: the whole event follows them.
                    continue
                for a2a_event in convert_event_to_a2a_events(event, invocation, task_id, context_id):
                    turn.saw(a2a_event)
                    await event_queue.enqueue_event(a2a_event)
        except StateRefused as error:
            await updater.reject(_agent_message(str(error)))
            return
        except (AgentNotFound, BuildError) as error:
            await updater.failed(_agent_message(str(error)))
            return
        except Exception as error:
            log.exception("A2A task %s of %s failed", task_id, app_name)
            await updater.failed(_agent_message(error_event(app_name, error).error_message or str(error)))
            return
        await _finish(updater, turn, event_queue, task_id, context_id)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("Cancelling needs the task's ID")
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


async def _finish(
    updater: TaskUpdater, turn: _Turn, event_queue: EventQueue, task_id: str, context_id: str
) -> None:
    """Ends the task as the turn did: its reply as the artifact, or failed or waiting with why."""
    if turn.state != TaskState.TASK_STATE_WORKING:
        await updater.update_status(turn.state, message=turn.message)
        return
    if turn.message is not None and len(turn.message.parts):
        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                artifact=Artifact(artifact_id=f"{task_id}-reply", name="reply", parts=turn.message.parts),
                last_chunk=True,
            )
        )
    await updater.complete()


def agent_message(text: str, *parts: Part) -> Message:
    """:return: A message from the agent: the text, and any other parts."""
    return Message(message_id=str(uuid.uuid4()), role=Role.ROLE_AGENT, parts=[Part(text=text), *parts])


_agent_message = agent_message


def _state_of(context: RequestContext) -> dict[str, Any] | None:
    """The state a message sets: its own ``metadata.state``, else the request's."""
    for metadata in (
        MessageToDict(context.message.metadata)
        if context.message and context.message.HasField("metadata")
        else {},
        context.metadata,
    ):
        state = metadata.get(STATE_KEY) if isinstance(metadata, Mapping) else None
        if state is not None:
            if not isinstance(state, Mapping):
                raise StateRefused(
                    ["metadata.state is an object: the state the agent's input schema declares"]
                )
            return dict(state)
    return None


def _check_context(params: SendMessageRequest) -> None:
    """:raises InvalidParamsError: A contextId a conversation can't have (before its task is saved)."""
    context_id = params.message.context_id
    if context_id and not CONTEXT_ID.fullmatch(context_id):
        raise InvalidParamsError(message="A contextId is letters, digits and ._:-, 36 at most (a UUID).")


class ForgeRequestHandler(DefaultRequestHandler):
    """
    A2A's request handler, as Forge serves agents: a contextId is one a
    conversation can have, and only callers the server knows list tasks.
    """

    async def on_message_send(self, params: SendMessageRequest, context: ServerCallContext) -> Message | Task:
        _check_context(params)
        return await super().on_message_send(params, context)

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event]:
        _check_context(params)
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def on_list_tasks(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        if not context.user.is_authenticated:
            raise UnsupportedOperationError(
                "Listing tasks needs a caller this server knows (an API key or a sign-in): "
                "get a task by its ID instead"
            )
        return await super().on_list_tasks(params, context)


@dataclass(frozen=True)
class A2aService:
    """
    Another kind of agent, served at the same URLs as the chat agents by its
    names' prefix: its card, its executor, and how a name is found.

    :ivar prefix: The names it serves start with this: ``ag_``.
    :ivar find: Finds the agent a name means, once the request may do what
        it asks (``card`` or ``a2a``): the ID its tasks are kept under. Refuses
        by raising ``HTTPException`` (404, 401, 403).
    :ivar card: The agent's card: ``(request, name, url)``.
    :ivar executor: Runs its tasks.
    :ivar handler: The request handler class its calls go through.
    """

    prefix: str
    find: Callable[[Request, str, CallAction], Awaitable[str]]
    card: Callable[[Request, str, str], Awaitable[AgentCard]]
    executor: A2aAgentExecutor
    handler: type[ForgeRequestHandler] = ForgeRequestHandler


#: The card the handlers check calls against; each agent's own is served apart.
_GENERIC_CARD = AgentCard(
    name="Forge agents",
    capabilities=AgentCapabilities(streaming=True, push_notifications=False),
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
)


class _CallContextBuilder(DefaultServerCallContextBuilder):
    """The call context: the agent the route found, and the caller when the server knows them."""

    def __init__(self, caller: Caller) -> None:
        self.caller = caller

    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        context.state[AGENT_KEY] = request.state.forge_a2a_agent
        context.state[APP_KEY] = request.state.forge_a2a_app
        context.state[REQUEST_KEY] = request
        who = self.caller(request)
        if who:
            context.user = _Caller(who)
        return context


def create_a2a_router(
    executor: Executor,
    *,
    path: str = "",
    agent: str | None = None,
    tasks: Tasks | None = None,
    dependencies: Sequence[DependsParam] = (),
    card_dependencies: Sequence[DependsParam] | None = None,
    caller: Caller = known_caller,
    security: Mapping[str, SecurityScheme] | None = None,
    base_url: str | None = None,
    provider: str | None = None,
    authorize: AuthorizeCall | None = None,
    services: Sequence[A2aService] = (),
) -> APIRouter:
    """
    :param executor: Runs the agents; or finds the executor for a request
        (``request.app.state``), for an app that makes it when it starts.
    :param path: Where the agents are, under wherever the router is mounted:
        ``/a2a`` serves ``POST /a2a/{app}``.
    :param agent: The one agent it serves (``ca_x``), at ``path`` itself rather
        than by name; its card is also at ``/.well-known/agent-card.json``,
        where A2A clients look first. Mount it at the root of the app.
    :param tasks: Where tasks are kept (:func:`task_store`), or finds the
        store for a request; in memory by default.
    :param dependencies: Run before each JSON-RPC call, e.g. one that authenticates the caller.
    :param card_dependencies: Run before a card is served; ``dependencies``
        by default. ``()`` serves it to anyone, as clients read it to learn
        how to sign in.
    :param caller: Who the request is from, when the server knows (after
        ``dependencies``): a sign-in's user ID by default.
    :param security: How callers sign in, for the cards (:func:`api_key_security`, :func:`bearer_security`).
    :param base_url: The server's public address (``https://agents.example.com``),
        for the cards; the request's own by default.
    :param provider: Who runs the agents, for the cards.
    :param authorize: Decides per agent whether a request may read its card
        (``card``) or call it (``a2a``), once it's found; refuses by raising
        ``HTTPException``, before any task is touched.
    :param services: Other kinds of agent served by name at the same URLs
        (not with ``agent``), sharing the task store.
    :return: The routes.
    """
    store = task_store() if tasks is None else tasks if isinstance(tasks, TaskStore) else _TasksOf(tasks)
    path = path.rstrip("/")

    def of(request: Request) -> AgentExecutor:
        return executor if isinstance(executor, AgentExecutor) else executor(request)

    handler = ForgeRequestHandler(
        agent_executor=ForgeA2aExecutor(lambda context: of(context.state[REQUEST_KEY])),
        task_store=store,
        # What the handler checks calls against; each agent's own card is served below.
        agent_card=_GENERIC_CARD,
    )
    builder = _CallContextBuilder(caller)
    dispatcher = JsonRpcDispatcher(
        request_handler=handler,
        context_builder=builder,
        enable_v0_3_compat=True,
    )
    others = [
        (
            service,
            JsonRpcDispatcher(
                request_handler=service.handler(
                    agent_executor=service.executor, task_store=store, agent_card=_GENERIC_CARD
                ),
                context_builder=builder,
                enable_v0_3_compat=True,
            ),
        )
        for service in services
    ]

    def service_of(app_name: str) -> tuple[A2aService, JsonRpcDispatcher] | None:
        return next(((s, d) for s, d in others if app_name.startswith(s.prefix)), None)

    router = APIRouter()

    async def found(request: Request, app_name: str, action: CallAction) -> ChatAgentDocument:
        try:
            resolved = await of(request).resolve(app_name)
        except AgentNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        if authorize is not None:
            await authorize(request, resolved, action=action, user_id=None)
        return resolved.document

    async def card(request: Request, name: str, rpc_route: str, **params: str) -> Response:
        url = request.url_for(rpc_route, **params)
        address = f"{base_url.rstrip('/')}{url.path}" if base_url else str(url)
        other = service_of(name)
        if other is not None:
            await other[0].find(request, name, "card")
            made = await other[0].card(request, name, address)
        else:
            doc = await found(request, name, "card")
            made = agent_card(doc, address, security=security, provider=provider)
        return JSONResponse(agent_card_to_dict(made), headers={"Cache-Control": "no-cache"})

    async def rpc(request: Request, app_name: str) -> Response:
        other = service_of(app_name)
        if other is not None:
            request.state.forge_a2a_agent = await other[0].find(request, app_name, "a2a")
            request.state.forge_a2a_app = app_name
            return await other[1].handle_requests(request)
        doc = await found(request, app_name, "a2a")
        request.state.forge_a2a_agent = doc.id
        request.state.forge_a2a_app = app_name
        return await dispatcher.handle_requests(request)

    rpc_dependencies = list(dependencies)
    card_deps = list(dependencies if card_dependencies is None else card_dependencies)
    card_summary = "The agent's A2A card: what it does, where to call it, how to sign in."
    rpc_summary = "A2A JSON-RPC (1.0's SendMessage, … and 0.3's message/send, …): tasks for the agent."
    if agent is None:

        @router.get(f"{path}/{{app_name}}{CARD_PATH}", dependencies=card_deps, summary=card_summary)
        async def a2a_card(request: Request, app_name: AppName) -> Response:
            return await card(request, app_name, "a2a_rpc", app_name=app_name)

        @router.post(f"{path}/{{app_name}}", dependencies=rpc_dependencies, summary=rpc_summary)
        async def a2a_rpc(request: Request, app_name: AppName) -> Response:
            return await rpc(request, app_name)

    else:
        served = agent

        async def one_card(request: Request) -> Response:
            return await card(request, served, "a2a_agent_rpc")

        for card_path in dict.fromkeys([f"{path}{CARD_PATH}", CARD_PATH]):
            router.add_api_route(
                card_path,
                one_card,
                methods=["GET"],
                dependencies=card_deps,
                summary=card_summary,
                name="a2a_agent_card",
            )

        @router.post(path or "/", dependencies=rpc_dependencies, summary=rpc_summary)
        async def a2a_agent_rpc(request: Request) -> Response:
            return await rpc(request, served)

    return router


__all__ = [
    "AGENT_KEY",
    "APP_KEY",
    "CARD_PATH",
    "REQUEST_KEY",
    "TASKS_TABLE",
    "A2aService",
    "ForgeA2aExecutor",
    "ForgeRequestHandler",
    "agent_message",
    "agent_card",
    "api_key_security",
    "bearer_security",
    "create_a2a_router",
    "known_caller",
    "owner_of",
    "task_store",
]
