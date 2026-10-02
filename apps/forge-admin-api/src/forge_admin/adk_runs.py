"""
Running an organization's ADK workflows (``forge.agent/v1``, ``agent_documents.py``)
on the async worker's ``adk_workflows`` queue: the ADK workflows task
(packages/python/tasks/adk-workflows) runs each on Google ADK's graph engine.
Their routes are ``api/routes/adk_workflow_runs.py``.

A run carries its ADK workflow's document as it is when the run starts, and
the documents of the saved ADK workflows it runs (its ``saved`` nodes', and
theirs, and so on), so editing any of them never changes a run in progress.
It acts as the member who started it. It's one of the organization's
background tasks, labelled with the ADK workflow and its ADK session.

Before it's submitted, the input is checked against the start's
``input_schema`` and the document is built (``build_agent``), so a mistake is
answered at once; the worker checks both again.

The run's state is its ADK session, which the worker keeps in the admin
database (ADK's ``DatabaseSessionService``, app ``adk_workflows``, user the
member it acts as, and the session ID made here): its steps are read from it
(``forge_task_adk_workflows.steps``).

Where it waits for a person is its background task's approval, whose
``details`` say what kind: an ``approval`` is decided; ``human_input`` is
answered, the answer held to the question's ``response_schema`` here and
sent as the decision's comment, as JSON.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from forge_admin.agent_build import AgentBuildError, build_agent
from forge_admin.embedding import ADK_WORKFLOWS, Embedding

#: ADK's app name for the runs' sessions.
APP_NAME = "adk_workflows"
#: The labels a run carries: the ADK workflow it runs, and its ADK session.
AGENT_LABEL = "adk_workflow"
SESSION_LABEL = "adk_session"
#: The most characters of JSON an answer may be: a decision's comment's.
MAX_ANSWER = 4000
#: The most problems a refusal names.
MAX_PROBLEMS = 5

#: Finds one of the organization's ADK workflows' records by ID; None when
#: there's no such one.
FindAgent = Callable[[str], Awaitable[dict[str, Any] | None]]


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
    """:return: The saved ADK workflows the document's ``saved`` nodes run, in order."""
    found: list[str] = []
    for node in document.get("nodes") or []:
        if isinstance(node, dict) and node.get("kind") == "saved":
            config = node.get("config")
            agent = config.get("agent") if isinstance(config, dict) else None
            if isinstance(agent, str) and agent and agent not in found:
                found.append(agent)
    return found


async def saved_documents(
    document: dict[str, Any], find: FindAgent
) -> dict[str, dict[str, Any]]:
    """
    The documents of the saved ADK workflows a document runs, and those they
    run, and so on, as they're saved now. One that isn't there is left out:
    the build says so.

    :param document: The ADK workflow's document.
    :param find: Finds one of the organization's ADK workflows' records.
    :return: Each one's document, by ID.
    :raises PyMongoError: The store isn't answering.
    """
    found: dict[str, dict[str, Any]] = {}
    missing: set[str] = set()
    waiting = saved_ids(document)
    while waiting:
        agent_id = waiting.pop(0)
        if agent_id in found or agent_id in missing:
            continue
        record = await find(agent_id)
        if record is None:
            missing.add(agent_id)
            continue
        found[agent_id] = snapshot(record["document"])
        waiting.extend(saved_ids(found[agent_id]))
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


async def prepare_run(
    record: dict[str, Any], input: Any, find: FindAgent
) -> dict[str, dict[str, Any]]:
    """
    Check a run can start: its input fits the start, and its document, with
    the saved ADK workflows it runs, builds.

    :param record: The ADK workflow's record (``AgentStore``).
    :param input: What the run starts with.
    :param find: Finds the organization's other ADK workflows' records.
    :return: The saved ADK workflows' documents it runs, by ID.
    :raises AdkRunError: The input doesn't fit, or the document doesn't build.
    :raises PyMongoError: The store isn't answering.
    """
    document = snapshot(record["document"])
    check_input(document, input)
    saved = await saved_documents(document, find)
    check_builds(document, saved)
    return saved


async def start_adk_run(
    embedding: Embedding,
    record: dict[str, Any],
    *,
    saved: dict[str, dict[str, Any]],
    input: Any,
    run_as: str,
    run_as_name: str,
    trigger: dict[str, Any],
) -> dict[str, str]:
    """
    Submit a run of an ADK workflow, as its record is now.

    :param record: The ADK workflow's record (``AgentStore``).
    :param saved: The saved ADK workflows' documents it runs (``prepare_run``).
    :param input: What the run starts with; checked already.
    :param run_as: The member it acts as: its ADK session's user.
    :param run_as_name: Their name, for its record.
    :param trigger: What started it: ``{"type": "manual", "by": <user>}``.
    :return: ``{"queue", "key", "session_id"}`` of its job and its ADK session.
    :raises EmbeddingError: The queue could not be reached.
    """
    run = uuid4()
    agent_id = record["_id"]
    key = f"{ADK_WORKFLOWS}.run:{agent_id}:{run.hex}"
    session_id = str(run)
    document = snapshot(record["document"])
    payload = {
        "tenant_id": record["organization_id"],
        "agent_id": agent_id,
        "revision": record["revision"],
        "name": str(document.get("name") or ""),
        "document": document,
        "saved": saved,
        "input": input,
        "session_id": session_id,
        "run_as": run_as,
        "run_as_name": run_as_name,
        "trigger": trigger,
    }
    await embedding.run_adk_workflow(
        tenant_id=record["organization_id"],
        key=key,
        labels={AGENT_LABEL: agent_id, SESSION_LABEL: session_id},
        payload=payload,
        requested_by={"id": run_as, "display_name": run_as_name},
    )
    return {"queue": ADK_WORKFLOWS, "key": key, "session_id": session_id}


def checked_answer(details: dict[str, Any], answer: Any) -> str:
    """
    A person's answer to a run's question (human input), as the decision's
    comment carries it.

    :param details: The question, as the run's approval has it: its
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
