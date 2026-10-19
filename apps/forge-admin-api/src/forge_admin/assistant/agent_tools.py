"""The agent builder tools: what the assistant needs to understand an
organization's agents (the Agents page's chat agents) and build them with the
person, as the person.

Every tool calls the admin API's own routes as the conversation's user
(``person_api.py``): it reads only the agents of organizations they may read
(``organizations:read``), and makes, saves or tries one only where they may
manage them (``agents:manage``), exactly as the Agents builder does, each
change audited as theirs.

``edit_agent`` and ``write_instruction`` save the agent's draft, never a
published version: the person publishes it when they're ready, and the
builder's undo takes a change back. So they don't ask the person first; a
test does, since an agent's tools act on other systems. The web console's
builder loads the draft a change saved as it's told of the tool's answer
(its ``agent_id`` and ``revision``), so the canvas shows it at once.
"""

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI
from forge_agent_runtime.catalog import CHAT_NODE_CATALOG
from forge_agent_runtime.document import FORMAT, HANDS_OFF, TOOLS, WAYS
from forge_common.adk import ToolFailure
from google.adk.tools.tool_context import ToolContext

from forge_admin.assistant.agent_edits import (
    WAY_WORDS,
    EditRefused,
    apply_changes,
    label,
    problems_of,
    settings_of_kind,
)
from forge_admin.assistant.building_blocks import agent_block, building_blocks
from forge_admin.assistant.person_api import PersonApi
from forge_admin.assistant.route_tools import (
    RouteToolset,
    pick,
    quoted,
    slug_arg,
    uuid_arg,
)
from forge_admin.instruction_writer.brief import chat_agent_brief

logger = logging.getLogger(__name__)

READ_TOOLS = (
    "list_agents",
    "get_agent",
    "describe_agent_parts",
    "list_building_blocks",
    "read_api_spec",
)
CHANGE_TOOLS = ("create_agent", "edit_agent", "write_instruction", "test_agent")
#: The changes that ask the person first: a test acts on other systems.
CONFIRMED = frozenset({"test_agent"})
TOOL_NAMES = frozenset(READ_TOOLS + CHANGE_TOOLS)

#: An agent's ID, as the API makes them.
AGENT_ID = r"^ca_[a-z0-9]{6,40}$"
#: The most messages one test sends.
MOST_TEST_MESSAGES = 5
#: Seconds a test may take, every turn of it.
TEST_SECONDS = 240.0
#: Seconds the instruction writer may take.
WRITE_SECONDS = 150.0
#: The most characters of a value a test shows.
TEST_VALUE_CHARS = 1500

# Added to the specialist's instruction while it has these tools.
INSTRUCTION = """
Building agents:
You are the person's co-pilot for building agents on Forge's Agents canvas. \
Most people you help are business users, not programmers: they know the job \
the agent should do, not prompts, tools or JSON. You do the technical part \
for them and explain it in their terms.

How an agent works (tell the person in plain words when it helps):
- An agent from the Agents page is a small team on a canvas. Its chat agent \
(kind agent, exactly one) is who people talk to: its instruction (what it's \
told to do), description and model. Everything else hangs off an agent's two \
ways out:
  - tools: what it can call. A knowledge base (searches the organization's \
documents), a workflow (runs one of the organization's ADK workflows with an \
input and gets its result), an MCP server's tools, an HTTP tool (one API \
call the model fills in), an OpenAPI spec (each operation a tool), skills \
from the organization's vault, memory of earlier conversations, code \
execution (Python, for calculations), rich answers (cards, tables, charts \
and buttons in the chat), or another agent called like a tool that answers \
back (a sub-agent or saved agent).
  - agents (hands off to): a sub-agent, intake agent or saved agent that \
takes over. A sub-agent's mode says how: chat (takes the conversation over), \
task (does one task, asking the person if it must, then hands back) or \
single_turn (answers once).
- A sub-agent is another LLM agent of this one, with its own instruction and \
tools. A saved agent is another of the organization's agents from the Agents \
page, reused whole. An intake agent walks the person through a form you \
design (its fields, in any order they answer), then hands the record back; \
its on_complete way starts workflows with the record.
- What each part does and its settings: describe_agent_parts. What the \
organization has to plug in: list_building_blocks (its agents, workflows \
with their input fields, credentials, knowledge bases with how many \
documents are searchable, MCP servers with their tools, skills, agent \
permissions for access rules, models).

How you work:
- Start every request by reading the agent with get_agent: the one on the \
person's page (its chatAgentId parameter, or the chat_agent on screen) \
unless they name another. It's the truth; the person edits it too.
- When the person describes what they need, design the system first: who \
it's for and what it should do; what it needs from the organization (check \
list_building_blocks and use what exists, by its id: knowledge bases, \
workflows, MCP servers, credentials for APIs, other agents); whether one \
agent with tools does the job (usually) or specialists help (a sub-agent per \
distinct job with its own instruction and tools; an intake agent to collect \
a request; a hand-off when another agent should take over). Prefer the \
simplest design that works. Tell them the plan in a short numbered list in \
their words (what it will do, what it will use) and ask whether to build \
it, unless they asked for one small change. Ask one short question at a \
time for what only they know (which documents, which system, who may use \
it, what it must never do).
- Build with edit_agent: one call with every node and connection of a step \
of the plan (a new node's attach_to connects it to the agent that uses it). \
Name nodes for what they do ("Search HR policies", "Order lookup", "Billing \
specialist"): an agent's and an HTTP tool's name is what the model calls it \
by. Give a tool a description where it has one, saying when to use it: the \
model decides by it. Give every sub-agent a description: the agent above it \
decides by that when to use it. Never invent ids.
- Then write each agent's instruction with write_instruction (the chat \
agent, and each sub-agent), once its tools and hand-offs are connected, so \
it names them as the agent calls them; write it again after you change \
them. Tell it the person's goal in their words, who the agent serves, its \
tone, what it should do step by step, and what it must never do. Don't \
write instructions into edit_agent's settings yourself.
- Settings beyond that: a model only when the person wants one (empty runs \
on the default); ask_person on when an agent should stop to ask a question \
with a form; a workflow tool's version null (its latest published; "draft" \
only to try an unpublished one); confirm on for tools that send, pay, change \
or delete something, so a person approves each call; access (who may use \
an agent: {"permission": <agent permission id>}, or {"any"|"all": [rules]}) \
only when the person asks to limit it.
- After each change, read its problems: they'd stop the agent working. Fix \
them before you say it's done. Its advice would make it work better: fix \
what you can, tell the person the rest.
- When something the person needs isn't in the organization (no knowledge \
base with those documents, no credential for that API, no MCP server for \
that system, no workflow for that process), say so plainly and where to add \
it (the Knowledge bases, Credentials, MCP servers or Workflows page), and \
build the rest. Never put secrets (API keys, passwords, tokens) in settings: \
HTTP and OpenAPI tools sign in with one of the organization's credentials, \
by id. read_api_spec shows what an API's OpenAPI spec offers before you add \
it.
- The person sees each change on their canvas at once and can undo it: \
don't ask before a change they asked for.
- Offer to try it with test_agent once it's built: it asks the person \
first, then sends what a real user would say to the draft and reports each \
reply and the tools it called. Put every message of a try in one call \
(separately true for separate scenarios), so the person approves once. \
Explain what went wrong plainly and offer the fix.
- create_agent makes a new agent, when the person asks for one or the \
design needs another agent to reuse; build it with edit_agent and \
write_instruction, and give the person its link.
- Report in two parts. "For the person:" in plain words, for someone who \
isn't technical: what the agent does now, each part by its name as the \
canvas shows it (never its id) and what it's for ("Search HR policies: \
looks answers up in the HR handbook"), then what's left for them (sign in \
to an MCP server, add a credential, try it in the Test panel, publish). No \
ids, JSON or setting names there. "Details:" the ids and settings that \
matter, for the assistant, briefly.
- Publishing stays with the person: tell them to publish from the builder's \
toolbar once it's ready; people chat with its latest published version.
"""

#: What each kind of node is, in a word or two, for get_agent.
_IS = {
    "agent": "the chat agent people talk to",
    "sub_agent": "a sub-agent",
    "intake_agent": "an intake agent",
    "saved_agent": "another of the organization's agents",
}


class AgentToolset(RouteToolset):
    """
    The agent builder tools, calling the admin API as the person.

    :param api: The admin API, called as the person.
    :param confirm_changes: Ask the person to confirm a test first.
    :param kwargs: :class:`RouteToolset`'s.
    """

    READ_TOOLS = READ_TOOLS
    CHANGE_TOOLS = CHANGE_TOOLS
    CONFIRMED = CONFIRMED
    STATUS_FIXES = {
        409: [
            "The agent was saved since you read it (the person may be editing it "
            "too): read it again with get_agent and make your changes on that.",
        ],
        422: ["Fix it as the reason says, then call again."],
    }
    TIMEOUTS = {"test_agent": TEST_SECONDS + 30.0, "write_instruction": WRITE_SECONDS}

    def __init__(
        self, api: PersonApi, *, confirm_changes: bool = True, **kwargs: Any
    ) -> None:
        kwargs.setdefault("max_result_chars", 60_000)
        super().__init__(api, confirm_changes=confirm_changes, **kwargs)

    async def confirmation_hint(
        self, name: str, args: Mapping[str, Any], tool_context: ToolContext
    ) -> str | None:
        if name != "test_agent":
            return None
        record = await self._read_quietly(
            tool_context,
            lambda: _agent(args.get("organization_id"), args.get("agent_id")),
        )
        return test_question(record, args.get("messages"), args.get("separately"))

    # ------------------------------------------------------------- reading

    async def list_agents(
        self, organization_id: str, tool_context: ToolContext
    ) -> list[dict[str, Any]]:
        """
        Lists the organization's agents from the Agents page.

        :param organization_id: The organization, by ID.
        :return: Each agent: its id, name, what it's for, status (draft,
            published, published+draft) and the inputs its chat takes.
        """
        found = await self._call(tool_context, "GET", _agents(organization_id))
        return [agent_block(record) for record in found or []]

    async def get_agent(
        self, organization_id: str, agent_id: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        Reads one agent as the builder has it: its draft (or its published
        version when it has no draft), each node with its settings, how
        they're connected, and what's wrong with it.

        :param organization_id: The organization, by ID.
        :param agent_id: The agent, by ID (ca_...).
        :return: The agent: revision (edits name it), nodes (id, kind, name,
            what it is, settings; empty settings left out), connections
            (from, way, to), problems (what would stop it working; empty when
            it works), advice (what would make it work better) and its link.
        """
        record = await self._call(
            tool_context, "GET", _agent(organization_id, agent_id)
        )
        blocks = await self._blocks(tool_context, organization_id)
        found = problems_of(record.get("document") or {}, blocks, agent_id=agent_id)
        return agent_view(record, found)

    async def describe_agent_parts(
        self, tool_context: ToolContext, kinds: list[str] | None = None
    ) -> dict[str, Any]:
        """
        Describes the kinds of node an agent can have: agents and the tools
        they call.

        :param kinds: Kinds to describe in full (their settings, as a JSON
            Schema with each setting's description, and what a new one
            starts with). Leave out for the list of every kind.
        :return: Without kinds: every kind (kind, label, what it does, how an
            agent uses it, its ways out). With them: those kinds' settings.
        """
        return describe_parts(kinds)

    async def list_building_blocks(
        self, organization_id: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        Lists what the organization has for its agents to use: its other
        agents (with the inputs each takes), its workflows (with their input
        fields), its credentials (which APIs each signs in to), knowledge
        bases (with how many documents are searchable), MCP servers (with
        their tools), skills, the agent permissions access rules name, its
        team UI components (which Rich answers pick by key), and the models
        agents can run on.

        :param organization_id: The organization, by ID.
        :return: Each list, by kind; a list the person may not read says why.
        """
        return await self._blocks(tool_context, organization_id)

    async def read_api_spec(
        self,
        organization_id: str,
        tool_context: ToolContext,
        url: str = "",
        spec: str = "",
        credential: str = "",
    ) -> dict[str, Any]:
        """
        Reads an OpenAPI spec as an OpenAPI tool would: the operations an
        agent could call (by the names it would call them) and the
        credentials the API takes.

        :param organization_id: The organization, by ID.
        :param url: Where the spec is (https://...); or give spec.
        :param spec: The spec itself, JSON or YAML, when it has no URL.
        :param credential: One of the organization's credentials, by id,
            sent with the request for the spec when the API needs it.
        :return: Its title, servers, operations (name, method, path,
            summary) and the credentials it takes.
        """
        found = await self._call(
            tool_context,
            "POST",
            f"/organizations/{uuid_arg(organization_id, 'organization_id')}/openapi/operations",
            json={
                "source": "inline" if spec.strip() and not url.strip() else "url",
                "url": url.strip(),
                "spec": spec,
                "credential": credential,
            },
        )
        found = dict(found or {})
        found["operations"] = [
            pick(operation, ("name", "method", "path", "summary"))
            for operation in found.get("operations") or []
        ][:120]
        return found

    # ------------------------------------------------------------- changing

    async def create_agent(
        self,
        organization_id: str,
        name: str,
        tool_context: ToolContext,
        description: str = "",
    ) -> dict[str, Any]:
        """
        Makes a new agent on the Agents page: a draft with just its chat
        agent, to build with edit_agent and write_instruction.

        :param organization_id: The organization, by ID.
        :param name: Its name, as the Agents page lists it ("HR helpdesk").
        :param description: What it's for, in a sentence.
        :return: Its id, its link and the id of its chat agent node.
        """
        if not name.strip():
            raise ToolFailure("Give the agent a name.")
        entry = CHAT_NODE_CATALOG.find("agent")
        assert entry is not None
        document = {
            "format": FORMAT,
            "id": "ca_new",
            "name": name.strip(),
            "description": description,
            "organization_id": uuid_arg(organization_id, "organization_id"),
            "nodes": [
                {
                    "id": "agent",
                    "kind": "agent",
                    "name": name.strip(),
                    "config": {**entry.defaults, "description": description},
                    "outputs": list(WAYS["agent"]),
                }
            ],
            "edges": [],
            "layout": {"agent": {"x": 0, "y": 0}},
        }
        record = await self._call(
            tool_context, "POST", _agents(organization_id), json={"document": document}
        )
        return {
            "agent_id": record["id"],
            "organization_id": record.get("organization_id"),
            "revision": record.get("revision"),
            "chat_agent_node": "agent",
            "link": _link(organization_id, record["id"]),
        }

    async def edit_agent(
        self,
        organization_id: str,
        agent_id: str,
        changes: list[dict[str, Any]],
        tool_context: ToolContext,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """
        Changes the agent's draft, every change at once: the person sees it
        on their canvas, and can undo it. A published agent without a draft
        gets one, from its latest version.

        :param organization_id: The organization, by ID.
        :param agent_id: The agent, by ID.
        :param changes: The changes, in order, each {"action": ..., ...}:
            add_node {kind, name, settings?, id?, attach_to?: {agent, way}}
            (its settings over what a new node of the kind starts with;
            attach_to connects that agent's way out to it);
            update_node {id, name?, settings?} (each setting given replaces
            the one it has, a list or object whole);
            remove_node {id} (its connections go too);
            connect {source, way?, target} (source an agent; way tools: it
            calls the target, agents: it hands off to it, on_complete: an
            intake agent starts the workflow; worked out when only one fits);
            disconnect {source, way?, target?};
            update_agent {name?, description?} (the agent's own).
        :param revision: The revision the changes were made from, as
            get_agent read it; a save since then refuses them. Leave out to
            change the draft as it is now.
        :return: What each change did, the agent's new revision, the nodes
            added, and its problems and advice now.
        """
        record, started = await self._draft(
            tool_context, organization_id, agent_id, revision
        )
        try:
            edited = apply_changes(record["document"], changes)
        except EditRefused as refused:
            raise ToolFailure(
                str(refused),
                [
                    "Fix that change and call again with the whole batch: none of it "
                    "was saved.",
                    "describe_agent_parts with the kind gives its settings.",
                ],
            ) from None
        _rename_entry(record["document"], edited.document)
        saved = await self._save(
            tool_context, organization_id, agent_id, record, edited.document
        )
        blocks = await self._blocks(tool_context, organization_id)
        found = problems_of(saved.get("document") or {}, blocks, agent_id=agent_id)
        answer = _changed(saved, organization_id, edited.done, started)
        answer["added"] = edited.added
        answer["problems"] = found.problems
        if found.advice:
            answer["advice"] = found.advice
        return answer

    async def write_instruction(
        self,
        organization_id: str,
        agent_id: str,
        node_id: str,
        ask: str,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """
        Writes one of the agent's agents' instruction with Forge's
        instruction writer, which knows the exact names the agent calls its
        tools and hand-offs by, and saves it to the draft: the person sees
        it on their canvas, and can undo it. It reworks the instruction the
        agent has, or writes a new one.

        :param organization_id: The organization, by ID.
        :param agent_id: The agent, by ID.
        :param node_id: The chat agent's or a sub-agent's node, by id.
        :param ask: What the agent should do, or what to change: the
            person's goal in their words, who it serves, its tone, its steps,
            what it must never do.
        :return: The instruction written, the writer's notes, the agent's
            new revision and its problems now.
        """
        record = await self._call(
            tool_context, "GET", _agent(organization_id, agent_id)
        )
        document = record.get("document") or {}
        try:
            brief = chat_agent_brief(
                document,
                node_id,
                operations=await self._operations(
                    tool_context, organization_id, document
                ),
            )
        except ValueError as error:
            raise ToolFailure(
                str(error),
                ["get_agent lists its agents: the nodes of kind agent or sub_agent."],
            ) from None
        node = next(n for n in document.get("nodes") or [] if n.get("id") == node_id)
        org = uuid_arg(organization_id, "organization_id")
        written = await self._call(
            tool_context,
            "POST",
            f"/organizations/{org}/helpers/instruction_writer",
            json={
                "ask": ask,
                "current": (node.get("config") or {}).get("instruction") or "",
                "brief": brief.model_dump(mode="json", exclude_none=True),
            },
        )
        instruction = ((written or {}).get("result") or {}).get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ToolFailure(
                "The instruction writer answered without an instruction; try again."
            )
        # Saved on the draft the writer worked from: a change since refuses it.
        read_from = record.get("revision") if record.get("has_draft") else None
        record, started = await self._draft(
            tool_context, organization_id, agent_id, read_from
        )
        try:
            edited = apply_changes(
                record["document"],
                [
                    {
                        "action": "update_node",
                        "id": node_id,
                        "settings": {"instruction": instruction},
                    }
                ],
            )
        except EditRefused as refused:
            raise ToolFailure(str(refused)) from None
        saved = await self._save(
            tool_context, organization_id, agent_id, record, edited.document
        )
        blocks = await self._blocks(tool_context, organization_id)
        found = problems_of(saved.get("document") or {}, blocks, agent_id=agent_id)
        answer = _changed(
            saved, organization_id, [f"Wrote the instruction of {label(node)}"], started
        )
        answer["instruction"] = instruction
        answer["notes"] = list((written or {}).get("notes") or [])
        answer["problems"] = found.problems
        return answer

    async def test_agent(
        self,
        organization_id: str,
        agent_id: str,
        messages: list[str],
        tool_context: ToolContext,
        separately: bool = False,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Tries the agent with its draft (or its latest published version when
        it has no draft), as a person chatting would: every message of one
        try goes in this one call, so the person approves once. Its tools
        act on other systems as in a real conversation, so this asks the
        person first.

        :param organization_id: The organization, by ID.
        :param agent_id: The agent, by ID.
        :param messages: What a person would say: 1 to 5 messages.
        :param separately: Each message starts a conversation of its own
            (separate scenarios: a return, a billing question); otherwise
            they're one conversation, in order.
        :param state: The inputs its chat takes, when it declares some.
        :return: Each conversation's turns: the message, the agents that
            answered, the tools they called (with their arguments and
            answers or errors), the reply, and an error when the turn
            failed. A turn where it stopped to ask the person something, or
            failed, ends its conversation.
        """
        wanted = [str(message).strip() for message in messages if str(message).strip()]
        if not wanted:
            raise ToolFailure("Give at least one message to send.")
        wanted = wanted[:MOST_TEST_MESSAGES]
        record = await self._call(
            tool_context, "GET", _agent(organization_id, agent_id)
        )
        app = f"{record['id']}@draft" if record.get("has_draft") else str(record["id"])
        conversations = []
        for said in [[message] for message in wanted] if separately else [wanted]:
            conversations.append(await self._converse(tool_context, app, said, state))
        return {
            "tried": "its draft"
            if record.get("has_draft")
            else "its latest published version",
            "conversations": conversations,
        }

    async def _converse(
        self,
        tool_context: ToolContext,
        app: str,
        messages: list[str],
        state: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        # One conversation of the person's own with the agent, as its Test
        # panel has them, a turn per message.
        user_id = str(tool_context.user_id or "")
        session = await self._call(
            tool_context,
            "POST",
            f"/runtime/apps/{app}/users/{slug_arg(user_id, 'user')}/sessions",
            json={"state": state} if state else {},
        )
        turns = []
        for message in messages:
            stream = await self._call(
                tool_context,
                "POST",
                "/runtime/run_sse",
                json={
                    "appName": app,
                    "userId": user_id,
                    "sessionId": session["id"],
                    "newMessage": {"role": "user", "parts": [{"text": message}]},
                    "streaming": False,
                },
                text=True,
            )
            turn = turn_report(message, stream or "")
            turns.append(turn)
            if turn.get("error") or turn.get("waiting_for_the_person"):
                break
        return turns

    # ------------------------------------------------------------- helpers

    async def _blocks(
        self, tool_context: ToolContext, organization_id: str
    ) -> dict[str, Any]:
        return await building_blocks(
            lambda method, path: self._call(tool_context, method, path),
            organization_id,
            access=True,
            ui_components=True,
        )

    async def _draft(
        self,
        tool_context: ToolContext,
        organization_id: str,
        agent_id: str,
        revision: int | None,
    ) -> tuple[dict[str, Any], bool]:
        # The agent's draft, started from its latest version when it has none.
        path = _agent(organization_id, agent_id)
        record = await self._call(tool_context, "GET", path)
        if not record.get("has_draft"):
            return await self._call(
                tool_context, "POST", f"{path}/versions", json={}
            ), True
        if revision is not None and int(revision) != int(record["revision"]):
            raise ToolFailure(
                f"The agent is at revision {record['revision']} now, not {revision}: "
                "the person (or another change) changed it since you read it. Read it "
                "again with get_agent and make your changes on that."
            )
        return record, False

    async def _save(
        self,
        tool_context: ToolContext,
        organization_id: str,
        agent_id: str,
        record: Mapping[str, Any],
        document: dict[str, Any],
    ) -> dict[str, Any]:
        saved: dict[str, Any] = await self._call(
            tool_context,
            "PUT",
            f"{_agent(organization_id, agent_id)}/draft",
            json={"document": document, "revision": record["revision"]},
        )
        return saved

    async def _operations(
        self,
        tool_context: ToolContext,
        organization_id: str,
        document: Mapping[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        # Each OpenAPI tool's operations, for the writer to name; one whose
        # spec can't be read is left without.
        found: dict[str, list[dict[str, Any]]] = {}
        for node in document.get("nodes") or []:
            if not isinstance(node, dict) or node.get("kind") != "openapi":
                continue
            config = node.get("config") or {}
            inline = config.get("source") == "inline"
            try:
                spec = await self.read_api_spec(
                    organization_id,
                    tool_context,
                    url="" if inline else str(config.get("url") or ""),
                    spec=str(config.get("spec") or "") if inline else "",
                    credential=str(config.get("credential") or ""),
                )
            except ToolFailure:
                continue
            found[str(node.get("id"))] = list(spec.get("operations") or [])
        return found


# ------------------------------------------------------------- views


def test_question(record: Any, messages: Any, separately: Any) -> str:
    """
    What the person is asked before test_agent tries an agent: which agent,
    with how many messages, and that its tools really act.

    :param record: The agent, as the API answers it; None when it couldn't
        be read.
    """
    record = record if isinstance(record, Mapping) else {}
    agent = quoted(_name_of(record), "the agent")
    said = [m for m in messages if str(m).strip()] if isinstance(messages, list) else []
    count = min(len(said), MOST_TEST_MESSAGES)
    if not record:
        version = "It"
    elif record.get("has_draft"):
        version = "Its draft"
    else:
        version = "Its published version"
    if count > 1 and separately is True:
        asked = f"{count} separate messages"
        how = f"Each starts a new conversation. {version} answers as in a real chat"
    elif count > 1:
        asked = f"a conversation of {count} messages"
        how = f"They're sent in order. {version} answers as in a real chat"
    else:
        asked = "this message"
        how = f"{version} answers as in a real chat"
    return (
        f"Try {agent} with {asked}?\n"
        f"{how}, and the tools it uses act on real systems, so what they do "
        "really happens."
    )


def agent_view(record: Mapping[str, Any], found: Any) -> dict[str, Any]:
    """
    :param record: The agent, as the API reads it.
    :param found: What's wrong with it (``agent_edits.problems_of``).
    :return: The agent, as get_agent answers it.
    """
    document = record.get("document") or {}
    nodes = [node for node in document.get("nodes") or [] if isinstance(node, dict)]
    names = {node.get("id"): node.get("name") for node in nodes}
    view: dict[str, Any] = {
        "agent_id": record.get("id"),
        "organization_id": record.get("organization_id"),
        "name": document.get("name"),
        "description": document.get("description") or "",
        "revision": record.get("revision"),
        "status": record.get("status"),
        "editing": (
            f"its draft (version {record.get('draft_version')})"
            if record.get("has_draft")
            else "its published version: a change starts a new draft"
        ),
        "nodes": [
            {
                "id": node.get("id"),
                "kind": node.get("kind"),
                "name": node.get("name"),
                "is": _IS.get(str(node.get("kind")), "a tool"),
                "settings": _filled(node.get("config") or {}),
            }
            for node in nodes
        ],
        "connections": [
            {
                "from": f"{edge.get('source')} ({names.get(edge.get('source'), '?')})",
                "way": f"{edge.get('source_output')} ("
                + WAY_WORDS.get(str(edge.get("source_output")), "?")
                + ")",
                "to": f"{edge.get('target')} ({names.get(edge.get('target'), '?')})",
            }
            for edge in document.get("edges") or []
            if isinstance(edge, dict)
        ],
        "problems": list(found.problems),
    }
    if found.advice:
        view["advice"] = list(found.advice)
    if record.get("organization_id") and record.get("id"):
        view["link"] = _link(str(record["organization_id"]), str(record["id"]))
    return view


def describe_parts(kinds: list[str] | None) -> dict[str, Any]:
    """
    :return: The kinds of node an agent can have: a list of every one, or
        the settings of those named.
    """
    if not kinds:
        return {
            "kinds": [
                {
                    "kind": entry.kind,
                    "label": entry.label,
                    "does": entry.summary,
                    "used_as": _used_as(entry.kind),
                    **(
                        {
                            "ways_out": [
                                f"{way} ({WAY_WORDS[way]})" for way in WAYS[entry.kind]
                            ]
                        }
                        if WAYS.get(entry.kind)
                        else {}
                    ),
                }
                for entry in CHAT_NODE_CATALOG.kinds
            ],
            "next": "Call again with kinds for their settings.",
        }
    found: dict[str, Any] = {}
    for kind in kinds:
        entry = CHAT_NODE_CATALOG.find(kind)
        if entry is None:
            found[kind] = {
                "unknown": "No such kind; call with no arguments for the list."
            }
            continue
        found[kind] = {
            "label": entry.label,
            "used_as": _used_as(kind),
            "settings": settings_of_kind(kind),
            "new_node_settings": json.loads(json.dumps(entry.defaults)),
        }
    return found


def _used_as(kind: str) -> str:
    entry = CHAT_NODE_CATALOG.find(kind)
    if entry is None or kind == "agent":
        return "where conversations start; nothing uses it"
    ways = []
    if entry.tool:
        ways.append(f"a tool an agent calls (way {TOOLS})")
    if entry.handed_to:
        ways.append(f"an agent another hands off to (way {HANDS_OFF})")
    if kind == "adk_workflow":
        ways.append("what an intake agent starts when it completes (way on_complete)")
    return "; or ".join(ways)


def turn_report(message: str, stream: str) -> dict[str, Any]:
    """
    :param message: What was sent.
    :param stream: The turn's Server-Sent Events, as the runtime streamed them.
    :return: The turn, as test_agent reports it.
    """
    replies: list[str] = []
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    authors: list[str] = []
    turn: dict[str, Any] = {"message": message}
    for event in _events(stream):
        author = event.get("author")
        if isinstance(author, str) and author != "user" and author not in authors:
            authors.append(author)
        if event.get("errorMessage") or event.get("errorCode"):
            turn["error"] = str(event.get("errorMessage") or event.get("errorCode"))
        long_running = set(event.get("longRunningToolIds") or [])
        for part in ((event.get("content") or {}).get("parts")) or []:
            if not isinstance(part, dict) or part.get("thought"):
                continue
            if isinstance(part.get("text"), str) and part["text"].strip():
                replies.append(part["text"].strip())
            call = part.get("functionCall")
            if isinstance(call, dict):
                if (
                    call.get("name") == "adk_request_input"
                    or call.get("id") in long_running
                ):
                    turn["waiting_for_the_person"] = _clipped(call.get("args"))
                    continue
                if call.get("name") in ("transfer_to_agent", "finish_task"):
                    continue
                shown = {
                    "tool": call.get("name"),
                    "by": author,
                    "args": _clipped(call.get("args")),
                }
                calls.append(shown)
                if call.get("id"):
                    by_id[str(call["id"])] = shown
            response = part.get("functionResponse")
            if isinstance(response, dict) and str(response.get("id")) in by_id:
                by_id[str(response["id"])]["answer"] = _clipped(
                    response.get("response")
                )
    turn["answered_by"] = authors
    if calls:
        turn["tool_calls"] = calls
    turn["reply"] = "\n\n".join(replies)
    return turn


def _events(stream: str) -> list[dict[str, Any]]:
    found = []
    for line in stream.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(event, dict):
            found.append(event)
    return found


def _changed(
    saved: Mapping[str, Any], organization_id: str, done: list[str], started: bool
) -> dict[str, Any]:
    # What every change answers: the builder reloads the draft by its first three.
    answer: dict[str, Any] = {
        "agent_id": saved["id"],
        "organization_id": uuid_arg(organization_id, "organization_id"),
        "revision": saved["revision"],
        "done": done,
    }
    if started:
        answer["started_draft"] = (
            f"It was published with no draft: this started draft version "
            f"{saved.get('draft_version')}."
        )
    return answer


def _rename_entry(before: Mapping[str, Any], after: dict[str, Any]) -> None:
    # The chat agent goes by the agent's name, as the builder names them both:
    # renaming the agent renames it too, while they matched.
    if after.get("name") == before.get("name"):
        return
    for node in after.get("nodes") or []:
        if node.get("kind") == "agent" and node.get("name") == before.get("name"):
            node["name"] = after.get("name")


def _filled(config: Mapping[str, Any]) -> dict[str, Any]:
    # Settings with a value: empty ones say nothing.
    return {
        key: value for key, value in config.items() if value not in ("", [], {}, None)
    }


def _clipped(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= TEST_VALUE_CHARS else text[:TEST_VALUE_CHARS] + "…"


def _name_of(record: Mapping[str, Any]) -> Any:
    document = record.get("document")
    return record.get("name") or (document or {}).get("name")


# ------------------------------------------------------------- paths


def _agent_id(value: str | None) -> str:
    if not re.fullmatch(AGENT_ID, value or ""):
        raise ToolFailure(f"agent_id must be an agent's ID (ca_...), not {value!r}")
    return str(value)


def _agents(organization_id: str) -> str:
    return f"/organizations/{uuid_arg(organization_id, 'organization_id')}/chat-agents"


def _agent(organization_id: str, agent_id: str) -> str:
    return f"{_agents(organization_id)}/{_agent_id(agent_id)}"


def _link(organization_id: str, agent_id: str) -> str:
    # The builder's page, as the web console routes it.
    return f"/organizations/{organization_id}/chat-agents/{agent_id}"


def toolset(app: FastAPI, **kwargs: Any) -> AgentToolset | None:
    """
    The agent builder tools, calling ``app``'s routes as each conversation's
    person.

    :param app: The admin API's application.
    :param kwargs: :class:`AgentToolset`'s options.
    :return: The toolset, or None without ``jwt_secret``, which the person's
        tokens are signed with.
    """
    if app.state.settings.jwt_secret is None:
        logger.warning(
            "FORGE_ADMIN_JWT_SECRET isn't set: the assistant can't build agents as "
            "the people it talks to"
        )
        return None
    return AgentToolset(PersonApi.of_app(app), **kwargs)


__all__ = ["INSTRUCTION", "TOOL_NAMES", "AgentToolset", "toolset"]
