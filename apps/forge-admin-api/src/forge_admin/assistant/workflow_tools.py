"""The workflow builder tools: what the assistant needs to understand an
organization's ADK workflows and build them with the person, as the person.

Every tool calls the admin API's own routes as the conversation's user
(``person_api.py``): it reads only the workflows of organizations they may
read (``organizations:read``), and saves a draft, or test-runs one, only
where they may manage them (``agents:manage``), exactly as the workflow
builder does, each change audited as theirs.

``edit_workflow`` saves the workflow's draft, never its published version:
the person publishes it when they're ready, and the builder's undo takes a
change back. So edits don't ask the person first; a test run does, since
the workflow's steps act on other systems. The web console's builder loads
the draft an edit saved as it's told of the edit's answer (its
``workflow_id`` and ``revision``), so the canvas shows it at once.
"""

import asyncio
import copy
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI
from forge_agent_runtime.catalog import CHAT_NODE_CATALOG
from forge_common.adk import ToolFailure
from forge_task_adk_workflows.graph import NODE_CATALOG, outputs_of
from google.adk.tools.tool_context import ToolContext

from forge_admin.assistant.building_blocks import building_blocks, workflow_block
from forge_admin.assistant.person_api import PersonApi
from forge_admin.assistant.route_tools import (
    ApiRefused,
    RouteToolset,
    pick,
    quoted,
    slug_arg,
    uuid_arg,
)
from forge_admin.assistant.workflow_edits import (
    ACTIONS,
    EditRefused,
    apply_changes,
    format_schema,
    schema_def,
    settings_of_kind,
)

logger = logging.getLogger(__name__)

READ_TOOLS = (
    "list_workflows",
    "get_workflow",
    "describe_node_kinds",
    "list_building_blocks",
    "list_workflow_runs",
    "get_workflow_run",
)
CHANGE_TOOLS = ("edit_workflow", "test_workflow")
#: The changes that ask the person first: a test run acts on other systems.
CONFIRMED = frozenset({"test_workflow"})
TOOL_NAMES = frozenset(READ_TOOLS + CHANGE_TOOLS)

#: A workflow's ID, as the API makes them.
WORKFLOW_ID = r"^ag_[a-z0-9]{6,40}$"
#: A run's ID.
RUN_ID = r"^[0-9a-f]{32}$"
#: The run statuses a run is still going in.
GOING = frozenset({"queued", "running"})
#: Seconds a test run is waited for, at most.
MAX_WAIT = 120
#: The most characters of a value a run's step shows.
STEP_VALUE_CHARS = 1500

# Added to the specialist's instruction while it has these tools.
INSTRUCTION = """
Building ADK workflows:
You are the person's co-pilot for building ADK workflows on Forge's canvas. \
Most people you help are business users, not programmers: they know the job \
the workflow should do, not JSONata or JSON Schema. You know both, and you \
do the technical part for them, explaining it in their terms.

How a workflow works (tell the person this in plain words when it helps):
- A workflow runs from its Start, step by step along the lines (edges) \
between nodes. Each node does one thing and hands its result on along one of \
its ways out (next; success / error; true / false; a case; approved / \
rejected...). The run ends at an End (succeeded or failed), or where a way \
leads nowhere.
- The Start says what a run is given: its input_schema (a JSON Schema \
object; each property is a field the person fills in to run it, with a \
description and, where it fits, an enum of choices). Steps read it as \
input.<field>.
- Every setting that's an expression (JSONata) or a template (text with \
{{ }}) reads the run's data: input.<field> (the run's input), previous (what \
the node before handed on), steps.<node id>.output (any node that ran \
before, by its id; .error for one that took its Error way), state, and in a \
loop's or for-each's body, its item (by its item name) and index. Write \
JSONata, e.g. input.customer.email, previous.body.items[status="open"], \
$count(steps.fetch_orders.output.body), "Hello " & input.name.
- LLM nodes are agents: an instruction (a template), a model, optional \
tools, and an output_schema. When later steps need fields from an LLM's \
answer, give it an output_schema (an object with the fields they need, each \
with a description), so steps.<id>.output.<field> is there for them; without \
one it hands on free text. An agent from the Agents page runs with \
source "agent", its agent's id, a message (template) and its inputs.
- What each kind of node does, its settings and what it hands on: call \
describe_node_kinds (no arguments for the list; with kinds for their \
settings). What the organization has to plug in (its agents from the Agents \
page, other workflows, credentials for APIs, knowledge bases, MCP servers, \
skills, models): call list_building_blocks.

How you work:
- Start every request by reading the workflow with get_workflow: the one on \
the person's page (its agentId parameter, or the workflow on screen) unless \
they name another. Its nodes, settings, connections and problems are the \
truth; what you remember from earlier may be stale, since the person edits \
it too.
- When the person describes a goal, work out the steps first: what the run \
is given, each step in order, what each needs from earlier ones, where it \
can branch or fail, and what it ends with. For a workflow of more than two \
or three nodes, or when you're unsure what they want, tell them the plan in \
a short numbered list in their words and ask whether to build it. Ask one \
short question at a time when something only they know is missing (which \
system, which credential, what should happen when X fails).
- Build with edit_workflow: one call with every change of a step of the \
plan (add nodes with their settings and connect them in the same call; a new \
node's after connects it to the node before it). Give nodes short names that \
say what they do ("Look up the customer", "Draft the reply"). Give new nodes \
an id that says what they are (snake_case: fetch_customer), so later \
expressions read well: steps.fetch_customer.output.body.
- Wire data on purpose: each setting reads exactly the fields it needs, from \
the node that has them; prefer steps.<id>.output.<field> over previous when \
the node isn't the one right before. Hold every LLM whose answer later steps \
read to an output_schema. Fill a Saved workflow's inputs from the run's data \
to fit that workflow's input fields. Fill an Agents-page agent's inputs to fit \
what it declares.
- Plan for failure the way a careful engineer would: an HTTP request's Error \
way (or status ways for codes like 404 or 401, with codes such as "404", \
"401, 403", "4xx"); an approval before anything that sends, pays or \
deletes; an End with outcome failed where the run should stop.
- After each edit, read what it reports: its problems are what would stop a \
run. Fix them before you say it's done. Dropped connections are lines it \
removed because their way out is gone: reconnect what should be.
- The person sees every change on their canvas at once and can undo it; you \
needn't ask before an edit they asked for.
- Report in two parts. "For the person:" in plain words, for someone who \
isn't technical: each step by its name as the canvas shows it (never its \
id), what it does and where it leads ("If GitHub has no such repository, \
the run ends with 'Repository not found'"), where data comes from in words \
("the number of stars, straight from GitHub"), in a short list; then what's \
left for them (pick a credential, fill in a secret, publish). No ids, \
expressions, JSON or settings there. "Details:" the ids, expressions and \
settings that matter, for the assistant, briefly.
- Offer to test it with test_workflow once it builds: it asks the person \
first, runs the draft on an input you make up from its input fields (or one \
they give), and reports each step's result or error. Read the failed step's \
error and what it was called with, explain the cause plainly, and offer the \
fix.
- To explain why a run failed, use list_workflow_runs and get_workflow_run.
- Never invent ids: use the ones get_workflow and list_building_blocks \
return. Never put secrets (API keys, passwords, tokens) in settings: an HTTP \
request signs in with one of the organization's credentials, by id; when \
there's none for that API, tell the person to add one on the Credentials \
page.
- Publishing stays with the person: tell them to publish from the \
builder's toolbar once the draft is ready.
"""


class WorkflowToolset(RouteToolset):
    """
    The workflow builder tools, calling the admin API as the person.

    :param api: The admin API, called as the person.
    :param confirm_changes: Ask the person to confirm a test run first.
    :param kwargs: :class:`RouteToolset`'s.
    """

    READ_TOOLS = READ_TOOLS
    CHANGE_TOOLS = CHANGE_TOOLS
    CONFIRMED = CONFIRMED
    STATUS_FIXES = {
        409: [
            "The workflow was saved since you read it (the person may be editing it "
            "too): read it again with get_workflow and make your changes on that.",
        ],
        422: [
            "Fix the workflow as the reason says (it names the node and setting), "
            "then call again.",
        ],
    }
    TIMEOUTS = {"test_workflow": MAX_WAIT + 30.0}

    def __init__(
        self, api: PersonApi, *, confirm_changes: bool = True, **kwargs: Any
    ) -> None:
        kwargs.setdefault("max_result_chars", 60_000)
        super().__init__(api, confirm_changes=confirm_changes, **kwargs)

    async def confirmation_hint(
        self, name: str, args: Mapping[str, Any], tool_context: ToolContext
    ) -> str | None:
        if name != "test_workflow":
            return None
        record = await self._read_quietly(
            tool_context,
            lambda: _workflow(args.get("organization_id"), args.get("workflow_id")),
        )
        return test_run_question(record, args.get("input"))

    # ------------------------------------------------------------- reading

    async def list_workflows(
        self, organization_id: str, tool_context: ToolContext
    ) -> list[dict[str, Any]]:
        """
        Lists the organization's ADK workflows.

        :param organization_id: The organization, by ID.
        :return: Each workflow: its id, name, description, status (draft,
            published, published+draft), and the input fields its start
            asks for.
        """
        found = await self._call(tool_context, "GET", _workflows(organization_id))
        return [workflow_block(record) for record in found or []]

    async def get_workflow(
        self, organization_id: str, workflow_id: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        Reads one workflow as the builder has it: its draft (or its published
        version when it has no draft), every node with its settings and ways
        out, how they're connected, and what would stop it running.

        :param organization_id: The organization, by ID.
        :param workflow_id: The workflow, by ID (ag_...).
        :return: The workflow: revision (edits name it), its input fields,
            nodes (id, kind, name, ways_out, settings; empty settings left
            out), connections (from, way, to), problems (why a run would be
            refused; empty when it would run) and publish_problems.
        """
        record = await self._call(
            tool_context, "GET", _workflow(organization_id, workflow_id)
        )
        check = await self._check(tool_context, organization_id, workflow_id)
        return workflow_view(record, check)

    async def describe_node_kinds(
        self,
        tool_context: ToolContext,
        kinds: list[str] | None = None,
        tool_kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Describes the kinds of node a workflow can have, and the tools an LLM
        node can be given.

        :param kinds: Node kinds to describe in full (their settings, as a
            JSON Schema with each setting's description, and what a new one
            starts with). Leave out for the list of every kind.
        :param tool_kinds: LLM tool kinds to describe in full, likewise.
        :return: Without kinds: every node kind (kind, label, group, what it
            does and hands on, its ways out) and tool kind. With them: those
            kinds' settings.
        """
        return describe_kinds(kinds, tool_kinds)

    async def list_building_blocks(
        self, organization_id: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        Lists what the organization has for a workflow's nodes to use: its
        agents from the Agents page (with the inputs each declares), its other
        workflows (with their input fields), its credentials (which APIs each
        signs in to), knowledge bases, MCP servers (with their tools),
        skills, and the models LLM nodes can run on.

        :param organization_id: The organization, by ID.
        :return: Each list, by kind; a list the person may not read says why.
        """
        return await building_blocks(
            lambda method, path: self._call(tool_context, method, path),
            organization_id,
        )

    async def list_workflow_runs(
        self,
        organization_id: str,
        workflow_id: str,
        tool_context: ToolContext,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Lists a workflow's latest runs, newest first.

        :param organization_id: The organization, by ID.
        :param workflow_id: The workflow, by ID.
        :param limit: How many, 1 to 20.
        :return: Each run: its id, status, version (draft or a number),
            when it was created and its error when it failed.
        """
        page = await self._call(
            tool_context,
            "GET",
            f"/organizations/{uuid_arg(organization_id, 'organization_id')}/adk-runs",
            params={
                "agent_id": _workflow_id(workflow_id),
                "limit": max(1, min(20, int(limit))),
            },
        )
        return [
            pick(run, ("id", "status", "version", "created_at", "finished_at", "error"))
            for run in (page or {}).get("items") or []
        ]

    async def get_workflow_run(
        self, organization_id: str, run_id: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        Reads one run: its status, input, result or error, and each step it
        reached: what it was handed (input), what it worked out and sent
        (called_with: a saved workflow's inputs, an HTTP request), what it
        handed on (output) or why it failed (error), and the tools its agents
        called.

        :param organization_id: The organization, by ID.
        :param run_id: The run, by ID.
        :return: The run.
        """
        return await self._run_view(tool_context, organization_id, run_id)

    # ------------------------------------------------------------- changing

    async def edit_workflow(
        self,
        organization_id: str,
        workflow_id: str,
        changes: list[dict[str, Any]],
        tool_context: ToolContext,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """
        Changes the workflow's draft, every change at once: the person sees it
        on their canvas, and can undo it. A published workflow without a
        draft gets one, from its latest version.

        :param organization_id: The organization, by ID.
        :param workflow_id: The workflow, by ID.
        :param changes: The changes, in order, each {"action": ..., ...}:
            add_node {kind, name, settings?, id?, after?: {node, way}} (its
            settings over what a new node of the kind starts with; after
            connects that node's way to it);
            update_node {id, name?, settings?} (each setting given replaces
            the one it has, a list or object whole);
            remove_node {id} (its connections go too);
            connect {source, way?, target} (way: one of the source's ways
            out; its only one when left out);
            disconnect {source, way?, target?};
            update_workflow {name?, description?}.
        :param revision: The revision the changes were made from, as
            get_workflow read it; a save since then refuses them. Leave out
            to change the draft as it is now.
        :return: What each change did, the workflow's new revision, the
            nodes added, connections dropped because their way out is gone,
            and its problems now (why a run would be refused).
        """
        org = uuid_arg(organization_id, "organization_id")
        path = _workflow(organization_id, workflow_id)
        record = await self._call(tool_context, "GET", path)
        started_draft = False
        if not record.get("has_draft"):
            record = await self._call(tool_context, "POST", f"{path}/versions", json={})
            started_draft = True
        elif revision is not None and int(revision) != int(record["revision"]):
            raise ToolFailure(
                f"The workflow is at revision {record['revision']} now, not {revision}: "
                "the person (or another edit) changed it since you read it. Read it "
                "again with get_workflow and make your changes on that."
            )
        try:
            edited = apply_changes(record["document"], changes)
        except EditRefused as refused:
            raise ToolFailure(
                str(refused),
                [
                    "Fix that change and call again with the whole batch: none of it "
                    "was saved.",
                    "describe_node_kinds with the kind gives its settings.",
                ],
            ) from None
        saved = await self._call(
            tool_context,
            "PUT",
            f"{path}/draft",
            json={"document": edited.document, "revision": record["revision"]},
        )
        check = await self._check(tool_context, organization_id, workflow_id)
        answer: dict[str, Any] = {
            "workflow_id": saved["id"],
            "organization_id": org,
            "revision": saved["revision"],
            "done": edited.done,
            "added": edited.added,
            "problems": check.get("problems", []),
        }
        if edited.dropped:
            answer["dropped_connections"] = edited.dropped
        if started_draft:
            answer["started_draft"] = (
                f"It was published with no draft: these changes started draft "
                f"version {saved.get('draft_version')}."
            )
        return answer

    async def test_workflow(
        self,
        organization_id: str,
        workflow_id: str,
        tool_context: ToolContext,
        input: dict[str, Any] | None = None,
        wait_seconds: int = 90,
    ) -> dict[str, Any]:
        """
        Runs the workflow's draft once, as a test, and waits for it: its steps
        act on other systems as a real run would, so this asks the person
        first.

        :param organization_id: The organization, by ID.
        :param workflow_id: The workflow, by ID.
        :param input: The run's input, fitting its start's input fields.
        :param wait_seconds: How long to wait for it, 5 to 120; a run still
            going then is reported as it is (read it later with
            get_workflow_run).
        :return: The run: its status, result or error, and each step it
            reached (input, called_with, output or error, tool calls).
        """
        run = await self._call(
            tool_context,
            "POST",
            f"{_workflow(organization_id, workflow_id)}/runs",
            json={"input": input or {}, "version": "draft"},
        )
        run_id = str(run["id"])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(5, min(MAX_WAIT, int(wait_seconds)))
        delay = 1.0
        while loop.time() < deadline:
            current = await self._call(
                tool_context, "GET", _run(organization_id, run_id)
            )
            if current.get("status") not in GOING:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 4.0)
        return await self._run_view(tool_context, organization_id, run_id)

    # ------------------------------------------------------------- helpers

    async def _check(
        self, tool_context: ToolContext, organization_id: str, workflow_id: str
    ) -> dict[str, Any]:
        try:
            found = await self._call(
                tool_context,
                "GET",
                f"{_workflow(organization_id, workflow_id)}/check",
            )
            return found or {}
        except ApiRefused as refused:
            return {"problems": [f"It couldn't be checked: {refused.reason}"]}

    async def _run_view(
        self, tool_context: ToolContext, organization_id: str, run_id: str
    ) -> dict[str, Any]:
        path = _run(organization_id, run_id)
        run = await self._call(tool_context, "GET", path)
        steps = await self._call(tool_context, "GET", f"{path}/steps")
        return run_view(run, steps)


# ------------------------------------------------------------- views


def test_run_question(record: Any, run_input: Any) -> str:
    """
    What the person is asked before test_workflow runs a workflow's draft:
    which workflow, and that its steps really act.

    :param record: The workflow, as the API answers it; None when it
        couldn't be read.
    :param run_input: The run's input, as the model gave it.
    """
    record = record if isinstance(record, Mapping) else {}
    document = record.get("document")
    workflow = quoted((document or {}).get("name"), "this workflow")
    given = "with the input below" if run_input else "with no input"
    return (
        f"Test-run {workflow}?\n"
        f"Its draft runs once {given}. Every step runs for real, so anything a "
        "step does, like sending a message or changing a record, really happens."
    )


def workflow_view(
    record: Mapping[str, Any], check: Mapping[str, Any]
) -> dict[str, Any]:
    """
    :param record: The workflow, as the API reads it.
    :param check: Its check (``…/check``).
    :return: The workflow, as get_workflow answers it.
    """
    document = record.get("document") or {}
    nodes = [node for node in document.get("nodes") or [] if isinstance(node, dict)]
    names = {node.get("id"): node.get("name") for node in nodes}
    start = next((node for node in nodes if node.get("kind") == "start"), None)
    view: dict[str, Any] = {
        "workflow_id": record.get("id"),
        "organization_id": record.get("organization_id"),
        "name": document.get("name"),
        "description": document.get("description") or "",
        "revision": record.get("revision"),
        "status": record.get("status"),
        "editing": (
            f"its draft (version {record.get('draft_version')})"
            if record.get("has_draft")
            else "its published version: an edit starts a new draft"
        ),
        "input_fields": ((start or {}).get("config") or {}).get("input_schema") or {},
        "nodes": [
            {
                "id": node.get("id"),
                "kind": node.get("kind"),
                "name": node.get("name"),
                "ways_out": ways_out(node),
                "settings": _filled(node.get("config") or {}),
            }
            for node in nodes
        ],
        "connections": [
            {
                "from": f"{edge.get('source')} ({names.get(edge.get('source'), '?')})",
                "way": edge.get("source_output"),
                "to": f"{edge.get('target')} ({names.get(edge.get('target'), '?')})",
            }
            for edge in document.get("edges") or []
            if isinstance(edge, dict)
        ],
        "problems": list(check.get("problems") or []),
    }
    if check.get("publish_problems"):
        view["publish_problems"] = list(check["publish_problems"])
    unconnected = _unconnected(nodes, document.get("edges") or [])
    if unconnected:
        view["ways_leading_nowhere"] = unconnected
    return view


def ways_out(node: Mapping[str, Any]) -> list[str]:
    """:return: A node's ways out, each ``id`` or ``id (label)`` when its label says more."""
    entry = NODE_CATALOG.find(node.get("kind"))
    labels: dict[str, str] = {}
    if entry is not None:
        labels = {output.id: output.label for output in entry.outputs}
        if entry.rows is not None:
            for row in (node.get("config") or {}).get(entry.rows.setting) or []:
                if isinstance(row, dict) and row.get("id"):
                    labels[row["id"]] = str(row.get(entry.rows.label) or "")
    found = []
    for way in outputs_of(dict(node)):
        label = labels.get(way, "")
        found.append(way if not label or label.lower() == way else f"{way} ({label})")
    return found


def run_view(run: Mapping[str, Any], steps: Mapping[str, Any]) -> dict[str, Any]:
    """
    :param run: A run, as the API reads it.
    :param steps: Its steps (``…/steps``).
    :return: The run, as the run tools answer it: the steps it reached.
    """
    reached = [
        step
        for step in (steps or {}).get("steps") or []
        if step.get("status") != "not_reached"
    ]
    view = pick(
        run,
        (
            "id",
            "status",
            "version",
            "input",
            "result",
            "error",
            "created_at",
            "finished_at",
        ),
    )
    if run.get("pause"):
        view["waiting_for"] = pick(run["pause"], ("kind", "reason"))
    view["steps"] = [
        {
            **pick(step, ("id", "name", "kind", "status")),
            **{
                key: _clipped(step[key])
                for key in ("input", "called_with", "output", "error")
                if step.get(key) not in (None, "", [], {})
            },
            **(
                {
                    "tool_calls": [
                        {
                            **pick(call, ("name", "status", "error")),
                            "args": _clipped(call.get("args")),
                        }
                        for call in step["calls"]
                    ]
                }
                if step.get("calls")
                else {}
            ),
        }
        for step in reached
    ]
    return view


def describe_kinds(
    kinds: list[str] | None, tool_kinds: list[str] | None
) -> dict[str, Any]:
    """
    :return: The node kinds (and LLM tool kinds): a list of every one, or
        the settings of those named.
    """
    tools = [kind for kind in CHAT_NODE_CATALOG.kinds if kind.workflow_tool]
    if not kinds and not tool_kinds:
        return {
            "node_kinds": [
                {
                    "kind": entry.kind,
                    "label": entry.label,
                    "group": entry.group,
                    "does": settings_of_kind(entry.kind).get("description")
                    or entry.summary,
                    "ways_out": [output.id for output in entry.outputs]
                    + (
                        [f"one per {entry.rows.setting} row, before these"]
                        if entry.rows
                        else []
                    ),
                }
                for entry in NODE_CATALOG.kinds
            ],
            "llm_tool_kinds": [
                {"kind": kind.kind, "label": kind.label, "does": kind.summary}
                for kind in tools
            ],
            "next": "Call again with kinds (and tool_kinds) for their settings.",
        }
    found: dict[str, Any] = {}
    for kind in kinds or []:
        entry = NODE_CATALOG.find(kind)
        if entry is None:
            found[kind] = {
                "unknown": "No such node kind; call with no arguments for the list."
            }
            continue
        schema = settings_of_kind(kind)
        found[kind] = {
            "label": entry.label,
            "ways_out": [output.id for output in entry.outputs],
            **(
                {"row_ways": f"one way out per row of {entry.rows.setting}, by its id"}
                if entry.rows
                else {}
            ),
            "settings": _resolved(schema),
            "new_node_settings": copy.deepcopy(entry.defaults),
        }
    named = {kind.kind: kind for kind in tools}
    for kind in tool_kinds or []:
        tool = named.get(kind)
        schema = schema_def(f"tool_config_{kind}")
        if tool is None:
            found[f"tool:{kind}"] = {
                "unknown": "No such tool kind; call with no arguments for the list."
            }
            continue
        found[f"tool:{kind}"] = {
            "label": tool.label,
            "does": tool.summary,
            "as_a_tool": "An LLM node's settings.tools entry: "
            f'{{"id": <unique>, "kind": "{kind}", "name": <name>, "config": <these settings>}}.',
            "settings": _resolved(schema),
        }
    return found


# ------------------------------------------------------------- shaping


def _filled(config: Mapping[str, Any]) -> dict[str, Any]:
    # Settings with a value: empty ones say nothing.
    return {
        key: value for key, value in config.items() if value not in ("", [], {}, None)
    }


def _unconnected(nodes: list[dict[str, Any]], edges: list[Any]) -> list[str]:
    # Ways out nothing leads on from, but an End's and the ways people leave
    # open on purpose (an Error way, a status way: those end quietly).
    taken = {
        (edge.get("source"), edge.get("source_output"))
        for edge in edges
        if isinstance(edge, dict)
    }
    found = []
    for node in nodes:
        if node.get("kind") in ("end",):
            continue
        for way in outputs_of(dict(node)):
            if (node.get("id"), way) not in taken and way in (
                "next",
                "success",
                "true",
            ):
                found.append(f"{node.get('id')} ({node.get('name')}) by {way}")
    return found


def _resolved(schema: Mapping[str, Any]) -> dict[str, Any]:
    # A settings schema with the definitions it refers to alongside.
    defs = format_schema()["$defs"]
    wanted: dict[str, Any] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name not in wanted and name in defs:
                    wanted[name] = defs[name]
                    visit(defs[name])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema)
    return {**schema, **({"$defs": wanted} if wanted else {})}


def _clipped(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= STEP_VALUE_CHARS else text[:STEP_VALUE_CHARS] + "…"


# ------------------------------------------------------------- paths


def _workflow_id(value: str | None) -> str:
    if not re.fullmatch(WORKFLOW_ID, value or ""):
        raise ToolFailure(
            f"workflow_id must be a workflow's ID (ag_...), not {value!r}"
        )
    return str(value)


def _workflows(organization_id: str) -> str:
    return f"/organizations/{uuid_arg(organization_id, 'organization_id')}/agents"


def _workflow(organization_id: str, workflow_id: str) -> str:
    return f"{_workflows(organization_id)}/{_workflow_id(workflow_id)}"


def _run(organization_id: str, run_id: str) -> str:
    if not re.fullmatch(RUN_ID, run_id or ""):
        raise ToolFailure(
            f"run_id must be a run's ID (32 hex characters), not {run_id!r}"
        )
    org = uuid_arg(organization_id, "organization_id")
    return f"/organizations/{org}/adk-runs/{slug_arg(run_id, 'run_id')}"


def toolset(app: FastAPI, **kwargs: Any) -> WorkflowToolset | None:
    """
    The workflow builder tools, calling ``app``'s routes as each
    conversation's person.

    :param app: The admin API's application.
    :param kwargs: :class:`WorkflowToolset`'s options.
    :return: The toolset, or None without ``jwt_secret``, which the person's
        tokens are signed with.
    """
    if app.state.settings.jwt_secret is None:
        logger.warning(
            "FORGE_ADMIN_JWT_SECRET isn't set: the assistant can't build workflows "
            "as the people it talks to"
        )
        return None
    return WorkflowToolset(PersonApi.of_app(app), **kwargs)


__all__ = ["ACTIONS", "INSTRUCTION", "TOOL_NAMES", "WorkflowToolset", "toolset"]
