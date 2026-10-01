"""The assistant's conversations, in the Google ADK API server's protocol.

The web console's assistant (``@assistant-ui/react-google-adk``, direct mode)
talks to these routes as it would to ``adk api_server``: a user's sessions
are their conversation list, ``run_sse`` streams a turn as Server-Sent Events
of ADK events (text and reasoning as they are written, tool calls and their
results, confirmation, sign-in and input requests, agent transfers, state and
artifact changes), and artifacts are the files an agent saved.

Everything is the signed-in user's own: the ``user_id`` in a path or body
must be theirs. Unlike ADK's server, a request sets only the state the web
console sends (``CLIENT_STATE``): the model section's choices, and the page
the person is on (``agents/page_context.py``), which is checked and left out
when malformed. Who the person is comes from their sign-in
(``agents/person.py``). Binary data is standard base64 (see
``agents/wire.py``).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from forge_common.model_provider import ModelProviderAuthError

# StreamingMode has no public export; ADK's own API server imports it from here too.
from google.adk.agents.run_config import (
    RunConfig,
    StreamingMode,  # pyright: ignore[reportPrivateImportUsage]
)
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import State
from google.adk.sessions.base_session_service import GetSessionConfig
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from forge_admin.agents import language_models
from forge_admin.agents.page_context import PAGE_CONTEXT, page_context_from
from forge_admin.agents.person import PERSON
from forge_admin.agents.person_api import CALLER_BASE_URL
from forge_admin.agents.runtime import AgentRuntime
from forge_admin.agents.screens import STICKY, page_of
from forge_admin.agents.wire import sse, to_wire
from forge_admin.auth.access import CurrentUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

AppName = Annotated[
    str, Path(pattern=r"^[a-z][a-z0-9_]*$", max_length=64, examples=["forge"])
]
UserId = Annotated[str, Path(max_length=255)]
SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9._:-]+$", max_length=128)]
ArtifactName = Annotated[str, Path(min_length=1, max_length=512)]
Version = Annotated[int, Path(ge=0)]

# The models an agent's conversations may run on (agents/language_models.py).
MODELS = "/apps/{app_name}/models"
USER = "/apps/{app_name}/users/{user_id}"
SESSIONS = f"{USER}/sessions"
# What the agent has on a page, for the person (agents/screens.py).
CAPABILITIES = f"{USER}/capabilities"
SESSION = f"{SESSIONS}/{{session_id}}"
ARTIFACTS = f"{SESSION}/artifacts"
# Names may contain "/", so the routes below this one's must come first.
ARTIFACT = f"{ARTIFACTS}/{{artifact_name:path}}"

# Keep proxies from buffering or caching the stream.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# The session state a request may set: the model section's choices and the
# page the person is on. The rest is the server's or the agent's: app-wide
# (``app:``) state every user shares, state shared across their conversations
# (``user:``), and who the person is (``temp:person``), which each run looks
# up from their sign-in.
CLIENT_STATE = frozenset({"model", "thinking_level", PAGE_CONTEXT})


class CamelModel(BaseModel):
    # ADK's protocol is camelCase; snake_case is accepted too.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SessionCreate(CamelModel):
    # The conversation's starting state.
    state: dict[str, Any] | None = None


class RunRequest(CamelModel):
    app_name: str
    user_id: str
    session_id: str
    # The user's turn: text and files, or answers to the agent's function
    # calls (functionResponse parts), e.g. to a confirmation request.
    new_message: types.Content | None = None
    # Stream text and reasoning as they're written (partial events).
    streaming: bool = False
    # Session state to set before the turn: model, thinking_level and
    # page_context (CLIENT_STATE).
    state_delta: dict[str, Any] | None = None
    # To resume an invocation that paused for a long-running call.
    invocation_id: str | None = None


class CapabilitiesRequest(CamelModel):
    # The page the person is on, as a run sends it; null when they don't
    # share it.
    page_context: dict[str, Any] | None = None
    # One of their conversations: the toolsets it kept from earlier pages
    # count too.
    session_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9._:-]+$", max_length=128
    )
    # List each toolset's tools as well (a server's are asked for).
    tools: bool = False


class AgentModel(CamelModel):
    """A model a conversation may run on. Nothing about how it's reached."""

    # provider/model: what a run's ``model`` state takes.
    id: str
    provider: str
    provider_name: str
    # The provider's own id for it.
    model: str
    name: str
    # openai-responses, openai-completions, anthropic-messages or
    # google-generative-ai.
    api: str
    reasoning: bool
    input: list[str]
    context_window: int
    # The most tokens it writes in one reply.
    max_tokens: int
    # What a run's ``thinking_level`` may be, from least to most thinking:
    # off, minimal, low, medium, high, xhigh. Only off for a model that
    # doesn't reason.
    thinking_levels: list[str]


class AgentModels(CamelModel):
    # The model a conversation runs on until it chooses; null when the agent
    # doesn't let conversations choose.
    default_model: str | None
    models: list[AgentModel]


def get_runtime(request: Request) -> AgentRuntime:
    """FastAPI dependency: the agents and their services, from the lifespan."""
    runtime: AgentRuntime = request.app.state.agents
    return runtime


Runtime = Annotated[AgentRuntime, Depends(get_runtime)]


@dataclass(frozen=True)
class Conversations:
    """The caller's conversations with one agent."""

    runtime: AgentRuntime
    app_name: str
    user_id: str


def _runner(runtime: AgentRuntime, app_name: str) -> Runner:
    runner = runtime.runners.get(app_name)
    if runner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such agent")
    return runner


def _own(user: str, user_id: str) -> None:
    if user_id != user:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You can only use your own conversations"
        )


def _session_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return state
    refused = sorted(key for key in state if key not in CLIENT_STATE)
    if refused:
        shared = [key for key in refused if key.startswith(State.APP_PREFIX)]
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"State keys starting with {State.APP_PREFIX} are shared by every "
            "user and can't be set here"
            if shared
            else f"These state keys can't be set here: {', '.join(refused)}",
        )
    if PAGE_CONTEXT in state:
        return {**state, PAGE_CONTEXT: page_context_from(state[PAGE_CONTEXT])}
    return state


async def _person(runtime: AgentRuntime, user: str) -> dict[str, Any] | None:
    # Best effort: without it the agent only lacks their name and organizations.
    if runtime.people is None:
        return None
    try:
        person = await runtime.people(user)
    except Exception:
        logger.exception("Couldn't look up %s for the assistant", user)
        return None
    return person.model_dump(mode="json") if person else None


def conversations(
    app_name: AppName, user_id: UserId, user: CurrentUser, runtime: Runtime
) -> Conversations:
    """
    FastAPI dependency: the conversations a session or artifact route acts on.

    :raises HTTPException: 404 for an unknown agent; 403 when the path names
        someone else.
    """
    _runner(runtime, app_name)
    _own(user, user_id)
    return Conversations(runtime, app_name, user_id)


Mine = Annotated[Conversations, Depends(conversations)]


async def _existing(mine: Conversations, session_id: str) -> None:
    session = await mine.runtime.sessions.get_session(
        app_name=mine.app_name,
        user_id=mine.user_id,
        session_id=session_id,
        config=GetSessionConfig(num_recent_events=0),
    )
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation")


@router.post(CAPABILITIES)
async def capabilities(
    mine: Mine, body: CapabilitiesRequest | None = None
) -> dict[str, Any]:
    """
    What the agent has for the caller on a page, as its configuration for
    screens gives it: the screens the page matches, the toolsets it may use
    (with a conversation's, those it kept from earlier pages, marked
    ``fromEarlier``) and, when asked, their tools, each saying whether it
    asks first (``asksFirst``); and the prompts to suggest under a new
    conversation's composer. Every tool acts as the caller.
    \f
    :return: ``{"screens": [{"name", "title"}], "toolsets": [{"name",
        "title", "description", "fromEarlier", "tools"?}], "prompts":
        [{"title", "label", "prompt"}]}``.
    :raises HTTPException: 403 for someone else's; 404 for an unknown agent
        or conversation.
    """
    body = body or CapabilitiesRequest()
    screens = mine.runtime.screens.get(mine.app_name)
    if screens is None:
        return {"screens": [], "toolsets": [], "prompts": []}
    page = page_of({PAGE_CONTEXT: page_context_from(body.page_context)})
    sticky = None
    if body.session_id is not None:
        session = await mine.runtime.sessions.get_session(
            app_name=mine.app_name,
            user_id=mine.user_id,
            session_id=body.session_id,
            config=GetSessionConfig(num_recent_events=0),
        )
        if session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation")
        sticky = session.state.get(STICKY)
    return await screens.describe(
        user_id=mine.user_id, page=page, sticky=sticky, tools=body.tools
    )


@router.get(MODELS)
async def list_models(
    app_name: AppName, user: CurrentUser, runtime: Runtime
) -> AgentModels:
    """
    The models the agent's conversations may run on, its default first, as
    the admin API's model provider configuration gives them: for the web
    console's model section, which sends its choice as a run's ``model`` and
    ``thinking_level`` state. Any signed-in user may list them.
    \f
    :raises HTTPException: 404 for an unknown agent.
    """
    _runner(runtime, app_name)
    models = runtime.models.get(app_name)
    if models is None:
        return AgentModels(default_model=None, models=[])
    return AgentModels(
        default_model=models.default.ref,
        models=[
            AgentModel.model_validate(language_models.describe(model))
            for model in models.available
        ],
    )


@router.get(SESSIONS)
async def list_sessions(mine: Mine) -> JSONResponse:
    """
    The caller's conversations with the agent, most recently active first,
    without their events.
    """
    listed = await mine.runtime.sessions.list_sessions(
        app_name=mine.app_name, user_id=mine.user_id
    )
    newest = sorted(listed.sessions, key=lambda s: s.last_update_time, reverse=True)
    return JSONResponse([to_wire(session) for session in newest])


@router.post(SESSIONS, status_code=status.HTTP_201_CREATED)
async def create_session(mine: Mine, body: SessionCreate | None = None) -> JSONResponse:
    """
    Start a conversation, optionally with some state.
    \f
    :raises HTTPException: 422 when the state sets keys the console can't
        (``CLIENT_STATE``), e.g. app-wide ``app:`` ones.
    """
    state = _session_state(body.state if body else None)
    session = await mine.runtime.sessions.create_session(
        app_name=mine.app_name, user_id=mine.user_id, state=state
    )
    return JSONResponse(to_wire(session), status_code=status.HTTP_201_CREATED)


@router.get(SESSION)
async def get_session(mine: Mine, session_id: SessionId) -> JSONResponse:
    """
    A conversation with its events, which the assistant replays to show it.
    \f
    :raises HTTPException: 404 when it doesn't exist.
    """
    session = await mine.runtime.sessions.get_session(
        app_name=mine.app_name, user_id=mine.user_id, session_id=session_id
    )
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation")
    return JSONResponse(to_wire(session))


@router.delete(SESSION, status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(mine: Mine, session_id: SessionId) -> None:
    """Delete a conversation and the files saved in it."""
    app_name, user_id, artifacts = mine.app_name, mine.user_id, mine.runtime.artifacts
    for name in await artifacts.list_artifact_keys(
        app_name=app_name, user_id=user_id, session_id=session_id
    ):
        await artifacts.delete_artifact(
            app_name=app_name, user_id=user_id, session_id=session_id, filename=name
        )
    await mine.runtime.sessions.delete_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )


@router.get(ARTIFACTS)
async def list_artifacts(mine: Mine, session_id: SessionId) -> list[str]:
    """The names of the files agents saved in a conversation."""
    return await mine.runtime.artifacts.list_artifact_keys(
        app_name=mine.app_name, user_id=mine.user_id, session_id=session_id
    )


@router.get(f"{ARTIFACT}/versions")
async def list_artifact_versions(
    mine: Mine, session_id: SessionId, artifact_name: ArtifactName
) -> list[int]:
    """A file's versions, oldest first."""
    return await mine.runtime.artifacts.list_versions(
        app_name=mine.app_name,
        user_id=mine.user_id,
        session_id=session_id,
        filename=artifact_name,
    )


async def _artifact(
    mine: Conversations, session_id: str, name: str, version: int | None
) -> JSONResponse:
    part = await mine.runtime.artifacts.load_artifact(
        app_name=mine.app_name,
        user_id=mine.user_id,
        session_id=session_id,
        filename=name,
        version=version,
    )
    if part is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such artifact")
    return JSONResponse(to_wire(part))


@router.get(f"{ARTIFACT}/versions/{{version}}")
async def load_artifact_version(
    mine: Mine, session_id: SessionId, artifact_name: ArtifactName, version: Version
) -> JSONResponse:
    """
    One version of a file, as an ADK part: ``text``, ``inlineData`` (base64)
    or ``fileData`` (a URI).
    \f
    :raises HTTPException: 404 when it doesn't exist.
    """
    return await _artifact(mine, session_id, artifact_name, version)


@router.get(ARTIFACT)
async def load_artifact(
    mine: Mine,
    session_id: SessionId,
    artifact_name: ArtifactName,
    version: Annotated[int | None, Query(ge=0)] = None,
) -> JSONResponse:
    """
    A file, as an ADK part; the latest version unless ``version`` is given.
    \f
    :raises HTTPException: 404 when it doesn't exist.
    """
    return await _artifact(mine, session_id, artifact_name, version)


@router.delete(ARTIFACT, status_code=status.HTTP_204_NO_CONTENT)
async def delete_artifact(
    mine: Mine, session_id: SessionId, artifact_name: ArtifactName
) -> None:
    """Delete a file and all its versions."""
    await mine.runtime.artifacts.delete_artifact(
        app_name=mine.app_name,
        user_id=mine.user_id,
        session_id=session_id,
        filename=artifact_name,
    )


def _error_event(author: str, error: Exception) -> Event:
    # After the stream has started, a failure can only be told as an event;
    # the assistant shows its message as the reply, marked as failed.
    if isinstance(error, genai_errors.APIError):
        code = error.status or str(error.code)
        message = f"The model refused the request ({error.code} {error.status}): {error.message}"
    elif isinstance(error, ModelProviderAuthError):
        # Its message holds no credentials.
        code = "MODEL_AUTH_FAILED"
        message = f"The assistant couldn't sign in to the model's provider: {error}"
    elif type(error).__module__.startswith("litellm") and isinstance(
        getattr(error, "status_code", None), int
    ):
        # An OpenAI or Anthropic model's refusal, through LiteLLM.
        code = f"MODEL_ERROR_{error.status_code}"  # type: ignore[attr-defined]
        message = f"The model refused the request: {str(error)[:500]}"
    elif isinstance(error, ValueError):
        # ADK's word on a request it can't run, e.g. an answer to an
        # unknown call or a session changed by another run.
        code, message = "INVALID_REQUEST", str(error)
    else:
        code, message = "INTERNAL_ERROR", "The assistant ran into a problem. Try again."
    return Event(author=author, error_code=code, error_message=message)


async def _stream(
    runner: Runner, body: RunRequest, state_delta: dict[str, Any] | None
) -> AsyncIterator[str]:
    # A comment first, so the response starts even while the model thinks.
    yield ":ok\n\n"
    mode = StreamingMode.SSE if body.streaming else StreamingMode.NONE
    try:
        # aclosing ends the run when the client goes away (Stop).
        async with aclosing(
            runner.run_async(
                user_id=body.user_id,
                session_id=body.session_id,
                invocation_id=body.invocation_id,
                new_message=body.new_message,
                state_delta=state_delta,
                run_config=RunConfig(streaming_mode=mode),
            )
        ) as events:
            async for event in events:
                yield sse(event)
    except Exception as error:
        logger.exception(
            "Run of %s failed in session %s", runner.app_name, body.session_id
        )
        yield sse(_error_event(runner.app_name, error))


async def _refuse(author: str, reason: str) -> AsyncIterator[str]:
    yield sse(Event(author=author, error_code="NOT_CONFIGURED", error_message=reason))


@router.post(
    "/run_sse",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def run_sse(
    body: RunRequest, user: CurrentUser, runtime: Runtime, request: Request
) -> StreamingResponse:
    """
    Run the caller's turn in a conversation and stream the agent's events as
    Server-Sent Events: each message is ``data: <ADK event JSON>``. With
    ``streaming``, text and reasoning arrive as they're written (``partial``
    events), then as a whole. A failure after the stream starts arrives as an
    event with ``errorCode`` and ``errorMessage``.
    \f
    :raises HTTPException: 404 for an unknown agent or conversation; 403 when
        the body names someone else; 422 when it sets state the console
        can't (``CLIENT_STATE``).
    """
    runner = _runner(runtime, body.app_name)
    _own(user, body.user_id)
    # The agent's tools call this API at the address the person used.
    CALLER_BASE_URL.set(str(request.base_url))
    state_delta = _session_state(body.state_delta)
    mine = Conversations(runtime, body.app_name, body.user_id)
    await _existing(mine, body.session_id)
    if runtime.unavailable:
        events = _refuse(runner.app_name, runtime.unavailable)
    else:
        # Who they are, for this run only (``temp:``), from their sign-in.
        person = await _person(runtime, user)
        if person is not None:
            state_delta = {**(state_delta or {}), PERSON: person}
        events = _stream(runner, body, state_delta)
    return StreamingResponse(
        events, media_type="text/event-stream", headers=SSE_HEADERS
    )
