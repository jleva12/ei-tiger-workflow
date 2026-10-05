"""Running chat agents by ID: each request names the agent it's for, and the
executor builds that agent from its definition (once, then from a cache) and
runs the turn.

An agent is named as ADK names an app (``appName``):

- ``ca_x``: its latest published version;
- ``ca_x@3``: version 3;
- ``ca_x@draft``: the draft being edited.

Where definitions come from is an :class:`AgentSource`: a file or a folder of
them here (:class:`FileAgentSource`, :class:`DirectoryAgentSource`), or a
database in a hosted deployment. Conversations (sessions) are kept per agent,
not per version, so a conversation carries on when a new version is
published.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.artifacts import BaseArtifactService, InMemoryArtifactService
from google.adk.events.event import Event
from google.adk.memory import BaseMemoryService, InMemoryMemoryService
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai import types
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from forge_agent_runtime.build import build_app
from forge_agent_runtime.document import ChatAgentDocument, DocumentError, load_document
from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.templating import REQUEST_KEY
from forge_common.adk.models import ProviderModels

log = logging.getLogger(__name__)

Version = int | Literal["draft"] | None
#: The state the model picker sends with every message.
FIXED_STATE: dict[str, Any] = {
    "model": {"type": "string"},
    "thinking_level": {"enum": ["", "off", "minimal", "low", "medium", "high", "xhigh"]},
}


class AgentNotFound(LookupError):
    """No agent (or no such version of one) by that name."""


class StateRefused(ValueError):
    """State a request may not set, or set to a value its agent doesn't declare."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class AgentRef:
    """An agent and which of its versions: ``ca_x``, ``ca_x@3``, ``ca_x@draft``."""

    agent_id: str
    version: Version = None

    @classmethod
    def parse(cls, text: str) -> AgentRef:
        agent_id, _, version = text.partition("@")
        if not agent_id:
            raise AgentNotFound(f"{text!r} doesn't name an agent.")
        if not version:
            return cls(agent_id)
        if version == "draft":
            return cls(agent_id, "draft")
        if version.isdigit() and int(version) > 0:
            return cls(agent_id, int(version))
        raise AgentNotFound(f"{text!r}: a version is a number, or draft.")

    def __str__(self) -> str:
        return self.agent_id if self.version is None else f"{self.agent_id}@{self.version}"


@dataclass(frozen=True)
class ResolvedAgent:
    """
    :ivar document: Its definition.
    :ivar version: Which version it is.
    :ivar cache_key: Changes whenever the definition does (a draft's revision),
        so a built agent is used only while it's current.
    """

    document: ChatAgentDocument
    version: Version
    cache_key: str


class AgentSource(Protocol):
    """Where agents' definitions come from."""

    async def get(self, ref: AgentRef) -> ResolvedAgent:
        """:raises AgentNotFound: There's no such agent or version."""
        ...


class FileAgentSource:
    """
    One agent from its exported JSON file (with the saved agents it uses, in
    its ``dependencies``), read again whenever the file changes.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._loaded: tuple[float, ChatAgentDocument] | None = None

    def document(self) -> ChatAgentDocument:
        stamp = self.path.stat().st_mtime
        if self._loaded is None or self._loaded[0] != stamp:
            self._loaded = (stamp, load_document(self.path))
        return self._loaded[1]

    async def get(self, ref: AgentRef) -> ResolvedAgent:
        doc = self.document()
        if ref.agent_id != doc.id:
            raise AgentNotFound(f"This runtime serves {doc.id}, not {ref.agent_id}.")
        if isinstance(ref.version, int) and isinstance(doc.version, int) and ref.version != doc.version:
            raise AgentNotFound(f"This runtime serves {doc.id}@{doc.version}, not @{ref.version}.")
        return ResolvedAgent(doc, doc.version, f"{doc.id}@{doc.version}:{self._loaded and self._loaded[0]}")


class DocumentAgentSource:
    """One agent, given as its document (a pin to another version isn't it)."""

    def __init__(self, doc: ChatAgentDocument) -> None:
        self.doc = doc

    async def get(self, ref: AgentRef) -> ResolvedAgent:
        doc = self.doc
        if ref.agent_id != doc.id:
            raise AgentNotFound(f"This runtime serves {doc.id}, not {ref.agent_id}.")
        if isinstance(ref.version, int) and isinstance(doc.version, int) and ref.version != doc.version:
            raise AgentNotFound(f"This runtime serves {doc.id}@{doc.version}, not @{ref.version}.")
        return ResolvedAgent(doc, doc.version, f"{doc.id}@{doc.version}")


class DirectoryAgentSource:
    """Every agent in a folder of exported JSON files; a pin picks among an agent's versions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _documents(self) -> list[ChatAgentDocument]:
        docs = []
        for file in sorted(self.path.glob("*.json")):
            try:
                docs.append(load_document(file))
            except DocumentError as exc:
                log.warning("Skipped %s: %s", file, exc)
        return docs

    async def get(self, ref: AgentRef) -> ResolvedAgent:
        versions = [doc for doc in self._documents() if doc.id == ref.agent_id]
        if ref.version is not None:
            versions = [doc for doc in versions if doc.version == ref.version]
        else:
            published = [doc for doc in versions if isinstance(doc.version, int)]
            if published:
                versions = [
                    max(published, key=lambda doc: doc.version if isinstance(doc.version, int) else 0)
                ]
        if not versions:
            raise AgentNotFound(f"There's no agent {ref}.")
        doc = versions[-1]
        return ResolvedAgent(doc, doc.version, f"{doc.id}@{doc.version}:{doc.updated_at}")


class CamelModel(BaseModel):
    # ADK's run API is camelCase; snake_case is accepted too.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RunRequest(CamelModel):
    """ADK's run request (``POST /run_sse``), its ``appName`` naming the agent."""

    app_name: str
    user_id: str
    session_id: str
    #: The person's turn: text and files, or answers to the agent's tool calls.
    new_message: types.Content | None = None
    #: Text and reasoning arrive as they're written (partial events).
    streaming: bool = False
    #: State to set before the turn: model, thinking_level and what the agent declares.
    state_delta: dict[str, Any] | None = None
    #: Resumes an invocation that paused on a long-running call.
    invocation_id: str | None = None


def check_state(doc: ChatAgentDocument, delta: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    :return: The state a request sets, when its agent allows it: the model
        picker's two keys and the fields its state schema declares, each of
        its declared type.
    :raises StateRefused: Anything else.
    """
    if not delta:
        return {}
    declared = doc.state_schema.get("properties")
    properties = {**FIXED_STATE, **(declared if isinstance(declared, dict) else {})}
    unknown = sorted(key for key in delta if key not in properties)
    if unknown:
        raise StateRefused([f"{doc.name} doesn't take these state keys: {', '.join(unknown)}"])
    validator = Draft202012Validator({"type": "object", "properties": properties})
    problems = [
        f"{'.'.join(str(p) for p in error.absolute_path) or 'stateDelta'}: {error.message}"
        for error in validator.iter_errors(dict(delta))
    ]
    if problems:
        raise StateRefused(problems)
    return dict(delta)


class RunObserver(Protocol):
    """
    Watches the agents an executor runs, e.g. to record what they use: its
    plugins go on each agent as it's built (once a version), and each turn
    runs inside its invocation.
    """

    def plugins(self, agent: ResolvedAgent) -> Sequence[BasePlugin]:
        """:return: ADK plugins for the agent's app."""
        ...

    def invocation(self, agent: ResolvedAgent, request: RunRequest) -> AbstractAsyncContextManager[object]:
        """:return: What a turn of the agent runs inside; it sees the turn fail or be cut off."""
        ...


class AgentExecutor:
    """
    Builds agents from their definitions and runs their turns.

    :param source: Where agents' definitions come from.
    :param models: The models agents run on.
    :param sessions: Where conversations are kept; in memory by default.
    :param artifacts: Where files agents make are kept; in memory by default.
    :param memory: Long-term memory, for agents with a Memory tool; in memory by default.
    :param services: What building and running agents may use. Saved agents
        are found in ``source`` (their latest published version) unless it
        says otherwise.
    :param cache_size: How many built agents are kept.
    :param observer: Watches the agents run, e.g. to record what they use.
    :param streaming: Whether replies stream (partial events as the model
        writes) or arrive whole; as each request asks (``streaming``) by default.
    """

    def __init__(
        self,
        source: AgentSource,
        *,
        models: ProviderModels,
        sessions: BaseSessionService | None = None,
        artifacts: BaseArtifactService | None = None,
        memory: BaseMemoryService | None = None,
        services: RuntimeServices | None = None,
        http: httpx.AsyncClient | None = None,
        cache_size: int = 64,
        observer: RunObserver | None = None,
        streaming: bool | None = None,
    ) -> None:
        self.source = source
        self.observer = observer
        self.streaming = streaming
        self.models = models
        self.sessions = sessions or InMemorySessionService()
        self.artifacts = artifacts or InMemoryArtifactService()
        self.memory = memory or InMemoryMemoryService()
        services = services or RuntimeServices()
        if services.resolve_agent is None:
            services = services.but(resolve_agent=self._latest)
        self.services = services
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient()
        self.cache_size = cache_size
        self._runners: OrderedDict[str, Runner] = OrderedDict()
        self._building: dict[str, asyncio.Lock] = {}

    async def _latest(self, agent_id: str) -> ChatAgentDocument:
        return (await self.source.get(AgentRef(agent_id))).document

    async def resolve(self, app_name: str) -> ResolvedAgent:
        """:raises AgentNotFound: There's no such agent or version."""
        return await self.source.get(AgentRef.parse(app_name))

    async def runner(self, app_name: str) -> tuple[Runner, ResolvedAgent]:
        """
        :return: A runner for the agent (built now if it isn't cached), and its definition.
        :raises AgentNotFound: There's no such agent or version.
        :raises BuildError: It can't be built as it is.
        """
        resolved = await self.resolve(app_name)
        key = resolved.cache_key
        runner = self._runners.get(key)
        if runner is None:
            lock = self._building.setdefault(key, asyncio.Lock())
            async with lock:
                runner = self._runners.get(key)
                if runner is None:
                    app = await build_app(
                        resolved.document,
                        models=self.models,
                        services=self.services,
                        http=self.http,
                        app_name=resolved.document.id,
                        plugins=self.observer.plugins(resolved) if self.observer is not None else (),
                    )
                    runner = Runner(
                        app=app,
                        session_service=self.sessions,
                        artifact_service=self.artifacts,
                        memory_service=self.memory,
                    )
                    self._runners[key] = runner
                    await self._evict()
            self._building.pop(key, None)
        self._runners.move_to_end(key)
        return runner, resolved

    async def _evict(self) -> None:
        while len(self._runners) > self.cache_size:
            _, runner = self._runners.popitem(last=False)
            try:
                await runner.close()
            except Exception:
                log.warning("Closing an evicted agent failed", exc_info=True)

    async def run(self, request: RunRequest) -> AsyncIterator[Event]:
        """
        Runs one turn: the request's state is checked against what its agent
        declares, and the request itself is kept for the turn (``temp:request``)
        for instructions to read.

        :raises AgentNotFound: There's no such agent or version.
        :raises BuildError: It can't be built as it is.
        :raises StateRefused: The request sets state its agent doesn't take.
        """
        runner, resolved = await self.runner(request.app_name)
        delta = check_state(resolved.document, request.state_delta)
        delta[REQUEST_KEY] = {
            "appName": request.app_name,
            "userId": request.user_id,
            "sessionId": request.session_id,
            "newMessage": json.loads(request.new_message.model_dump_json(exclude_none=True))
            if request.new_message
            else None,
            "streaming": request.streaming,
        }
        log.info(
            "Running %s (version %s) for %s in session %s",
            resolved.document.id,
            resolved.version,
            request.user_id,
            request.session_id,
        )
        streams = request.streaming if self.streaming is None else self.streaming
        mode = StreamingMode.SSE if streams else StreamingMode.NONE
        watched = (
            self.observer.invocation(resolved, request)
            if self.observer is not None
            else contextlib.nullcontext()
        )
        async with (
            watched,
            aclosing(
                runner.run_async(
                    user_id=request.user_id,
                    session_id=request.session_id,
                    invocation_id=request.invocation_id,
                    new_message=request.new_message,
                    state_delta=delta,
                    run_config=RunConfig(streaming_mode=mode),
                )
            ) as events,
        ):
            async for event in events:
                yield event

    async def close(self) -> None:
        """Closes every built agent (their toolsets' connections) and the HTTP client, if made here."""
        while self._runners:
            _, runner = self._runners.popitem()
            try:
                await runner.close()
            except Exception:
                log.warning("Closing an agent failed", exc_info=True)
        if self._owns_http:
            await self.http.aclose()
