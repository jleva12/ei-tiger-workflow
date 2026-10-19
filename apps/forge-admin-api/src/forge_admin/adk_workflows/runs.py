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
answered at once; the worker checks both again. The files it's sent are
held to its start (``files.py``) and saved as ADK artifacts of its session,
where the worker reads them, before it's queued.

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
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from forge_agent_runtime.access import caller_json
from forge_common.patterns import schema_errors
from forge_task_adk_workflows.files import save_run_files
from forge_task_adk_workflows.graph.uses import (
    AgentUse,
    agents_in,
    saved_in,
    tools_in,
    workflows_in,
)
from forge_task_adk_workflows.run_store import (
    CHILD_RUN,
    PAUSED,
    Actor,
    Provenance,
    RunStore,
    RunTaken,
    open_gates,
)
from google.adk.artifacts import BaseArtifactService
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy.exc import SQLAlchemyError

from forge_admin.adk_workflows.build import AgentBuildError, build_agent
from forge_admin.adk_workflows.files import (
    CheckedFile,
    FilesRefused,
    SentFile,
    checked_files,
)
from forge_admin.adk_workflows.queue import Embedding, EmbeddingError
from forge_admin.chat_agents.access import AgentUseRefused, rules_in

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


def going(run: Mapping[str, Any]) -> bool:
    """
    Whether a run that's waited for is still going: in a status of
    :data:`GOING`, or paused only for runs it started (workflows its LLM
    agents called: nobody's to decide or answer anything, and those runs'
    ends carry it on).
    """
    if run.get("status") in GOING:
        return True
    gates = open_gates(run)
    return (
        run.get("status") == PAUSED
        and bool(gates)
        and all(gate.get("kind") == CHILD_RUN for gate in gates)
    )


class AdkRunError(ValueError):
    """The run can't start, or the answer doesn't fit: the message says why."""


class AdkRunRefused(AdkRunError):
    """
    Whoever the run is for may not use an agent from the Agents page it uses
    (``forge_admin.chat_agents.access``): the message names it, and what it needs.
    """


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
    How a value doesn't fit a JSON Schema (Draft 2020-12), for a refusal. Its
    patterns run on forge-common's bounded engine (``schema_errors``): one that
    runs away refuses the value in half a second, rather than holding the
    interpreter lock, and the event loop with it, for as long as it backtracks.

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
    found = sorted(schema_errors(schema, value), key=lambda e: list(e.absolute_path))
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
    :ivar files: The files it starts with, held to its start; saved as it starts.
    :ivar skills: The skills their Skills tools use, each whole
        (``forge_agent_runtime.SkillBundle``'s JSON), by ID.
    :ivar agent_access: What whoever the run is for holds of the agent
        permissions its agents' rules name (``forge_agent_runtime.access``),
        as it starts: what the worker checks those rules against. None:
        nobody holds any.
    """

    saved: dict[str, dict[str, Any]]
    chat_agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    knowledge_bases: dict[str, dict[str, str]] = field(default_factory=dict)
    files: list[CheckedFile] = field(default_factory=list)
    skills: dict[str, dict[str, Any]] = field(default_factory=dict)
    agent_access: dict[str, Any] | None = None


async def prepare_run(
    record: dict[str, Any],
    input: Any,
    find: FindAgent,
    resources: "RunResources | None" = None,
    files: list[SentFile] | None = None,
) -> PreparedRun:
    """
    Check a run can start: its input fits the start, and its files too, its
    document, with the saved ADK workflows it runs, builds, and what its LLM
    agents use is the organization's.

    :param record: The ADK workflow's record (``AgentStore``).
    :param input: What the run starts with.
    :param find: Finds the organization's other ADK workflows' records.
    :param resources: The organization's agents, knowledge bases and MCP
        servers; None when there are none to use.
    :param files: The files it starts with, as they were sent.
    :return: What the run carries.
    :raises AdkRunRefused: Whoever the run is for may not run it, a saved
        ADK workflow it runs, or an agent from the Agents page it uses.
    :raises AdkRunError: The input or a file doesn't fit, the document
        doesn't build, or something it uses isn't there (naming the node).
    :raises PyMongoError: The store isn't answering.
    """
    document = snapshot(record["document"])
    saved = await saved_documents(document, find)
    if resources is not None:
        try:
            await resources.check_workflows(record, saved)
        except AgentUseRefused as error:
            raise AdkRunRefused(str(error)) from None
    check_input(document, input)
    try:
        checked = checked_files(document, files or [])
    except FilesRefused as error:
        raise AdkRunError(str(error)) from None
    check_builds(document, saved)
    chat_agents, knowledge_bases, skills = await used_resources(
        [document, *saved.values()], resources
    )
    access = None
    if resources is not None:
        wanted = wanted_permissions([document, *saved.values()], chat_agents.values())
        caller = await resources.caller(wanted)
        access = caller_json(caller) if caller is not None else None
    return PreparedRun(saved, chat_agents, knowledge_bases, checked, skills, access)


def wanted_permissions(
    documents: Sequence[Mapping[str, Any]], chat_agents: Iterable[Mapping[str, Any]]
) -> frozenset[int]:
    """
    :param documents: A run's ADK workflow and the saved ones it runs.
    :param chat_agents: The agents from the Agents page they use, as the run
        carries them.
    :return: The agent permissions their rules name: on those agents' nodes,
        and on the LLM agents' Saved agent tools.
    """
    return rules_in(chat_agents) | rules_in(
        [
            {"nodes": [{"config": tool.config}]}
            for workflow in documents
            for tool in tools_in(dict(workflow))
            if tool.kind == "saved_agent"
        ]
    )


async def access_now(
    payload: Mapping[str, Any], resources: "RunResources"
) -> dict[str, Any] | None:
    """
    What whoever a resubmission is for holds now of the agent permissions its
    agents' rules name (``PreparedRun.agent_access``): the agents the run it's
    of carries, as they were when that started. What that run held isn't
    carried forward: it may have been taken away since, or been someone
    else's.

    :param payload: The run resubmitted's (``RunPayload``).
    :param resources: The organization's, for whoever the resubmission is for.
    """
    documents = [payload.get("document") or {}, *(payload.get("saved") or {}).values()]
    wanted = wanted_permissions(documents, (payload.get("chat_agents") or {}).values())
    caller = await resources.caller(wanted)
    return caller_json(caller) if caller is not None else None


async def used_resources(
    documents: list[dict[str, Any]], resources: "RunResources | None"
) -> tuple[
    dict[str, dict[str, Any]], dict[str, dict[str, str]], dict[str, dict[str, Any]]
]:
    """
    What the documents' LLM agents use from the organization, checked.

    :return: The agents from the Agents page, by reference, the knowledge
        bases, by ID, and the skills their Skills tools use, whole, by ID
        (those of the agents from the Agents page are bundled with them).
    :raises AdkRunError: One isn't the organization's, or can't be had (naming the node).
    :raises AdkRunRefused: Whoever the run is for may not use an agent from
        the Agents page it uses (naming the node).
    """
    agents: list[AgentUse] = []
    knowledge: dict[str, str] = {}
    servers: dict[str, str] = {}
    skills: dict[str, str] = {}
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
            elif tool.kind == "skills":
                for skill in tool.config.get("skills") or []:
                    skills.setdefault(str(skill), tool.where)
    if not (agents or knowledge or servers or skills):
        return {}, {}, {}
    if resources is None:
        where = (agents[0].where if agents else None) or next(
            iter({**knowledge, **servers, **skills}.values())
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
        except AgentUseRefused as error:
            raise AdkRunRefused(f"{use.where}: {error}") from None
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
    bundles = await resources.skills(list(skills)) if skills else {}
    for skill, where in skills.items():
        if skill not in bundles:
            raise AdkRunError(
                f"{where}: the organization has no skill {skill}: it was deleted; "
                "pick another"
            )
    return (
        chat_agents,
        found,
        {key: bundle.model_dump(mode="json") for key, bundle in bundles.items()},
    )


#: The header a client sends to make a start safe to retry: the same key
#: (in the organization, from the same caller) starts one run, however many
#: times it's sent.
IDEMPOTENCY_KEY = "Idempotency-Key"
#: What an idempotency key may be: up to 255 printable ASCII characters.
IDEMPOTENCY_PATTERN = r"^[\x21-\x7e]{1,255}$"


def idempotent_run_id(organization_id: str, key: str) -> str:
    """
    :return: The ID of the run a start sent with an idempotency key makes:
        UUID5 of the key in the organization's namespace, as the run store's
        hex. A retry of the start finds that run rather than making another.
    """
    try:
        namespace = UUID(organization_id)
    except ValueError:
        namespace = uuid5(NAMESPACE_URL, f"forge:organization:{organization_id}")
    return uuid5(namespace, key).hex


def same_input(run: dict[str, Any], input: Any) -> bool:
    """:return: Whether a run started with ``input``: an idempotent start's
    retry starts with the same."""
    payload = run.get("payload")
    kept = payload.get("input") if isinstance(payload, dict) else None
    return json.dumps(kept, sort_keys=True, default=str) == json.dumps(
        input, sort_keys=True, default=str
    )


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
    artifacts: BaseArtifactService | None = None,
    provenance: Provenance | None = None,
) -> dict[str, Any]:
    """
    Start a run of an ADK workflow, as its record is now: the run, queued,
    in a new ADK session (its files saved as artifacts of it), and a job to
    take it.

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
    :param run_id: Its ID, when the caller has one for it; one already
        taken fails (:func:`start_adk_run_once` hands back its run instead).
    :param artifacts: Where its files are saved, when it has any (the
        worker's runs' artifacts).
    :param provenance: How it was started, for its audit
        (``forge_admin.adk_workflows.provenance``).
    :return: The run, as the store has it.
    :raises SQLAlchemyError: The run store's database isn't answering.
    :raises botocore.exceptions.BotoCoreError: Its files' storage isn't
        answering (an S3 artifact service's; ``OSError`` a folder's).
    """
    session_id, name, payload = await _new_run(
        record, prepared, input, run_as, run_as_name, trigger, version, artifacts
    )
    run = await runs.create(
        organization_id=record["organization_id"],
        agent_id=record["_id"],
        agent_name=name or record["_id"],
        revision=record["revision"],
        session_id=session_id,
        payload=payload,
        requested_by=Actor(run_as, run_as_name),
        version=None if version is None else str(version),
        run_id=run_id,
        provenance=provenance,
    )
    await queue_run(queue, run["id"])
    return run


async def start_adk_run_once(
    runs: RunStore,
    queue: Embedding,
    record: dict[str, Any],
    *,
    run_id: str,
    prepared: PreparedRun,
    input: Any,
    run_as: str,
    run_as_name: str,
    trigger: dict[str, Any],
    version: int | str | None = None,
    artifacts: BaseArtifactService | None = None,
    provenance: Provenance | None = None,
) -> tuple[dict[str, Any], bool]:
    """
    :func:`start_adk_run`, under an ID its start makes for it (an
    idempotency key's, an A2A task's, an intake's record's): asked again for
    that ID, it's the run the first start made, queued once. Its files are
    saved only for a run that's new; a start that loses a race for the ID
    takes the files it saved away again.

    :return: The run, and whether this start made it.
    :raises RunTaken: The ID is a run of another workflow, or someone else's.
    :raises SQLAlchemyError: The run store's database isn't answering.
    """
    requested_by = Actor(run_as, run_as_name)
    found = await runs.started_as(
        run_id,
        organization_id=record["organization_id"],
        agent_id=record["_id"],
        requested_by=requested_by,
    )
    if found is not None:
        return found, False
    session_id, name, payload = await _new_run(
        record, prepared, input, run_as, run_as_name, trigger, version, artifacts
    )
    try:
        run, created = await runs.create_once(
            run_id=run_id,
            organization_id=record["organization_id"],
            agent_id=record["_id"],
            agent_name=name or record["_id"],
            revision=record["revision"],
            session_id=session_id,
            payload=payload,
            requested_by=requested_by,
            version=None if version is None else str(version),
            provenance=provenance,
        )
    except RunTaken:
        # Nothing has them. (Failing otherwise, the run may have been made
        # with them: they stay.)
        await _drop_files(artifacts, run_as, session_id, payload)
        raise
    if created:
        await queue_run(queue, run["id"])
    else:
        # Another start for the ID got there first: its files are the run's.
        await _drop_files(artifacts, run_as, session_id, payload)
    return run, created


async def _new_run(
    record: dict[str, Any],
    prepared: PreparedRun,
    input: Any,
    run_as: str,
    run_as_name: str,
    trigger: dict[str, Any],
    version: int | str | None,
    artifacts: BaseArtifactService | None,
) -> tuple[str, str, dict[str, Any]]:
    """:return: A new run's ADK session (its files saved in it), its name, and its payload."""
    session_id = str(uuid4())
    document = snapshot(record["document"])
    name = str(document.get("name") or "")
    files = []
    if prepared.files:
        assert artifacts is not None, "a run with files needs where to save them"
        files = await save_run_files(
            artifacts,
            app_name=APP_NAME,
            user_id=run_as,
            session_id=session_id,
            files=[file.saved() for file in prepared.files],
        )
    # What the worker runs (forge_task_adk_workflows.runs.RunPayload).
    payload = {
        "tenant_id": record["organization_id"],
        "agent_id": record["_id"],
        "revision": record["revision"],
        "version": version,
        "name": name,
        "document": document,
        "saved": prepared.saved,
        "chat_agents": prepared.chat_agents,
        "knowledge_bases": prepared.knowledge_bases,
        "skills": prepared.skills,
        "agent_access": prepared.agent_access,
        "input": input,
        "files": [file.model_dump(mode="json") for file in files],
        "session_id": session_id,
        "run_as": run_as,
        "run_as_name": run_as_name,
        "trigger": trigger,
    }
    return session_id, name, payload


async def _drop_files(
    artifacts: BaseArtifactService | None,
    run_as: str,
    session_id: str,
    payload: dict[str, Any],
) -> None:
    """Take away the files a start saved for a run it didn't make: only
    logged when their storage won't."""
    if artifacts is None:
        return
    for file in payload.get("files") or []:
        try:
            await artifacts.delete_artifact(
                app_name=APP_NAME,
                user_id=run_as,
                session_id=session_id,
                filename=str(file["name"]),
            )
        except Exception as error:  # noqa: BLE001 - a leftover file doesn't fail the start
            logger.warning(
                "Couldn't remove file %s a duplicate start saved in session %s: %s",
                file.get("name"),
                session_id,
                error,
            )


async def refuse_start(
    runs: RunStore,
    *,
    organization_id: str,
    agent_id: str,
    status_code: int,
    reason: str,
    requested_by: Actor | None,
    provenance: Provenance | None,
    version: int | str | None = None,
) -> None:
    """
    Keep a start that was refused (403: they may not; 422: what it'd run
    doesn't fit or build) for the organization's audit
    (``RunStore.refuse``). Its store not answering doesn't change the
    answer: it's logged instead.
    """
    try:
        await runs.refuse(
            organization_id=organization_id,
            agent_id=agent_id,
            version=None if version is None else str(version),
            status_code=status_code,
            reason=reason,
            requested_by=requested_by,
            provenance=provenance,
        )
    except (SQLAlchemyError, OSError) as error:
        logger.warning(
            "Couldn't keep the refused start of %s in organization %s (%d: %s): %s",
            agent_id,
            organization_id,
            status_code,
            reason,
            error,
        )


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
        if run is None or not going(run) or left <= 0:
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
    return await check_runnable(document, find, resources)


async def check_runnable(
    document: dict[str, Any],
    find: FindAgent,
    resources: "RunResources | None" = None,
) -> list[str]:
    """
    Whether a workflow's document can run as it is (its draft too): it has a
    start, the workflows it runs are there, it builds, and what its LLM
    agents use is the organization's. The build stops at its first problem,
    so a document may have more than the one named.

    :param document: The document.
    :param find: Finds the organization's workflows (``versions.workflow_finder``).
    :param resources: The organization's agents, knowledge bases and MCP servers.
    :return: Why it can't run yet; none when it can.
    """
    if start_schema(document) is None:
        return ["It has no start."]
    try:
        saved = await saved_documents(document, find)
        check_builds(document, saved)
        await used_resources([document, *saved.values()], resources)
    except AdkRunError as error:
        return [str(error)]
    return []


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
