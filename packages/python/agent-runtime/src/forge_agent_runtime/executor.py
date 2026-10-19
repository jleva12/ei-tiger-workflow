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
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import httpx
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.artifacts import BaseArtifactService, InMemoryArtifactService
from google.adk.events.event import Event
from google.adk.memory import BaseMemoryService, InMemoryMemoryService
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.adk.sessions.base_session_service import GetSessionConfig
from google.genai import types
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from forge_agent_runtime.access import AccessRefused, Guarded, RuleError, node_rule, refused
from forge_agent_runtime.build import BuildError, build_app
from forge_agent_runtime.document import ChatAgentDocument, DocumentError, load_document, parse_document
from forge_agent_runtime.leases import (
    LEASE_SECONDS,
    TURN_WAIT,
    MemoryTurnLeases,
    TurnKey,
    TurnLease,
    TurnLeaseLost,
    TurnLeasePlugin,
    TurnLeases,
    holding,
)
from forge_agent_runtime.refs import AgentNotFound, AgentRef, Version
from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.tasks import without_cancelled_tasks
from forge_agent_runtime.templating import REQUEST_KEY
from forge_agent_runtime.tools import http_client
from forge_common.adk.follow_ups import MOST
from forge_common.adk.follow_ups import REQUEST_KEY as FOLLOW_UPS_REQUEST
from forge_common.adk.models import ProviderModels
from forge_common.adk.titles import TITLE_KEY, with_title
from forge_common.adk.turns import TurnOutcomePlugin, current_turn, persist_turn, turn_scope
from forge_common.patterns import schema_errors

log = logging.getLogger(__name__)

#: The state a chat may send with every message: the model picker's choices,
#: and whether to suggest follow-up questions after the reply (how many).
FIXED_STATE: dict[str, Any] = {
    "model": {"type": "string"},
    "thinking_level": {"enum": ["", "off", "minimal", "low", "medium", "high", "xhigh"]},
    FOLLOW_UPS_REQUEST: {
        "anyOf": [
            {"type": "boolean"},
            {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1, "maximum": MOST}}},
        ]
    },
}


_running: ContextVar[ResolvedAgent | None] = ContextVar("forge_running_agent", default=None)


def running_agent() -> ResolvedAgent | None:
    """:return: The agent whose turn runs here, as it was resolved (which
    version): what its tools act for; None outside a turn."""
    return _running.get()


class StateRefused(ValueError):
    """State a request may not set, or set to a value its agent doesn't declare."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


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
    #: The external definitions captured when resolving this request. None
    #: means the source has not yet been snapshotted by the executor.
    dependencies: tuple[ResolvedAgent, ...] | None = None
    #: Every saved agent in the captured graph, including exported definitions.
    #: Authorization must check these agents, rather than traverse latest again.
    used_agent_ids: frozenset[str] = frozenset()

    async def resolve_dependency(self, agent_id: str) -> ChatAgentDocument:
        for agent in self.dependencies or ():
            if agent.document.id == agent_id:
                return agent.document
        raise AgentNotFound(f"The resolved graph has no saved agent {agent_id}.")


@dataclass(frozen=True)
class LocatedAgent:
    """
    The agent a call names, found by its ID before anything of it is read:
    whose it is, so a server lets the caller in or refuses them before its
    definition is parsed or built (:meth:`AgentExecutor.locate`).
    """

    ref: AgentRef
    organization_id: str | None


class AgentSource(Protocol):
    """
    Where agents' definitions come from. One that can tell an agent's
    organization without reading its definition has ``locate(ref) ->
    LocatedAgent`` too (raising :class:`AgentNotFound` for none), which
    servers ask before anything else.
    """

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
        its declared type. The schema's patterns run on forge-common's
        bounded engine (``schema_errors``): one that runs away refuses the
        state, rather than holding the interpreter lock.
    :raises StateRefused: Anything else.
    """
    if not delta:
        return {}
    state_schema = doc.state_schema
    declared = state_schema.get("properties")
    properties = {**FIXED_STATE, **(declared if isinstance(declared, dict) else {})}
    unknown = sorted(key for key in delta if key not in properties)
    if unknown:
        raise StateRefused([f"{doc.name} doesn't take these state keys: {', '.join(unknown)}"])
    # A delta sets some of the state: only its keys' schemas apply, not the
    # state schema's own keywords (required and the like). Their references
    # resolve as in the state schema, against its $id and the schemas in its
    # $defs or definitions; one that isn't a schema is left out, so a
    # reference to it is refused rather than raising.
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if isinstance(state_schema.get("$id"), str):
        schema["$id"] = state_schema["$id"]
    for keyword in ("$defs", "definitions"):
        defs = state_schema.get(keyword)
        if isinstance(defs, dict):
            schema[keyword] = {name: each for name, each in defs.items() if isinstance(each, (dict, bool))}
    problems = [
        f"{'.'.join(str(p) for p in error.absolute_path) or 'stateDelta'}: {error.message}"
        for error in schema_errors(schema, dict(delta))
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


@dataclass
class _BuildLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


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
    :param leases: Where conversations' turn leases are kept
        (:mod:`.leases`): one turn at a time in each conversation, across
        every process sharing them; this process's alone by default.
    :param turn_wait: Seconds a turn waits for its conversation's lease
        before it's refused (:class:`~.leases.TurnBusy`).
    :param lease_seconds: Seconds a turn's lease lasts unless renewed (the
        turn renews it as it runs).
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
        leases: TurnLeases | None = None,
        turn_wait: float = TURN_WAIT,
        lease_seconds: float = LEASE_SECONDS,
    ) -> None:
        self.source = source
        self.leases: TurnLeases = leases or MemoryTurnLeases()
        self.turn_wait = turn_wait
        self.lease_seconds = lease_seconds
        self.observer = observer
        self.streaming = streaming
        self.models = models
        self.sessions = sessions or InMemorySessionService()
        self.artifacts = artifacts or InMemoryArtifactService()
        self.memory = memory or InMemoryMemoryService()
        services = services or RuntimeServices()
        self._source_dependencies = services.resolve_agent is None
        if services.resolve_agent is None:
            services = services.but(resolve_agent=self._latest)
        self.services = services
        self._owns_http = http is None
        self.http = http or http_client()
        self.cache_size = max(1, cache_size)
        self._runners: OrderedDict[str, Runner] = OrderedDict()
        self._building: dict[str, _BuildLock] = {}
        self._active: dict[str, int] = {}
        self._closing: set[asyncio.Task[None]] = set()
        self._pending = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def _latest(self, agent_id: str) -> ChatAgentDocument:
        return (await self.source.get(AgentRef(agent_id))).document

    async def locate(self, app_name: str) -> LocatedAgent:
        """
        :return: The agent a name means, and its organization, read before
            anything else of it: what a server lets callers in by, so
            nothing of another organization's agent is read for them.
        :raises AgentNotFound: There's no such agent.
        :raises BuildError: Its definition can't be read (a source without
            ``locate`` reads it to tell).
        """
        ref = AgentRef.parse(app_name)
        locate = getattr(self.source, "locate", None)
        if locate is not None:
            located: LocatedAgent = await locate(ref)
            return located
        try:
            found = await self.source.get(ref)
        except DocumentError as error:
            raise BuildError(str(error)) from error
        return LocatedAgent(ref, found.document.organization_id)

    async def resolve(self, app_name: str) -> ResolvedAgent:
        """:raises AgentNotFound: There's no such agent or version."""
        return await self._snapshot(await self.source.get(AgentRef.parse(app_name)))

    async def refusal(self, resolved: ResolvedAgent) -> str | None:
        """
        Check that whoever the current call is for (``services.access``) may
        start a turn with the agent: its chat agent node's rule, with any the
        runtime holds it to. Checked as every turn starts, not only as the
        chat agent runs: a conversation handed to a sub-agent goes straight
        to it on later turns.

        :return: Why they may not, as the agent would say it; None when they may.
        :raises BuildError: Its rule isn't one.
        """
        doc = resolved.document
        entry = doc.entry
        try:
            rule = node_rule(entry.config, doc.required_rules.get(entry.id), where=f"{entry.name!r}'s access")
        except RuleError as error:
            raise BuildError(str(error)) from error
        if rule is None:
            return None
        return await refused(
            Guarded(entry.name, rule), self.services.access, organization_id=doc.organization_id
        )

    async def _snapshot(self, resolved: ResolvedAgent) -> ResolvedAgent:
        """Capture exactly the graph both authorization and building will use.

        Published parents may refer to the latest saved agents, so a parent's
        version alone is not a runner cache key. Copy the definitions now and
        resolve external references once, before authorization. A build never
        consults the mutable source a second time.
        """
        if resolved.dependencies is not None:
            return resolved
        document = resolved.document.model_copy(deep=True)
        dependencies: dict[str, ResolvedAgent] = {}
        used: set[str] = set()
        walked: set[str] = set()

        async def visit(doc: ChatAgentDocument, chain: tuple[str, ...]) -> None:
            if doc.id in chain:
                raise BuildError(f"Saved agent {doc.id} uses itself through another saved agent.")
            # Embedded bundles may resolve the same ID differently in separate
            # branches; inspect each distinct document, including its bundle.
            identity = json.dumps(doc.to_json(), sort_keys=True)
            if identity in walked:
                return
            walked.add(identity)
            for node in doc.nodes:
                if node.kind != "saved_agent" or not (agent_id := str(node.config.get("agent") or "")):
                    continue
                used.add(agent_id)
                raw = next(
                    (raw for key, raw in doc.dependencies.items() if key.split("@", 1)[0] == agent_id),
                    None,
                )
                if raw is not None:
                    try:
                        child = parse_document(
                            {
                                **raw,
                                "dependencies": doc.dependencies,
                                "skills": {**doc.skills, **(raw.get("skills") or {})},
                                "ui_components": {
                                    **doc.ui_components,
                                    **(raw.get("ui_components") or {}),
                                },
                            }
                        )
                    except DocumentError as error:
                        raise BuildError(str(error)) from error
                    if child.id != agent_id:
                        raise BuildError(f"Saved agent {agent_id} carries the definition of {child.id}.")
                else:
                    if agent_id not in dependencies:
                        if self._source_dependencies:
                            found = await self.source.get(AgentRef(agent_id))
                        else:
                            assert self.services.resolve_agent is not None
                            child = await self.services.resolve_agent(agent_id)
                            found = ResolvedAgent(child, child.version, "")
                        dependencies[agent_id] = replace(found, document=found.document.model_copy(deep=True))
                    child = dependencies[agent_id].document
                if doc.organization_id is not None and child.organization_id != doc.organization_id:
                    raise BuildError(f"Saved agent {agent_id} isn't one of this organization's agents.")
                await visit(child, (*chain, doc.id))

        await visit(document, ())
        # Include actual contents for sources whose version stamp is not enough,
        # and source stamps for mutable resources such as each child's skills.
        fingerprint = json.dumps(
            [
                document.to_json(),
                # Not part of the format: the rules the source holds its agents to.
                document.required_rules,
                [
                    (key, each.cache_key, each.document.to_json())
                    for key, each in sorted(dependencies.items())
                ],
            ],
            sort_keys=True,
        )
        digest = hashlib.sha256(fingerprint.encode()).hexdigest()
        return replace(
            resolved,
            document=document,
            cache_key=f"{resolved.cache_key}#graph:{digest}",
            dependencies=tuple(dependencies.values()),
            used_agent_ids=frozenset(used),
        )

    async def runner(
        self, app_name: str, *, resolved: ResolvedAgent | None = None
    ) -> tuple[Runner, ResolvedAgent]:
        """
        :param app_name: The agent.
        :param resolved: Its definition, when the caller already found it.
        :return: A runner for the agent (built now if it isn't cached), and its definition.
        :raises AgentNotFound: There's no such agent or version.
        :raises BuildError: It can't be built as it is.

        For inspecting/preparing an agent. Call :meth:`use_runner` when
        retaining a runner across awaits, so eviction cannot close it.
        """
        async with self.use_runner(app_name, resolved=resolved) as found:
            return found

    @asynccontextmanager
    async def use_runner(
        self, app_name: str, *, resolved: ResolvedAgent | None = None
    ) -> AsyncIterator[tuple[Runner, ResolvedAgent]]:
        """Retain an agent until its turn (including cancellation) finishes.

        Active runners may temporarily exceed the cache size; only idle
        runners are evicted. Cache mutations do not await cleanup, so a slow
        toolset close cannot race another insertion or block another build.
        """
        if self._closed:
            raise RuntimeError("The agent executor is closed.")
        self._pending += 1
        self._idle.clear()
        key: str | None = None
        try:
            resolved = await self.resolve(app_name) if resolved is None else await self._snapshot(resolved)
            runner = await self._acquire_runner(resolved)
            key = resolved.cache_key
            self._evict()
            yield runner, resolved
        finally:
            if key is not None:
                self._active[key] -= 1
                if not self._active[key]:
                    del self._active[key]
                self._evict()
            self._pending -= 1
            if not self._pending:
                self._idle.set()

    async def _acquire_runner(self, resolved: ResolvedAgent) -> Runner:
        key = resolved.cache_key
        slot = self._building.setdefault(key, _BuildLock())
        slot.users += 1
        try:
            async with slot.lock:
                runner = self._runners.get(key)
                if runner is None:
                    app = await build_app(
                        resolved.document,
                        models=self.models,
                        services=self.services.but(resolve_agent=resolved.resolve_dependency),
                        http=self.http,
                        app_name=resolved.document.id,
                        plugins=(
                            TurnOutcomePlugin(),
                            TurnLeasePlugin(),
                            *(self.observer.plugins(resolved) if self.observer is not None else ()),
                        ),
                    )
                    runner = Runner(
                        app=app,
                        session_service=self.sessions,
                        artifact_service=self.artifacts,
                        memory_service=self.memory,
                    )
                    self._runners[key] = runner
                # No await between registration, recency, and retaining it.
                self._runners.move_to_end(key)
                self._active[key] = self._active.get(key, 0) + 1
                return runner
        finally:
            slot.users -= 1
            if not slot.users:
                self._building.pop(key, None)

    def _evict(self) -> None:
        for key in list(self._runners):
            if len(self._runners) <= self.cache_size:
                break
            if self._active.get(key):
                continue
            self._close_later(self._runners.pop(key))

    def _close_later(self, runner: Runner) -> None:
        task = asyncio.create_task(self._close_runner(runner))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    @staticmethod
    async def _close_runner(runner: Runner) -> None:
        try:
            await runner.close()
        except Exception:
            log.warning("Closing an agent failed", exc_info=True)

    def turn_key(self, request: RunRequest, *, resolved: ResolvedAgent | None = None) -> TurnKey:
        """:return: The conversation a request's turn is in, as its lease is kept."""
        agent_id = resolved.document.id if resolved is not None else AgentRef.parse(request.app_name).agent_id
        return TurnKey(agent_id, request.user_id, request.session_id)

    async def take_turn(self, request: RunRequest, *, resolved: ResolvedAgent | None = None) -> TurnLease:
        """
        :return: The lease of the request's conversation, for a turn in it:
            the caller gives it back (:meth:`TurnLease.release`) once the
            turn's over, its outcome kept.
        :raises TurnBusy: Another turn is running there (still, after ``turn_wait``).
        """
        return await TurnLease.take(
            self.leases,
            self.turn_key(request, resolved=resolved),
            seconds=self.lease_seconds,
            wait=self.turn_wait,
        )

    async def run(
        self,
        request: RunRequest,
        *,
        resolved: ResolvedAgent | None = None,
        lease: TurnLease | None = None,
    ) -> AsyncGenerator[Event, None]:
        """
        Runs one turn: whoever it's for may start it (:meth:`refusal`), the
        request's state is checked against what its agent declares, and the
        request itself is kept for the turn (``temp:request``) for
        instructions to read. It runs alone in its conversation: it takes
        the conversation's lease first (:meth:`take_turn`), unless its caller
        holds it (``lease``) and gives it back after.

        :raises TurnBusy: Another turn is running in the conversation; nothing
            of this one ran.
        :raises TurnLeaseLost: The turn's lease went to another turn; it stopped.
        :raises AgentNotFound: There's no such agent or version.
        :raises BuildError: It can't be built as it is.
        :raises StateRefused: The request sets state its agent doesn't take.
        :raises AccessRefused: Whoever it's for may not use the agent.
        """
        held = lease if lease is not None else await self.take_turn(request, resolved=resolved)
        owns_outcome = current_turn() is None
        try:
            with turn_scope() as outcome:
                try:
                    async with (
                        self.use_runner(request.app_name, resolved=resolved) as (runner, resolved),
                        aclosing(self._run(request, runner, resolved, held)) as events,
                    ):
                        async for event in events:
                            outcome.observe(event)
                            yield event
                    outcome.succeed()
                except AccessRefused as error:
                    outcome.fail("ACCESS_DENIED", str(error))
                    raise
                except Exception as error:
                    # The observer keeps the exception details; the public outcome
                    # is safe even when execution has no HTTP error mapper.
                    outcome.fail("INTERNAL_ERROR", "The agent ran into a problem. Try again.")
                    if held.lost and not isinstance(error, TurnLeaseLost):
                        # ADK wraps what TurnLeasePlugin raised: it's this.
                        raise TurnLeaseLost(str(error.__cause__ or error)) from error
                    raise
                except BaseException:
                    outcome.cancel()
                    raise
                finally:
                    # A transport persists after its own conversion/error mapping.
                    # Embedded calls have no outer owner to retain their failure.
                    # A turn that lost its conversation's lease mustn't write to it.
                    if owns_outcome and not held.lost:
                        app_name = (
                            resolved.document.id
                            if resolved is not None
                            else request.app_name.partition("@")[0]
                        )
                        await persist_turn(
                            outcome,
                            sessions=self.sessions,
                            app_name=app_name,
                            user_id=request.user_id,
                            session_id=request.session_id,
                            author=app_name,
                            invocation_id=request.invocation_id,
                        )
        finally:
            if lease is None:
                await held.release()

    async def _run(
        self, request: RunRequest, runner: Runner, resolved: ResolvedAgent, lease: TurnLease
    ) -> AsyncGenerator[Event, None]:
        why = await self.refusal(resolved)
        if why is not None:
            raise AccessRefused(why)
        delta = check_state(resolved.document, request.state_delta)
        delta = await self._titled(resolved.document.id, request, delta)
        request = await self._tasks_left_open(resolved.document.id, request)
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
        # Which agent, at which version, for what its tools start (running_agent).
        token = _running.set(resolved)
        try:
            # A turn whose conversation went to another writes nothing more (TurnLeasePlugin).
            with holding(lease):
                async with aclosing(self._events(runner, request, delta, mode, watched)) as events:
                    async for event in events:
                        yield event
        finally:
            # A turn closed from another context has nothing of its own to undo.
            with contextlib.suppress(ValueError):
                _running.reset(token)

    @staticmethod
    async def _events(
        runner: Runner,
        request: RunRequest,
        delta: dict[str, Any],
        mode: StreamingMode,
        watched: AbstractAsyncContextManager[object],
    ) -> AsyncGenerator[Event, None]:
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

    async def _tasks_left_open(self, agent_id: str, request: RunRequest) -> RunRequest:
        """
        :return: The request without the cancellations a chat sent for tasks
            still open in the conversation (``tasks``): the task goes on.
        """
        message = request.new_message
        if message is None or not any(part.function_response for part in message.parts or ()):
            return request
        session = await self.sessions.get_session(
            app_name=agent_id, user_id=request.user_id, session_id=request.session_id
        )
        if session is None:
            return request
        kept = without_cancelled_tasks(message, session.events)
        return request if kept is message else request.model_copy(update={"new_message": kept})

    async def _titled(self, agent_id: str, request: RunRequest, delta: dict[str, Any]) -> dict[str, Any]:
        """
        :return: The turn's state, naming a conversation that has no title
            yet after its first message (``forge_common.adk.titles``), with
            the turn rather than on its own, so the runner's session never
            goes stale under it.
        """
        user_id, session_id = request.user_id, request.session_id
        bare = await self.sessions.get_session(
            app_name=agent_id,
            user_id=user_id,
            session_id=session_id,
            config=GetSessionConfig(num_recent_events=0),
        )
        if bare is None or bare.state.get(TITLE_KEY):
            return delta
        # Untitled (mostly a new one, with no past to read): its first message names it.
        session = await self.sessions.get_session(app_name=agent_id, user_id=user_id, session_id=session_id)
        return with_title(delta, session, request.new_message) or delta

    async def close(self) -> None:
        """Wait for active turns/builds, then close their toolsets and owned HTTP client."""
        async with self._close_lock:
            self._closed = True
            await self._idle.wait()
            while self._runners:
                _, runner = self._runners.popitem()
                self._close_later(runner)
            if self._closing:
                # Cleanup outlives a cancelled close caller and is joined by
                # the next close call rather than abandoned mid-toolset.
                await asyncio.shield(asyncio.gather(*self._closing))
            if self._owns_http:
                await self.http.aclose()
