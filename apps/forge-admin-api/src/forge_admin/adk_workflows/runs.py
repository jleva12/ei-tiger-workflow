"""
Running an organization's ADK workflows (``forge.agent/v1``, ``documents.py``):
a run is kept in this API's database by the run store
(``forge_task_adk_workflows.run_store``, tables ``adk_runs`` and
``adk_run_events``), and the async worker's ``adk_workflows`` queue has a
``run_adk`` job take it: the ADK workflows task
(packages/python/adk-workflows) runs it on Google ADK's graph engine.
Their routes are ``api/routes/adk_workflow_runs.py``.

A run carries its ADK workflow's document as it is when the run starts (the
version it runs: its draft, or a published one), and the documents of the
saved ADK workflows it runs (its ``saved`` nodes', the workflows its LLM
agents call as tools, and theirs, and so on), each at the version named
(``versions.py``), so editing any of them never changes a run in progress.
So does what its LLM agents use
(``resources.py``): the agents from the Agents page they are or call, at the
version named, and the knowledge bases they search; the MCP servers they use
are checked to be the organization's. It acts as the member who started it.

Before it's created, the input is checked against the start's
``input_schema`` and the document is built (``build_agent``), so a mistake is
answered at once; the worker checks both again.

The run's state is its ADK session, which the worker keeps in the admin
database (ADK's ``DatabaseSessionService``, app ``adk_workflows``, user the
member it acts as, and the session ID made here): its steps are read from it
(``forge_task_adk_workflows.steps``).

Where it waits for a person is its ``pause``, whose ``kind`` (and its
``details``' ``kind``) says what: an ``approval`` is decided; ``human_input``
is answered, the answer held to the question's ``response_schema`` here and
kept as the decision's comment, as JSON.

Whenever a run is to be taken (created, decided, answered, retried,
resubmitted), a job is queued for it. If that fails, the run stays queued and
the worker's maintenance finds it: the caller isn't refused.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from forge_task_adk_workflows.graph.uses import (
    AgentUse,
    agents_in,
    saved_in,
    tools_in,
    workflows_in,
)
from forge_task_adk_workflows.run_store import Actor, RunStore
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from forge_admin.adk_workflows.build import AgentBuildError, build_agent
from forge_admin.adk_workflows.queue import Embedding, EmbeddingError

if TYPE_CHECKING:
    from forge_admin.adk_workflows.resources import RunResources

logger = logging.getLogger(__name__)

#: ADK's app name for the runs' sessions.
APP_NAME = "adk_workflows"
#: The most characters of JSON an answer may be: a decision's comment's.
MAX_ANSWER = 4000
#: The most problems a refusal names.
MAX_PROBLEMS = 5

#: Finds one of the organization's ADK workflows' documents by reference
#: (``ag_x``, ``ag_x@3``, ``ag_x@draft``; ``versions.workflow_finder``):
#: None when there's no such workflow; LookupError for no such version.
FindAgent = Callable[[str], Awaitable[dict[str, Any] | None]]
#: The run statuses a run that's waited for is still going in: a pause
#: (someone's to decide or answer) or an end stops the wait.
GOING = frozenset({"queued", "running", "waiting"})


class AdkRunError(ValueError):
    """The run can't start, or the answer doesn't fit: the message says why."""


def start_schema(document: dict[str, Any]) -> dict[str, Any] | None:
    """
    :return: The input schema the ADK workflow's start declares (``{}`` for
        any input); None when it has no start.
    """
    for node in document.get("nodes") or []:
        if isinstance(node, dict) and node.get("kind") == "start":
            config = node.get("config")
            schema = config.get("input_schema") if isinstance(config, dict) else None
            return schema if isinstance(schema, dict) else {}
    return None


def check_input(document: dict[str, Any], value: Any) -> None:
    """
    :raises AdkRunError: The value doesn't fit the start's input schema, or
        the ADK workflow has no start.
    """
    schema = start_schema(document)
    if schema is None:
        raise AdkRunError("The ADK workflow has no start")
    problems = misfits(
        schema, value, not_a_schema="The start's input schema isn't a JSON Schema"
    )
    if problems:
        raise AdkRunError("The input doesn't fit the start: " + problems)


def misfits(
    schema: dict[str, Any], value: Any, *, not_a_schema: str, whole: str = "input"
) -> str:
    """
    How a value doesn't fit a JSON Schema (Draft 2020-12), for a refusal.

    :param schema: The schema; ``{}`` takes anything.
    :param value: The value.
    :param not_a_schema: What a refusal says when the schema isn't one.
    :param whole: What a problem with the whole value is said of.
    :return: The first few problems, each with where (``request/id: ...``);
        empty when it fits.
    :raises AdkRunError: The schema isn't a JSON Schema.
    """
    if not schema:
        return ""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise AdkRunError(f"{not_a_schema}: {error.message}") from None
    validator = Draft202012Validator(schema)
    found = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    return "; ".join(
        f"{'/'.join(map(str, e.absolute_path)) or whole}: {e.message}"
        for e in found[:MAX_PROBLEMS]
    )


def saved_ids(document: dict[str, Any]) -> list[str]:
    """
    :return: The saved ADK workflows the document's ``saved`` nodes run, then
        those its LLM agents call as tools, in order, by reference (``ag_x``,
        ``ag_x@3``, ``ag_x@draft``).
    """
    found = saved_in(document)
    return found + [w for w in workflows_in(document) if w not in found]


async def saved_documents(
    document: dict[str, Any], find: FindAgent
) -> dict[str, dict[str, Any]]:
    """
    The documents of the saved ADK workflows a document runs, and those they
    run, and so on, each at the version named. One that isn't there is left
    out: the build says so, naming the node.

    :param document: The ADK workflow's document.
    :param find: Finds one of the organization's ADK workflows' documents.
    :return: Each one's document, by reference.
    :raises AdkRunError: One names a version it doesn't have.
    :raises PyMongoError: The store isn't answering.
    """
    found: dict[str, dict[str, Any]] = {}
    missing: set[str] = set()
    waiting = saved_ids(document)
    while waiting:
        ref = waiting.pop(0)
        if ref in found or ref in missing:
            continue
        try:
            other = await find(ref)
        except LookupError as error:
            raise AdkRunError(str(error)) from None
        if other is None:
            missing.add(ref)
            continue
        found[ref] = snapshot(other)
        waiting.extend(saved_ids(found[ref]))
    return found


def check_builds(document: dict[str, Any], saved: dict[str, dict[str, Any]]) -> None:
    """
    :raises AdkRunError: The document can't be built into an ADK workflow,
        with the build's reason (naming the node).
    """
    try:
        build_agent(document, resolve=saved.get)
    except AgentBuildError as error:
        raise AdkRunError(str(error)) from None


@dataclass(frozen=True)
class PreparedRun:
    """
    What a run carries besides its document, as it starts.

    :ivar saved: The saved ADK workflows' documents it runs, by ID.
    :ivar chat_agents: The agents from the Agents page its LLM agents are or
        call, by reference (``ca_x``, ``ca_x@3``, ``ca_x@draft``).
    :ivar knowledge_bases: The knowledge bases they search: name and description, by ID.
    """

    saved: dict[str, dict[str, Any]]
    chat_agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    knowledge_bases: dict[str, dict[str, str]] = field(default_factory=dict)


async def prepare_run(
    record: dict[str, Any],
    input: Any,
    find: FindAgent,
    resources: "RunResources | None" = None,
) -> PreparedRun:
    """
    Check a run can start: its input fits the start, its document, with the
    saved ADK workflows it runs, builds, and what its LLM agents use is the
    organization's.

    :param record: The ADK workflow's record (``AgentStore``).
    :param input: What the run starts with.
    :param find: Finds the organization's other ADK workflows' records.
    :param resources: The organization's agents, knowledge bases and MCP
        servers; None when there are none to use.
    :return: What the run carries.
    :raises AdkRunError: The input doesn't fit, the document doesn't build,
        or something it uses isn't there (naming the node).
    :raises PyMongoError: The store isn't answering.
    """
    document = snapshot(record["document"])
    check_input(document, input)
    saved = await saved_documents(document, find)
    check_builds(document, saved)
    chat_agents, knowledge_bases = await used_resources(
        [document, *saved.values()], resources
    )
    return PreparedRun(saved, chat_agents, knowledge_bases)


async def used_resources(
    documents: list[dict[str, Any]], resources: "RunResources | None"
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    """
    What the documents' LLM agents use from the organization, checked.

    :return: The agents from the Agents page, by reference, and the
        knowledge bases, by ID.
    :raises AdkRunError: One isn't the organization's, or can't be had (naming the node).
    """
    agents: list[AgentUse] = []
    knowledge: dict[str, str] = {}
    servers: dict[str, str] = {}
    for document in documents:
        agents.extend(agents_in(document))
        for tool in tools_in(document):
            if tool.kind == "saved_agent":
                agents.append(
                    AgentUse(tool.where, str(tool.config.get("agent") or ""), None)
                )
            elif tool.kind == "knowledge_base":
                for kb in tool.config.get("knowledge_bases") or []:
                    knowledge.setdefault(str(kb), tool.where)
            elif tool.kind == "mcp" and tool.config.get("server"):
                servers.setdefault(str(tool.config["server"]), tool.where)
    if not (agents or knowledge or servers):
        return {}, {}
    if resources is None:
        where = (agents[0].where if agents else None) or next(
            iter({**knowledge, **servers}.values())
        )
        raise AdkRunError(f"{where}: what it uses isn't set up here")
    chat_agents: dict[str, dict[str, Any]] = {}
    for use in agents:
        if not use.agent:
            raise AdkRunError(f"{use.where}: choose the agent it uses")
        if use.ref in chat_agents:
            continue
        try:
            chat_agents[use.ref] = await resources.chat_agent(use.agent, use.version)
        except LookupError as error:
            raise AdkRunError(f"{use.where}: {error}") from None
    found = await resources.knowledge_bases(list(knowledge)) if knowledge else {}
    for kb, where in knowledge.items():
        if kb not in found:
            raise AdkRunError(
                f"{where}: the organization has no knowledge base {kb}: pick another"
            )
    known = await resources.mcp_servers(list(servers)) if servers else set()
    for server, where in servers.items():
        if server not in known:
            raise AdkRunError(
                f"{where}: the organization has no MCP server {server}: pick another"
            )
    return chat_agents, found


async def start_adk_run(
    runs: RunStore,
    queue: Embedding,
    record: dict[str, Any],
    *,
    prepared: PreparedRun,
    input: Any,
    run_as: str,
    run_as_name: str,
    trigger: dict[str, Any],
    version: int | str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """
    Start a run of an ADK workflow, as its record is now: the run, queued,
    in a new ADK session, and a job to take it.

    :param runs: The run store.
    :param queue: The async worker's queues.
    :param record: The ADK workflow's record (``AgentStore``), its document
        the version that runs (``ResolvedWorkflow.run_record``).
    :param prepared: What it carries besides its document (``prepare_run``).
    :param input: What the run starts with; checked already.
    :param run_as: Who it acts as: its ADK session's user (a member, an API
        key, a chat agent).
    :param run_as_name: Their name, for its record.
    :param trigger: What started it: ``{"type": "manual", "by": <user>}``.
    :param version: Which version runs: ``"draft"`` or a published one's number.
    :param run_id: Its ID, when the caller has one for it (an A2A task's).
    :return: The run, as the store has it.
    :raises SQLAlchemyError: The run store's database isn't answering.
    """
    agent_id = record["_id"]
    session_id = str(uuid4())
    document = snapshot(record["document"])
    name = str(document.get("name") or "")
    # What the worker runs (forge_task_adk_workflows.runs.RunPayload).
    payload = {
        "tenant_id": record["organization_id"],
        "agent_id": agent_id,
        "revision": record["revision"],
        "version": version,
        "name": name,
        "document": document,
        "saved": prepared.saved,
        "chat_agents": prepared.chat_agents,
        "knowledge_bases": prepared.knowledge_bases,
        "input": input,
        "session_id": session_id,
        "run_as": run_as,
        "run_as_name": run_as_name,
        "trigger": trigger,
    }
    run = await runs.create(
        organization_id=record["organization_id"],
        agent_id=agent_id,
        agent_name=name or agent_id,
        revision=record["revision"],
        session_id=session_id,
        payload=payload,
        requested_by=Actor(run_as, run_as_name),
        version=None if version is None else str(version),
        run_id=run_id,
    )
    await queue_run(queue, run["id"])
    return run


async def settle(
    runs: RunStore,
    run_id: str,
    *,
    wait: float,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    organization_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Wait for a run to stop going: to pause for someone, or end. Polls the
    store, every half second at first, then less often.

    :param runs: The run store.
    :param run_id: The run.
    :param wait: The most seconds to wait.
    :param is_disconnected: Whether whoever's waiting has gone.
    :param organization_id: The organization it must be; None for any.
    :return: The run as it is when it stopped, or the wait ran out; None
        when there's no such run.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, wait)
    delay = 0.5
    while True:
        run = await runs.get(run_id, organization_id=organization_id)
        left = deadline - loop.time()
        if run is None or run["status"] not in GOING or left <= 0:
            return run
        if is_disconnected is not None and await is_disconnected():
            return run
        await asyncio.sleep(min(delay, left))
        delay = min(delay * 1.5, 2.0)


async def check_publishable(
    document: dict[str, Any],
    find: FindAgent,
    resources: "RunResources | None" = None,
) -> list[str]:
    """
    Whether a workflow's draft can be published: a published version never
    changes, so it may only run what doesn't either.

    :param document: The draft.
    :param find: Finds the organization's workflows (``versions.workflow_finder``).
    :param resources: The organization's agents, knowledge bases and MCP servers.
    :return: Why it can't be published yet; none when it can.
    """
    problems: list[str] = []
    if start_schema(document) is None:
        problems.append("It has no start.")
    for ref in saved_ids(document):
        workflow_id, _, version = ref.partition("@")
        if version == "draft":
            problems.append(
                f"It runs workflow {workflow_id}'s draft, which can change: "
                "pick one of its published versions, or its latest."
            )
        elif not version:
            try:
                latest = await find(ref)
            except LookupError as error:
                problems.append(str(error))
                continue
            if latest is not None and "version" not in latest:
                problems.append(
                    f"It runs workflow {workflow_id}, which hasn't been "
                    "published: publish that first."
                )
    for use in agents_in(document):
        if use.version == "draft":
            problems.append(
                f"{use.where} uses agent {use.agent}'s draft, which can change: "
                "pick one of its published versions, or its latest."
            )
    if problems:
        return problems
    try:
        saved = await saved_documents(document, find)
        check_builds(document, saved)
        await used_resources([document, *saved.values()], resources)
    except AdkRunError as error:
        problems.append(str(error))
    return problems


async def queue_run(queue: Embedding, run_id: str) -> None:
    """
    Queue a job that takes the run. When the queue can't be reached, the run
    stays queued and the worker's maintenance finds it later: only logged.
    """
    try:
        await queue.run_adk(run_id)
    except EmbeddingError as error:
        logger.warning(
            "Couldn't queue ADK workflow run %s; the worker will find it: %s",
            run_id,
            error,
        )


def checked_answer(details: dict[str, Any], answer: Any) -> str:
    """
    A person's answer to a run's question (human input), as the decision's
    comment carries it.

    :param details: The question, its pause's details: its
        ``response_schema`` (none or ``{}`` takes any answer).
    :param answer: The answer.
    :return: The answer as JSON.
    :raises AdkRunError: It doesn't fit the schema, or is too long.
    """
    schema = details.get("response_schema")
    problems = misfits(
        schema if isinstance(schema, dict) else {},
        answer,
        not_a_schema="What the question asks for isn't a JSON Schema",
        whole="answer",
    )
    if problems:
        raise AdkRunError("The answer doesn't fit what the question asks: " + problems)
    text = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_ANSWER:
        raise AdkRunError(
            f"The answer is {len(text):,} characters of JSON; the most it can be "
            f"is {MAX_ANSWER:,}"
        )
    return text


def snapshot(document: Any) -> dict[str, Any]:
    """:return: A document as JSON (MongoDB hands back its own types)."""
    copied = json.loads(json.dumps(document, default=str))
    return copied if isinstance(copied, dict) else {}
