"""Chat agents over HTTP: ADK's run API (as ``adk api_server`` serves it), the
agent named by ``appName``. Needs ``forge-agent-runtime[server]``.

Routes (under wherever the router is mounted):

- ``GET /apps/{app}``: the agent: its name, version and the state it takes.
- ``GET|POST /apps/{app}/users/{user}/sessions``: a person's conversations.
- ``GET|DELETE /apps/{app}/users/{user}/sessions/{session}``: one of them.
- ``POST /run_sse``: run a turn, streaming ADK's events as Server-Sent
  Events (``data: <event JSON>``). A failure after the stream starts arrives
  as an event with ``errorCode`` and ``errorMessage``.

``{app}`` is ``ca_x``, ``ca_x@3`` or ``ca_x@draft``; a conversation belongs
to the agent, whichever version runs it. ``@assistant-ui/react-google-adk``
talks to these routes as they are::

    app = FastAPI()
    app.include_router(create_router(executor), prefix="/agents")

An app that decides per agent who may call it passes ``authorize``: it's
called once the agent is found, before anything is built or run, and refuses
by raising ``HTTPException``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Path, Request, status
from fastapi.params import Depends as DependsParam
from fastapi.responses import JSONResponse, StreamingResponse
from google.adk.events import Event
from google.genai import errors as genai_errors
from pydantic import Field

from forge_agent_runtime.build import BuildError, describe
from forge_agent_runtime.executor import (
    AgentExecutor,
    AgentNotFound,
    AgentRef,
    CamelModel,
    ResolvedAgent,
    RunRequest,
    StateRefused,
    check_state,
)
from forge_agent_runtime.wire import sse, to_wire
from forge_common.model_provider import ModelProviderAuthError

log = logging.getLogger(__name__)

#: Keep proxies from buffering or caching the stream.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

AppName = Annotated[str, Path(pattern=r"^[A-Za-z][A-Za-z0-9_-]*(@(draft|[0-9]+))?$", max_length=96)]
UserId = Annotated[str, Path(min_length=1, max_length=255)]
SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9._:-]+$", max_length=128)]


#: What a call does with an agent: read it (``agent``), its conversations
#: (``sessions``), run a turn (``run``); over A2A, read its card (``card``) or
#: send it a JSON-RPC request (``a2a``).
CallAction = Literal["agent", "sessions", "run", "card", "a2a"]


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


class SessionCreate(CamelModel):
    #: The conversation's starting state: what the agent's state schema declares.
    state: dict[str, Any] | None = None
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]+$", max_length=128)


def error_event(author: str, error: Exception) -> Event:
    """A failure after the stream started, as the event the chat shows as a failed reply."""
    if isinstance(error, genai_errors.APIError):
        code = error.status or str(error.code)
        message = f"The model refused the request ({error.code} {error.status}): {error.message}"
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


async def _stream(executor: AgentExecutor, body: RunRequest, author: str) -> AsyncIterator[str]:
    # A comment first, so the response starts even while the model thinks.
    yield ":ok\n\n"
    try:
        async for event in executor.run(body):
            yield sse(event)
    except Exception as error:
        log.exception("Run of %s failed in session %s", body.app_name, body.session_id)
        yield sse(error_event(author, error))


def create_router(
    executor: AgentExecutor | Callable[[Request], AgentExecutor],
    *,
    dependencies: Sequence[DependsParam] = (),
    authorize: AuthorizeCall | None = None,
) -> APIRouter:
    """
    :param executor: Runs the agents; or finds the executor for a request,
        for an app that makes it when it starts (``request.app.state``).
    :param dependencies: Run before every route, e.g. one that authenticates the caller.
    :param authorize: Decides per agent whether a request may call it.
    :return: The routes.
    """
    router = APIRouter(dependencies=list(dependencies))

    def of(request: Request) -> AgentExecutor:
        return executor if isinstance(executor, AgentExecutor) else executor(request)

    async def agent_of(
        request: Request, app_name: str, *, action: CallAction, user_id: str | None = None
    ) -> ResolvedAgent:
        try:
            resolved = await of(request).resolve(app_name)
        except AgentNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except BuildError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"The agent can't be built: {exc}"
            ) from exc
        if authorize is not None:
            await authorize(request, resolved, action=action, user_id=user_id)
        return resolved

    @router.get("/apps/{app_name}")
    async def get_agent(request: Request, app_name: AppName) -> dict[str, Any]:
        """The agent: its name, version, and the state its chat may send."""
        return dict(describe((await agent_of(request, app_name, action="agent")).document))

    @router.get("/apps/{app_name}/users/{user_id}/sessions")
    async def list_sessions(request: Request, app_name: AppName, user_id: UserId) -> JSONResponse:
        """A person's conversations with the agent, most recently active first, without their events."""
        agent = (await agent_of(request, app_name, action="sessions", user_id=user_id)).document.id
        listed = await of(request).sessions.list_sessions(app_name=agent, user_id=user_id)
        newest = sorted(listed.sessions, key=lambda s: s.last_update_time, reverse=True)
        return JSONResponse([to_wire(session) for session in newest])

    @router.post("/apps/{app_name}/users/{user_id}/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(
        request: Request, app_name: AppName, user_id: UserId, body: SessionCreate | None = None
    ) -> JSONResponse:
        """Start a conversation, optionally with some of the state the agent declares."""
        resolved = await agent_of(request, app_name, action="sessions", user_id=user_id)
        try:
            state = check_state(resolved.document, body.state if body else None)
        except StateRefused as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.problems) from exc
        session = await of(request).sessions.create_session(
            app_name=resolved.document.id,
            user_id=user_id,
            state=state or None,
            session_id=body.session_id if body else None,
        )
        return JSONResponse(to_wire(session), status_code=status.HTTP_201_CREATED)

    @router.get("/apps/{app_name}/users/{user_id}/sessions/{session_id}")
    async def get_session(
        request: Request, app_name: AppName, user_id: UserId, session_id: SessionId
    ) -> JSONResponse:
        """A conversation with its events, which a chat replays to show it."""
        agent = (await agent_of(request, app_name, action="sessions", user_id=user_id)).document.id
        session = await of(request).sessions.get_session(
            app_name=agent, user_id=user_id, session_id=session_id
        )
        if session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation")
        return JSONResponse(to_wire(session))

    @router.delete(
        "/apps/{app_name}/users/{user_id}/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    async def delete_session(
        request: Request, app_name: AppName, user_id: UserId, session_id: SessionId
    ) -> None:
        """Delete a conversation and the files saved in it."""
        agent = (await agent_of(request, app_name, action="sessions", user_id=user_id)).document.id
        artifacts = of(request).artifacts
        for name in await artifacts.list_artifact_keys(
            app_name=agent, user_id=user_id, session_id=session_id
        ):
            await artifacts.delete_artifact(
                app_name=agent, user_id=user_id, session_id=session_id, filename=name
            )
        await of(request).sessions.delete_session(app_name=agent, user_id=user_id, session_id=session_id)

    @router.post(
        "/run_sse",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def run_sse(request: Request, body: RunRequest) -> StreamingResponse:
        """
        Run a person's turn and stream the agent's events as Server-Sent Events.
        \f
        :raises HTTPException: 404 for an unknown agent or conversation; 422
            for state the agent doesn't take, or an agent that can't be built;
            whatever ``authorize`` raises.
        """
        # Who may call it is decided before it's built: building connects
        # to its MCP servers and fetches its specs.
        resolved = await agent_of(request, body.app_name, action="run", user_id=body.user_id)
        try:
            executor_ = of(request)
            runner, resolved = await executor_.runner(body.app_name, resolved=resolved)
            check_state(resolved.document, body.state_delta)
        except AgentNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except StateRefused as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.problems) from exc
        except BuildError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"The agent can't be built: {exc}"
            ) from exc
        session = await of(request).sessions.get_session(
            app_name=resolved.document.id, user_id=body.user_id, session_id=body.session_id
        )
        if session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation")
        return StreamingResponse(
            _stream(executor_, body, runner.app_name), media_type="text/event-stream", headers=SSE_HEADERS
        )

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
    "SSE_HEADERS",
    "AgentRef",
    "AuthorizeCall",
    "CallAction",
    "create_app",
    "create_router",
    "error_event",
]
