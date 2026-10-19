"""Chat agents over HTTP: ADK's run API (as ``adk api_server`` serves it), the
agent named by ``appName``. Needs ``forge-agent-runtime[server]``.

Routes (under wherever the router is mounted):

- ``GET /apps/{app}``: the agent: its name, version and the state it takes.
- ``GET|POST /apps/{app}/users/{user}/sessions``: a person's conversations.
- ``GET|DELETE /apps/{app}/users/{user}/sessions/{session}``: one of them.
- ``GET /apps/{app}/users/{user}/sessions/{session}/workflow-runs/{call}``:
  how the workflow a call in it started is going (``WorkflowRunner.progress``),
  for the chat to show its steps while the call waits.
- ``POST /run_sse``: run a turn, streaming ADK's events as Server-Sent
  Events (``data: <event JSON>``). A failure after the stream starts arrives
  as an event with ``errorCode`` and ``errorMessage``. While nothing goes out
  for a while (a tool waiting on a slow workflow), a ``: keepalive`` comment
  does, so proxies don't drop the connection as idle.
  ``customMetadata["forge:turn"]`` starts the invocation with ``running``
  and ends it with ``succeeded``, ``failed`` or ``cancelled`` and the same
  ``runId``. EOF without that terminal event is an interrupted transport,
  not a successful reply. Individual agents' final messages aren't the
  invocation's completion signal.

``{app}`` is ``ca_x``, ``ca_x@3`` or ``ca_x@draft``; a conversation belongs
to the agent, whichever version runs it. ``@assistant-ui/react-google-adk``
talks to these routes as they are::

    app = FastAPI()
    app.include_router(create_router(executor), prefix="/agents")

An app that decides who may call its agents passes these, each refusing by
raising ``HTTPException``:

- ``admit``: called first, with only the agent's ID and organization
  (:meth:`AgentExecutor.locate`), before its definition is read: nothing of
  another organization's agent, not even whether it builds, reaches a caller
  it doesn't let in. An agent that can't be built is answered without why
  (the server's log has it).
- ``authorize``: called once the agent is found, before anything is built or
  run, for what its definition says (who its rules let in, its version).
- ``owner``: whose conversations a call reaches. The ``userId`` a call names
  is only which of the caller's own it means: ``owner`` says the user ID
  they're kept under, from who the caller is (a sign-in, an API key, a
  visitor's token), and refuses someone else's. Every route reads and
  writes conversations under that ID, so a conversation's intake requests
  and workflow runs are the caller's too; responses show the ``userId`` the
  call named. Without it, the ``userId`` a call names is whose they are: for
  a server only its own people reach.

And one that decides how its turns reply passes ``streaming``: whether a
turn's replies stream, whatever its request asks (``streaming``), e.g. as a
chat page is set to.

Each turn starts only when the agent's chat agent node lets whoever it's for
in (:meth:`AgentExecutor.refusal`): 403 otherwise, saying what it needs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing, asynccontextmanager
from typing import Annotated, Any, Literal, Protocol

from anyio import CancelScope
from fastapi import APIRouter, FastAPI, HTTPException, Path, Request, status
from fastapi.params import Depends as DependsParam
from fastapi.responses import JSONResponse, StreamingResponse
from google.adk.events import Event
from google.genai import errors as genai_errors
from pydantic import Field

from forge_agent_runtime.access import AccessRefused
from forge_agent_runtime.build import BuildError, describe
from forge_agent_runtime.executor import (
    AgentExecutor,
    AgentNotFound,
    AgentRef,
    CamelModel,
    LocatedAgent,
    ResolvedAgent,
    RunRequest,
    StateRefused,
    check_state,
)
from forge_agent_runtime.leases import TurnBusy, TurnLease
from forge_agent_runtime.wire import sse, to_wire
from forge_common.adk.turns import TurnOutcome, persist_turn, turn_scope
from forge_common.model_provider import ModelProviderAuthError

log = logging.getLogger(__name__)

#: Keep proxies from buffering or caching the stream.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
#: Seconds a stream may go without sending anything before it says it's still
#: there: proxies drop connections quiet for longer than their idle timeout
#: (nginx's and AWS load balancers' are 60 s by default), and a turn can be
#: quiet much longer, e.g. a Workflow tool waiting up to its "Wait up to".
KEEPALIVE_SECONDS = 15.0
#: What it sends then: an SSE comment, which clients skip.
KEEPALIVE = ": keepalive\n\n"
#: Ends a turn's events in :func:`_stream`'s queue.
_END = object()
#: What a caller is told of an agent that can't be built: why is in the server's log.
CANT_BUILD = "The agent can't be built as it is. Whoever edits it can see why in its builder."

AppName = Annotated[str, Path(pattern=r"^[A-Za-z][A-Za-z0-9_-]*(@(draft|[0-9]+))?$", max_length=96)]
UserId = Annotated[str, Path(min_length=1, max_length=255)]
SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9._:-]+$", max_length=128)]
CallId = Annotated[str, Path(pattern=r"^[A-Za-z0-9._:-]+$", max_length=128)]


#: What a call does with an agent: read it (``agent``), its conversations
#: (``sessions``), run a turn (``run``); over A2A, read its card (``card``) or
#: send it a JSON-RPC request (``a2a``).
CallAction = Literal["agent", "sessions", "run", "card", "a2a"]


class AdmitCall(Protocol):
    """
    Decides whether a request may call an agent before anything of it is
    read: by its ID and organization alone. Refuses by raising
    ``HTTPException`` (401, 403; 404 for an agent the caller mayn't know of).
    """

    async def __call__(self, request: Request, agent: LocatedAgent, *, action: CallAction) -> None:
        """
        :param request: The request.
        :param agent: The agent called: its reference (version included) and organization.
        :param action: What the call does.
        """


class ConversationOwner(Protocol):
    """
    Whose conversations a call reaches, from who the caller is rather than
    from what it names: refuses someone else's by raising ``HTTPException`` (403).
    """

    async def __call__(self, request: Request, user_id: str) -> str:
        """
        :param request: The request, its caller identified.
        :param user_id: The user ID the call names (ADK's ``userId``).
        :return: The user ID the conversations it means are kept under.
        """
        ...


#: The longest user ID conversations are kept under: ADK's database sessions keep 128 characters.
MAX_USER_ID = 128


def owned_by(caller: str, user_id: str) -> str:
    """
    :param caller: Who's calling, as the server knows them (``apikey:<id>``).
    :param user_id: The user ID a call names: one of the caller's end users,
        or the caller themself.
    :return: The user ID the end user's conversations are kept under: the
        caller's, then the end user's, so two callers naming the same end
        user never share; the caller's own when they name themself. Too long
        for ADK's databases, the end user's part is a hash of it.
    """
    if user_id == caller:
        return caller
    owned = f"{caller}/{user_id}"
    if len(owned) > MAX_USER_ID:
        owned = f"{caller}/#{hashlib.sha256(user_id.encode()).hexdigest()}"
    return owned


class AuthorizeCall(Protocol):
    """
    Decides whether a request may do something with an agent, once it's
    found and before anything is built or run: refuses by raising
    ``HTTPException`` (401, 403).
    """

    async def __call__(
        self,
        request: Request,
        agent: ResolvedAgent,
        *,
        action: CallAction,
        user_id: str | None,
    ) -> None:
        """
        :param request: The request.
        :param agent: The agent called.
        :param action: What the call does.
        :param user_id: Whose conversations it reads or adds to (ADK's
            ``userId``), when it does.
        """


class TurnStreaming(Protocol):
    """
    Whether a turn's replies stream (partial events as the model writes) or
    arrive whole, decided for the request rather than by what it asks.
    """

    async def __call__(self, request: Request, agent: ResolvedAgent) -> bool | None:
        """
        :param request: The turn's request, once ``admit`` and ``authorize`` let it through.
        :param agent: The agent it runs.
        :return: True or False; None to do as the request asks.
        """
        ...


class SessionCreate(CamelModel):
    #: The conversation's starting state: what the agent's state schema declares.
    state: dict[str, Any] | None = None
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]+$", max_length=128)


def error_event(author: str, error: Exception) -> Event:
    """A failure after the stream started, as the event the chat shows as a failed reply."""
    if isinstance(error, genai_errors.APIError):
        code = error.status or str(error.code)
        message = f"The model refused the request ({error.code} {error.status}): {error.message}"
    elif isinstance(error, AccessRefused):
        code, message = "ACCESS_DENIED", str(error)
    elif isinstance(error, ModelProviderAuthError):
        code, message = "MODEL_AUTH_FAILED", f"The agent couldn't sign in to the model's provider: {error}"
    elif type(error).__module__.startswith("litellm") and isinstance(
        getattr(error, "status_code", None), int
    ):
        code = f"MODEL_ERROR_{error.status_code}"  # type: ignore[attr-defined]
        message = f"The model refused the request: {str(error)[:500]}"
    elif isinstance(error, ValueError):
        code, message = "INVALID_REQUEST", str(error)
    else:
        code, message = "INTERNAL_ERROR", "The agent ran into a problem. Try again."
    return Event(author=author, error_code=code, error_message=message)


async def _pump(
    events: AsyncGenerator[Event, None],
    queue: asyncio.Queue[Any],
    outcome: TurnOutcome,
    author: str,
    failure: Callable[[str, Exception], Event],
    persist: Callable[[TurnOutcome], Awaitable[None]] | None,
) -> None:
    """
    Runs a turn from start to end in one task, putting its events on the
    queue, then a failure if it fails, then :data:`_END`. One task, because
    ADK and the plugins keep context variables from one step of it to the next.
    """
    with turn_scope(outcome):
        try:
            async with aclosing(events):
                try:
                    async for event in events:
                        outcome.observe(event)
                        # Serialize before resuming the iterator: a serialization
                        # failure must reach the still-open invocation's audit.
                        queue.put_nowait(sse(event))
                except Exception as error:
                    public_error = failure(author, error)
                    outcome.fail(
                        public_error.error_code or "INTERNAL_ERROR",
                        public_error.error_message or "The agent ran into a problem. Try again.",
                        replace=not outcome.native_failure,
                    )
                    raise
                except BaseException:
                    outcome.cancel()
                    raise
            outcome.succeed()
        except asyncio.CancelledError:
            outcome.cancel()
            raise
        except Exception as error:
            log.exception("Turn %s of %s failed", outcome.run_id, author)
            event = failure(author, error)
            outcome.fail(
                event.error_code or "INTERNAL_ERROR",
                event.error_message or "The agent ran into a problem. Try again.",
                replace=not outcome.native_failure,
            )
            queue.put_nowait(sse(event))
        finally:
            try:
                if persist is not None:
                    await persist(outcome)
            except Exception:
                log.exception("Couldn't persist turn %s", outcome.run_id)
            finally:
                # Execution, audit and terminal history cleanup finish before
                # a terminal outcome is allowed onto the wire.
                queue.put_nowait(_END)


async def stream_events(
    events: AsyncGenerator[Event, None],
    author: str,
    *,
    keepalive: float | None = KEEPALIVE_SECONDS,
    failure: Callable[[str, Exception], Event] = error_event,
    persist: Callable[[TurnOutcome], Awaitable[None]] | None = None,
) -> AsyncGenerator[str, None]:
    """One turn: start, ADK events/heartbeats, then exactly one final outcome.

    HTTP EOF is not the completion signal. The terminal control event is
    emitted only after the execution task and its audit have settled.
    """
    outcome = TurnOutcome()
    yield ":ok\n\n"
    yield sse(outcome.event(author, started=True))
    queue: asyncio.Queue[Any] = asyncio.Queue()
    turn = asyncio.create_task(_pump(events, queue, outcome, author, failure, persist))
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), keepalive)
            except TimeoutError:
                yield KEEPALIVE
                continue
            if item is _END:
                yield sse(outcome.event(author))
                return
            yield item
    finally:
        if not turn.done():
            turn.cancel()
        # Starlette disconnects cancel an AnyIO scope. Joining the independent
        # producer under that same scope would abandon its audit cleanup.
        with CancelScope(shield=True):
            await asyncio.wait({turn})


async def _stream(
    executor: AgentExecutor,
    body: RunRequest,
    author: str,
    *,
    lease: TurnLease,
    keepalive: float | None = KEEPALIVE_SECONDS,
    resolved: ResolvedAgent | None = None,
) -> AsyncGenerator[str, None]:
    """
    One turn's stream, holding its conversation's lease (taken before the
    response started): given back as soon as the turn's outcome is kept, so
    a chat that sends its next message on the final event finds the
    conversation free.
    """
    # Keep the graph authorized before the response starts, including its
    # captured saved-agent dependencies, throughout this turn.
    events = executor.run(body, resolved=resolved, lease=lease)

    async def persist(outcome: TurnOutcome) -> None:
        try:
            # A turn whose conversation went to another writes nothing to it.
            if not lease.lost:
                await persist_turn(
                    outcome,
                    sessions=executor.sessions,
                    app_name=resolved.document.id
                    if resolved is not None
                    else AgentRef.parse(body.app_name).agent_id,
                    user_id=body.user_id,
                    session_id=body.session_id,
                    author=author,
                    invocation_id=body.invocation_id,
                )
        finally:
            await lease.release()

    try:
        async with aclosing(stream_events(events, author, keepalive=keepalive, persist=persist)) as stream:
            async for chunk in stream:
                yield chunk
    finally:
        # Given back already, unless the stream never ran its turn.
        await lease.release()


def create_router(
    executor: AgentExecutor | Callable[[Request], AgentExecutor],
    *,
    dependencies: Sequence[DependsParam] = (),
    admit: AdmitCall | None = None,
    authorize: AuthorizeCall | None = None,
    owner: ConversationOwner | None = None,
    streaming: TurnStreaming | None = None,
    keepalive: float | None = KEEPALIVE_SECONDS,
) -> APIRouter:
    """
    :param executor: Runs the agents; or finds the executor for a request,
        for an app that makes it when it starts.
    :param dependencies: Run before every route, e.g. one that authenticates the caller.
    :param admit: Decides by an agent's ID and organization whether a request
        may call it, before anything of it is read.
    :param authorize: Decides per agent whether a request may call it, once it's found.
    :param owner: Whose conversations a call reaches; the ``userId`` it names when None.
    :param streaming: Whether a turn's replies stream, whatever it asks; as it asks when None.
    :param keepalive: Seconds a turn's stream may be quiet before it sends a
        ``: keepalive`` comment; None never sends one.
    :return: The routes.
    """
    router = APIRouter(dependencies=list(dependencies))

    def of(request: Request) -> AgentExecutor:
        return executor if isinstance(executor, AgentExecutor) else executor(request)

    async def agent_of(
        request: Request, app_name: str, *, action: CallAction, user_id: str | None = None
    ) -> ResolvedAgent:
        executor_ = of(request)
        try:
            located = await executor_.locate(app_name)
            if admit is not None:
                await admit(request, located, action=action)
            resolved = await executor_.resolve(app_name)
        except AgentNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except BuildError as exc:
            log.warning("%s can't be built: %s", app_name, exc)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, CANT_BUILD) from exc
        if authorize is not None:
            await authorize(request, resolved, action=action, user_id=user_id)
        return resolved

    async def owner_of(request: Request, user_id: str) -> str:
        """:return: The user ID the caller's conversations a call names are kept under."""
        return user_id if owner is None else await owner(request, user_id)

    def shown(value: dict[str, Any], user_id: str) -> dict[str, Any]:
        """A conversation as the caller named it: with the ``userId`` the call did."""
        return {**value, "userId": user_id} if "userId" in value else value

    @router.get("/apps/{app_name}")
    async def get_agent(request: Request, app_name: AppName) -> dict[str, Any]:
        """The agent: its name, version, and the state its chat may send."""
        return dict(describe((await agent_of(request, app_name, action="agent")).document))

    @router.get("/apps/{app_name}/users/{user_id}/sessions")
    async def list_sessions(request: Request, app_name: AppName, user_id: UserId) -> JSONResponse:
        """A person's conversations with the agent, most recently active first, without their events."""
        agent = (await agent_of(request, app_name, action="sessions", user_id=user_id)).document.id
        kept = await owner_of(request, user_id)
        listed = await of(request).sessions.list_sessions(app_name=agent, user_id=kept)
        newest = sorted(listed.sessions, key=lambda s: s.last_update_time, reverse=True)
        return JSONResponse([shown(to_wire(session), user_id) for session in newest])

    @router.post("/apps/{app_name}/users/{user_id}/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(
        request: Request, app_name: AppName, user_id: UserId, body: SessionCreate | None = None
    ) -> JSONResponse:
        """Start a conversation, optionally with some of the state the agent declares."""
        resolved = await agent_of(request, app_name, action="sessions", user_id=user_id)
        kept = await owner_of(request, user_id)
        try:
            state = check_state(resolved.document, body.state if body else None)
        except StateRefused as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.problems) from exc
        session = await of(request).sessions.create_session(
            app_name=resolved.document.id,
            user_id=kept,
            state=state or None,
            session_id=body.session_id if body else None,
        )
        return JSONResponse(shown(to_wire(session), user_id), status_code=status.HTTP_201_CREATED)

    @router.get("/apps/{app_name}/users/{user_id}/sessions/{session_id}")
    async def get_session(
        request: Request, app_name: AppName, user_id: UserId, session_id: SessionId
    ) -> JSONResponse:
        """A conversation with its events, which a chat replays to show it."""
        agent = (await agent_of(request, app_name, action="sessions", user_id=user_id)).document.id
        session = await of(request).sessions.get_session(
            app_name=agent, user_id=await owner_of(request, user_id), session_id=session_id
        )
        if session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation")
        return JSONResponse(shown(to_wire(session), user_id))

    @router.delete(
        "/apps/{app_name}/users/{user_id}/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    async def delete_session(
        request: Request, app_name: AppName, user_id: UserId, session_id: SessionId
    ) -> None:
        """Delete a conversation and the files saved in it."""
        agent = (await agent_of(request, app_name, action="sessions", user_id=user_id)).document.id
        kept = await owner_of(request, user_id)
        artifacts = of(request).artifacts
        for name in await artifacts.list_artifact_keys(app_name=agent, user_id=kept, session_id=session_id):
            if name.startswith("user:"):
                continue
            await artifacts.delete_artifact(
                app_name=agent, user_id=kept, session_id=session_id, filename=name
            )
        await of(request).sessions.delete_session(app_name=agent, user_id=kept, session_id=session_id)

    @router.get("/apps/{app_name}/users/{user_id}/sessions/{session_id}/workflow-runs/{call_id}")
    async def get_workflow_run(
        request: Request, app_name: AppName, user_id: UserId, session_id: SessionId, call_id: CallId
    ) -> dict[str, Any]:
        """
        How the workflow a call in the conversation started is going: its
        steps so far, each with its status and times, for the chat to show
        while the call waits. Asked again as it goes, until it ends.
        \f
        :raises HTTPException: 404 when the call started no run in the
            person's conversation (or not yet), or there are no workflows here.
        """
        document = (await agent_of(request, app_name, action="sessions", user_id=user_id)).document
        kept = await owner_of(request, user_id)
        workflows = of(request).services.workflows
        found = (
            await workflows.progress(
                agent_id=document.id,
                user_id=kept,
                session_id=session_id,
                call_id=call_id,
                organization_id=document.organization_id,
            )
            if workflows is not None
            else None
        )
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That call started no workflow run")
        return found

    @router.post(
        "/run_sse",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def run_sse(request: Request, body: RunRequest) -> StreamingResponse:
        """
        Run a person's turn and stream the agent's events as Server-Sent Events.
        \f
        :raises HTTPException: 404 for an unknown agent or conversation; 409
            while another turn runs in the conversation; 422
            for state the agent doesn't take, or an agent that can't be built;
            403 when the agent's chat agent node doesn't let whoever it's for
            in; whatever ``admit``, ``authorize`` and ``owner`` raise.
        """
        # Who may call it is decided before it's built: building connects
        # to its MCP servers and fetches its specs.
        resolved = await agent_of(request, body.app_name, action="run", user_id=body.user_id)
        body = body.model_copy(update={"user_id": await owner_of(request, body.user_id)})
        streams = None if streaming is None else await streaming(request, resolved)
        if streams is not None:
            body = body.model_copy(update={"streaming": streams})
        try:
            executor_ = of(request)
            # Each turn, not only the first: a conversation handed to a
            # sub-agent goes straight to it, past the chat agent's own check.
            why = await executor_.refusal(resolved)
            if why is not None:
                raise AccessRefused(why)
            runner, resolved = await executor_.runner(body.app_name, resolved=resolved)
            check_state(resolved.document, body.state_delta)
        except AccessRefused as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except AgentNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except StateRefused as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.problems) from exc
        except BuildError as exc:
            log.warning("%s can't be built: %s", body.app_name, exc)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, CANT_BUILD) from exc
        session = await of(request).sessions.get_session(
            app_name=resolved.document.id, user_id=body.user_id, session_id=body.session_id
        )
        if session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation")
        # One turn at a time in a conversation: taken before the response
        # starts, so a second is refused (409) rather than run beside it.
        try:
            lease = await executor_.take_turn(body, resolved=resolved)
        except TurnBusy as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc), headers={"Retry-After": "2"}) from exc
        try:
            stream = _stream(
                executor_, body, runner.app_name, lease=lease, keepalive=keepalive, resolved=resolved
            )
            # A stream that never starts (its client went first) gives it back as it's dropped.
            lease.held_by(stream)
            return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)
        except BaseException:
            await lease.release()
            raise

    return router


def create_app(executor: AgentExecutor, *, prefix: str = "", title: str = "Forge agent runtime") -> FastAPI:
    """
    :return: A server for ``executor``'s agents: the run API at ``prefix``,
        and ``GET /healthz``. Closing the app closes the executor.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await executor.close()

    app = FastAPI(title=title, lifespan=lifespan)
    app.include_router(create_router(executor), prefix=prefix)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


__all__ = [
    "KEEPALIVE",
    "KEEPALIVE_SECONDS",
    "SSE_HEADERS",
    "CANT_BUILD",
    "MAX_USER_ID",
    "AdmitCall",
    "AgentRef",
    "AuthorizeCall",
    "ConversationOwner",
    "CallAction",
    "TurnStreaming",
    "create_app",
    "create_router",
    "error_event",
    "owned_by",
    "stream_events",
]
