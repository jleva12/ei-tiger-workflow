"""The skill creator: an ADK agent that writes a skill for the organization's
vault from what a person describes, in the background.

It knows Forge (what its agents and workflows can have: the Agents canvas's
node catalog) and looks at what the organization has with read-only tools:
its MCP servers and their tools, its knowledge bases (and searches them),
its workflows and what they take, its agents, its API credentials (names and
where they go, never secrets) and the skills it has already. So the skill it
writes names real tools and resources, and says plainly what the
organization would still need. It hands the skill in with ``save_skill``,
which holds it to the skill format (``forge_agent_runtime.skills``) and tells
it what to fix; the last skill it saved is the one kept.

A job (``forge_admin.models.SkillCreatorJob``) runs in this process, beside
the request that started it (:func:`run_job`); the vault lists it until it's
done, then the skill it saved. It runs on the assistant's models and its
calls count as the assistant's for the organization.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from forge_agent_runtime import SkillBundle, SkillFile
from forge_agent_runtime.catalog import CHAT_NODE_CATALOG
from forge_common.adk.usage import (
    MODEL_KEY,
    Attribution,
    PriceOf,
    UsagePlugin,
    UsageSink,
)
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.function_tool import FunctionTool
from google.genai import types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import undefer

from forge_admin.models import ApiCredential, KnowledgeBase, McpServer, Skill

logger = logging.getLogger(__name__)

#: The agent's (and its app's) name.
NAME = "skill_creator"
#: How long the model thinks: some, to plan the skill.
THINKING_LEVEL = "medium"
#: Seconds a job may take before it's given up.
CREATOR_SECONDS = 300.0
#: The most notes a job keeps.
MOST_NOTES = 5
NOTE_CHARS = 400
#: How much of a description or a passage a listing shows.
CLIP = 400
#: The most records a listing shows.
MOST = 50

CREATOR_INSTRUCTION = """\
You write skills for Forge, a platform where organizations build Google ADK
agents: chat agents people talk to, and LLM agent steps inside workflows.

# What a skill is

A skill (the Agent Skills format) is a folder an agent loads when a task
calls for it: a SKILL.md with a name and a description, then Markdown
instructions, and optionally files beside it. An agent sees every skill's
name and description from the start; it loads a skill's instructions
(load_skill) only when its description matches the task, and reads its other
files (load_skill_resource) only when the instructions point to them. So:

- The name: lowercase words joined by hyphens, at most 64 characters
  (e.g. triage-support-tickets).
- The description (at most 1024 characters) is what decides when the skill
  is used: say what it does AND when to use it, with the words a request
  would use. One or two sentences.
- The instructions: a short playbook. An overview line, then numbered steps
  the agent follows, the checks it makes, what it hands back (its format,
  with a short example when the shape matters), and what to do when
  something's missing or fails. Imperative and specific; no filler. Under
  500 lines.
- Long reference material (a field list, a policy, an API's endpoints, a
  template) goes in its own file, references/<name>.md or assets/<name>,
  and the instructions say when to read it, by that path.
- A script (scripts/<name>.py) only when the task needs exact computation
  the person asked for; agents run them only where code execution is on,
  and otherwise read them and follow them.

# Using what the organization has

Look before you write: list what's relevant with your tools (MCP servers and
their tools, knowledge bases, workflows, agents, credentials, the skills it
already has), and search a knowledge base when the skill should follow the
organization's own documents. Then:

- Name the real things: an MCP server's tools by their exact names
  ("the GitHub server's `create_issue` tool"), a workflow by its name and the
  input it takes, a knowledge base by its name. The agent using the skill has
  them only if it's given them as tools, so say which tools the skill needs at
  the top of the instructions ("Needs: …").
- Never invent servers, tools, workflows, documents, people or numbers. When
  the skill needs something the organization doesn't have, write it so the
  agent asks for it or says it can't, and add a note for the person.
- Don't repeat a skill the organization already has: build on it, or say
  so in a note.
- Never put secrets in a skill.

# Handing it in

Call save_skill with the whole skill: name, description, instructions (the
SKILL.md body, without frontmatter), its files (each a path and its text),
and notes for the person (what to check, what to connect, what you assumed;
at most five, short). If it answers with problems, fix them and call it
again. When it's saved, answer with one line saying what you made. Don't ask
the person anything: nobody is there to answer. Decide sensibly, and say
what you decided in the notes.
"""


class CreatorError(Exception):
    """The creator couldn't write the skill: the model failed, or saved nothing."""


@dataclass
class Workspace:
    """
    What the creator may read of the organization: only its own records.

    :ivar chat_agents: Its agents (``ChatAgentStore``); None without MongoDB.
    :ivar workflows: Its workflows (``AgentStore``); likewise.
    :ivar knowledge: Searches its knowledge bases
        (``forge_admin.knowledge.tools.OrganizationKnowledgeBases``); None
        when searching isn't set up.
    """

    organization_id: str
    sessions: async_sessionmaker[AsyncSession]
    chat_agents: Any = None
    workflows: Any = None
    knowledge: Any = None


@dataclass
class Created:
    """What the creator handed in."""

    skill: SkillBundle
    notes: list[str] = field(default_factory=list)
    model: str = ""


def _clip(text: object, most: int = CLIP) -> str:
    said = " ".join(str(text or "").split())
    return said if len(said) <= most else said[: most - 1] + "…"


def _fields(schema: object) -> list[str]:
    """A JSON Schema's top-level fields: ``name (type, required)``."""
    if not isinstance(schema, Mapping):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return []
    required = set(schema.get("required") or [])
    return [
        f"{name} ({(spec or {}).get('type', 'any') if isinstance(spec, Mapping) else 'any'}"
        f"{', required' if name in required else ''})"
        for name, spec in list(properties.items())[:20]
    ]


async def _records(store: Any, organization_id: str) -> list[Mapping[str, Any]]:
    if store is None:
        return []
    try:
        return list(await store.list(organization_id))
    except Exception:
        logger.warning("The skill creator couldn't list a store", exc_info=True)
        return []


def creator_tools(workspace: Workspace, handed_in: list[Created]) -> list[FunctionTool]:
    """
    The creator's tools: reading the organization, and handing the skill in.

    :param handed_in: Where ``save_skill`` keeps each skill it accepts.
    """
    org = workspace.organization_id

    async def list_tool_kinds() -> dict[str, Any]:
        """Lists the kinds of tool a Forge agent can be given (on the Agents
        canvas and in workflows' LLM agents), each with what it does."""
        return {
            "tool_kinds": [
                {"kind": kind.kind, "label": kind.label, "does": kind.summary}
                for kind in CHAT_NODE_CATALOG.kinds
                if kind.group == "tools" or kind.kind == "saved_agent"
            ]
        }

    async def list_mcp_servers() -> dict[str, Any]:
        """Lists the organization's MCP servers, each with its tools (their
        exact names and what they do), as last checked."""
        async with workspace.sessions() as session:
            rows = await session.scalars(
                select(McpServer)
                .where(McpServer.organization_id == org)
                .order_by(McpServer.name)
                .limit(MOST)
            )
            servers = [
                {
                    "name": row.name,
                    "description": _clip(row.description),
                    "tools": [
                        {
                            "name": str(tool.get("name")),
                            "does": _clip(tool.get("description"), 200),
                        }
                        for tool in (row.tools or [])[:60]
                        if isinstance(tool, Mapping)
                    ],
                }
                for row in rows
            ]
        return {"mcp_servers": servers}

    async def list_knowledge_bases() -> dict[str, Any]:
        """Lists the organization's knowledge bases: named sets of documents
        (or, for a system design one, code and how a system fits together)
        that agents search."""
        async with workspace.sessions() as session:
            rows = await session.scalars(
                select(KnowledgeBase)
                .where(KnowledgeBase.organization_id == org)
                .order_by(KnowledgeBase.name)
                .limit(MOST)
            )
            bases = [
                {
                    "name": row.name,
                    "description": _clip(row.description),
                    "kind": row.kind or "rag",
                }
                for row in rows
            ]
        return {"knowledge_bases": bases}

    async def search_knowledge_base(knowledge_base: str, query: str) -> dict[str, Any]:
        """Searches one of the organization's knowledge bases, by its name, for
        the passages that best match the query: to write a skill that follows
        the organization's own documents.

        Args:
          knowledge_base: The knowledge base's name, as list_knowledge_bases has it.
          query: What to look for, in a few words.
        """
        if workspace.knowledge is None:
            return {"error": "Searching knowledge bases isn't set up here."}
        async with workspace.sessions() as session:
            row = await session.scalar(
                select(KnowledgeBase).where(
                    KnowledgeBase.organization_id == org,
                    KnowledgeBase.name == knowledge_base,
                )
            )
        if row is None:
            return {"error": f"There's no knowledge base called {knowledge_base!r}."}
        try:
            passages = await workspace.knowledge.search(
                [row.id], query, organization_id=org, limit=5
            )
        except (LookupError, RuntimeError) as error:
            return {"error": str(error)}
        return {
            "passages": [
                {
                    "document": p.get("document"),
                    "where": p.get("location") or p.get("section"),
                    "text": _clip(p.get("text"), 1_200),
                }
                for p in passages
            ]
        }

    async def list_workflows() -> dict[str, Any]:
        """Lists the organization's workflows, each with what it does and the
        input its start takes: an agent can run one as a tool."""
        found = []
        for record in (await _records(workspace.workflows, org))[:MOST]:
            document = record.get("document") or {}
            start = next(
                (n for n in document.get("nodes") or [] if n.get("kind") == "start"),
                {},
            )
            found.append(
                {
                    "name": document.get("name"),
                    "description": _clip(document.get("description")),
                    "input": _fields((start.get("config") or {}).get("input_schema")),
                }
            )
        return {"workflows": found}

    async def list_agents() -> dict[str, Any]:
        """Lists the organization's agents (from the Agents page), each with
        what it's for and the tools it has."""
        found = []
        for record in (await _records(workspace.chat_agents, org))[:MOST]:
            document = record.get("document") or {}
            nodes = [n for n in document.get("nodes") or [] if isinstance(n, Mapping)]
            found.append(
                {
                    "name": document.get("name"),
                    "description": _clip(document.get("description")),
                    "tools": [
                        f"{n.get('name')} ({n.get('kind')})"
                        for n in nodes
                        if n.get("kind") not in ("agent", "sub_agent")
                    ][:20],
                }
            )
        return {"agents": found}

    async def list_credentials() -> dict[str, Any]:
        """Lists the organization's API credentials: how its HTTP and OpenAPI
        tools sign in to APIs, and the addresses each may be sent to. Never
        their secrets."""
        async with workspace.sessions() as session:
            rows = await session.scalars(
                select(ApiCredential)
                .where(ApiCredential.organization_id == org)
                .order_by(ApiCredential.name)
                .limit(MOST)
            )
            found = [
                {
                    "name": row.name,
                    "description": _clip(row.description),
                    "sent_to": list(row.urls or []),
                }
                for row in rows
            ]
        return {"credentials": found}

    async def list_skills() -> dict[str, Any]:
        """Lists the skills the organization already has, by name and description."""
        async with workspace.sessions() as session:
            rows = await session.execute(
                select(Skill.name, Skill.description)
                .where(Skill.organization_id == org)
                .order_by(Skill.name)
                .limit(200)
            )
            return {
                "skills": [
                    {"name": name, "description": _clip(description)}
                    for name, description in rows
                ]
            }

    async def read_skill(name: str) -> dict[str, Any]:
        """Reads one of the organization's skills whole: its instructions and files.

        Args:
          name: The skill's name, as list_skills has it.
        """
        async with workspace.sessions() as session:
            row = await session.scalar(
                select(Skill)
                .options(undefer(Skill.bundle))
                .where(Skill.organization_id == org, Skill.name == name)
            )
        if row is None:
            return {"error": f"There's no skill called {name!r}."}
        skill = SkillBundle.model_validate(row.bundle)
        return {
            "name": skill.name,
            "description": skill.description,
            "instructions": skill.instructions,
            "files": [
                {
                    "path": f.path,
                    "content": f.content[:8_000]
                    if f.encoding == "utf-8"
                    else "(binary)",
                }
                for f in skill.files[:30]
            ],
        }

    async def save_skill(
        name: str,
        description: str,
        instructions: str,
        files: list[dict[str, str]] | None = None,
        notes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Hands the skill in. Call it again with the whole skill to change it.

        Args:
          name: Lowercase words joined by hyphens, e.g. triage-support-tickets.
          description: What it does and when to use it (at most 1024 characters).
          instructions: The SKILL.md body: Markdown, without frontmatter.
          files: Its other files, each {"path": "references/fields.md", "content": "..."}.
          notes: For the person: what to check, connect or decide; at most five.
        """
        problems: list[str] = []
        made: list[SkillFile] = []
        for item in files or []:
            if not isinstance(item, Mapping):
                problems.append("each file is {path, content}")
                continue
            try:
                made.append(
                    SkillFile(
                        path=str(item.get("path") or ""),
                        content=str(item.get("content") or ""),
                    )
                )
            except ValueError as error:
                problems.append(_problem(error))
        body = re.sub(r"^---\n.*?\n---\n", "", instructions.strip(), flags=re.DOTALL)
        if problems:
            return {"saved": False, "problems": problems}
        try:
            skill = SkillBundle(
                name=name.strip(),
                description=description,
                instructions=body.strip(),
                files=made,
            )
        except ValueError as error:
            return {"saved": False, "problems": [_problem(error)]}
        if not skill.instructions:
            return {"saved": False, "problems": ["write its instructions"]}
        kept = [_clip(note, NOTE_CHARS) for note in notes or [] if str(note).strip()]
        handed_in.append(Created(skill, kept[:MOST_NOTES]))
        return {"saved": True, "name": skill.name, "files": len(skill.files)}

    return [
        FunctionTool(tool)
        for tool in (
            list_tool_kinds,
            list_mcp_servers,
            list_knowledge_bases,
            search_knowledge_base,
            list_workflows,
            list_agents,
            list_credentials,
            list_skills,
            read_skill,
            save_skill,
        )
    ]


def _problem(error: ValueError) -> str:
    errors = getattr(error, "errors", None)
    if callable(errors):
        return "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc') or ())}: "
            f"{str(e.get('msg') or '').removeprefix('Value error, ')}".lstrip(": ")
            for e in errors()
        )
    return str(error)


def _creator_instruction(_context: ReadonlyContext) -> str:
    # A provider, not a string: ADK would read {braces} in it as state.
    return CREATOR_INSTRUCTION


def _thinking(model: BaseLlm) -> Callable[[CallbackContext, LlmRequest], None]:
    def choose(_context: CallbackContext, request: LlmRequest) -> None:
        select_model = getattr(model, "select", None)
        if callable(select_model):
            select_model(request, None, THINKING_LEVEL)

    return choose


def creator_agent(model: BaseLlm, tools: Sequence[FunctionTool]) -> LlmAgent:
    return LlmAgent(
        name=NAME,
        description="Writes a skill for the organization's vault.",
        model=model,
        instruction=_creator_instruction,
        before_model_callback=_thinking(model),
        tools=list(tools),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def prompt(brief: str, revising: SkillBundle | None) -> str:
    """What the creator is sent: the person's description, and the skill to rework."""
    parts = ["# What the person wants", brief.strip()]
    if revising is not None:
        parts += [
            "# The skill to rework",
            "Keep its name unless the person asks otherwise. Its current SKILL.md and files:",
            revising.skill_md(),
            json.dumps(
                [
                    {"path": f.path, "content": f.content[:4_000]}
                    for f in revising.files
                    if f.encoding == "utf-8"
                ][:20],
                ensure_ascii=False,
            ),
        ]
    return "\n\n".join(parts)


async def create(
    model: BaseLlm,
    workspace: Workspace,
    *,
    brief: str,
    user_id: str,
    revising: SkillBundle | None = None,
    attribution: Attribution | None = None,
    usage: UsageSink | None = None,
    price: PriceOf | None = None,
) -> Created:
    """
    Has the creator write a skill.

    :param model: What it runs on: the assistant's models, or a test's.
    :param workspace: What it may read of the organization.
    :param brief: What the person described.
    :param user_id: Who asked.
    :param revising: The skill to rework; None for a new one.
    :return: The skill it saved last, its notes and its model.
    :raises CreatorError: It failed, or saved nothing.
    """
    handed_in: list[Created] = []
    plugins: Sequence[BasePlugin] = (
        [UsagePlugin(usage, attribution, price=price)]
        if usage is not None and attribution is not None
        else []
    )
    sessions = InMemorySessionService()
    agent = creator_agent(model, creator_tools(workspace, handed_in))
    runner = Runner(
        app=App(name=NAME, root_agent=agent, plugins=list(plugins)),
        session_service=sessions,
    )
    session = await sessions.create_session(
        app_name=NAME, user_id=user_id, session_id=uuid.uuid4().hex
    )
    message = types.Content(
        role="user", parts=[types.Part(text=prompt(brief, revising))]
    )
    model_name, stopped = "", ""
    try:
        async with aclosing(
            runner.run_async(
                user_id=user_id, session_id=session.id, new_message=message
            )
        ) as events:
            async for event in events:
                if event.error_code:
                    stopped = event.error_code
                    continue
                if not event.partial and event.author == NAME:
                    model_name = (event.custom_metadata or {}).get(
                        MODEL_KEY, ""
                    ) or model_name
    finally:
        await runner.close()
    if not handed_in:
        raise CreatorError(
            f"The model stopped without saving a skill ({stopped[:60]}); try again."
            if stopped
            else "The creator finished without saving a skill; try again, or describe it more."
        )
    created = handed_in[-1]
    created.model = model_name or getattr(model, "model", "") or ""
    return created


async def free_name(
    session: AsyncSession, organization_id: str, name: str, *, keep: str | None = None
) -> str:
    """:return: ``name``, or ``name-2``, ``name-3``…: one no other skill of the
    organization has."""
    taken = set(
        await session.scalars(
            select(Skill.name).where(
                Skill.organization_id == organization_id,
                Skill.name.like(f"{name[:58]}%"),
                *([Skill.id != keep] if keep else []),
            )
        )
    )
    if name not in taken:
        return name
    for n in range(2, 1_000):
        candidate = f"{name[: 64 - len(str(n)) - 1]}-{n}"
        if candidate not in taken:
            return candidate
    return f"{name[:55]}-{uuid.uuid4().hex[:8]}"


def describe_failure(error: Exception) -> str:
    """:return: Why a job failed, for the person: never raw internals."""
    if isinstance(error, CreatorError):
        return str(error)
    if isinstance(error, TimeoutError):
        return "The creator took too long; try again, or ask for a smaller skill."
    from forge_common.model_provider import ModelProviderAuthError
    from google.genai import errors as genai_errors

    if isinstance(error, genai_errors.APIError):
        return f"The model refused the request ({error.code} {error.status}): {error.message}"
    if isinstance(error, ModelProviderAuthError):
        return f"The creator couldn't sign in to the model's provider: {error}"
    if type(error).__module__.startswith("litellm"):
        return f"The model refused the request: {str(error)[:500]}"
    return "The creator ran into a problem. Try again."
