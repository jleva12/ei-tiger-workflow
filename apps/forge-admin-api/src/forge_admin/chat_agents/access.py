"""Who may use an organization's agents and run its workflows: its agent permissions.

Chat agents: each agent on an agent's canvas (the chat agent, its
sub-agents, intake agents and saved agents) may have a rule on its node
saying which of the organization's agent permissions someone needs to use
it (``config.access``, ``forge_agent_runtime.access``). The runtime checks
it as the agent runs, for whoever the call is for
(:class:`OrganizationAccess`), so a coordinator may let someone in while a
sub-agent of it doesn't. A rule is part of the version it's in, but an old
version, or the draft, is never open to more people than the live one:
each of its agents is held to the rules the live version takes to reach
what that agent is and the tools it has, matched by what they are (the
organization's workflows, servers, credentials, …) rather than by node
(:func:`as_strict_as_live`).

Workflows: a workflow's record may say which permissions let someone run it,
any one of them (``access.permission_ids``, set from its builder; not
versioned, so it holds for every version at once), checked as its run
starts (:func:`check_agent_use`).

Someone holds a permission through a company group their sign-in names
linked to it (forge_admin.agent_access), or, for an API key, by being linked
to it itself. Those who manage the organization's agents (``agents:manage``)
may use them all. Nobody unknown holds any: an anonymous call, or a chat
page's visitor. A chat agent's workflow tool runs for the chat's caller,
and the agents a workflow runs use what its runner held as the run started.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import casbin
from forge_agent_runtime.access import (
    SETTING,
    Caller,
    Rule,
    RuleError,
    all_of,
    any_of,
    parse_rule,
    permissions_of,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.agent_access.policy import holds_any, team_enforcer
from forge_admin.auth.access import allows
from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.models import AgentPermission

#: What lets someone use any of the organization's agents.
MANAGE = "agents:manage"


class AgentUseRefused(Exception):
    """Someone may not use an agent: the message says which, and what it needs."""


@dataclass(frozen=True)
class AgentUser:
    """
    Who's using an organization's agents.

    :param subject: A user's ID, an API key's (``apikey:<id>``), or None
        for someone unknown.
    :param groups: The company groups their sign-in names.
    :param manager: They manage the organization's agents (``agents:manage``
        there), so they may use any of them.
    """

    subject: str | None
    groups: tuple[str, ...] = ()
    manager: bool = False


#: Someone unknown: no restricted agent is theirs to use.
ANONYMOUS = AgentUser(None)

# Who the current call of an agent is for, which its workflow tools' runs
# are checked for: the runtime sets it once it has let the caller in.
_current: ContextVar[AgentUser] = ContextVar(
    "forge_admin_agent_user", default=ANONYMOUS
)


def set_agent_user(user: AgentUser) -> None:
    """Record who the rest of this call of an agent is for."""
    _current.set(user)


def current_agent_user() -> AgentUser:
    """:return: Who this call of an agent is for; someone unknown when unsaid."""
    return _current.get()


async def agent_user(
    enforcer: casbin.AsyncEnforcer,
    organization_id: str,
    subject: str | None,
    groups: Iterable[str] = (),
) -> AgentUser:
    """
    :return: Who's using the organization's agents, whether they manage
        them read from the site's policy (reloaded first).
    """
    if subject is None:
        return ANONYMOUS
    await enforcer.load_policy()
    manager = allows(enforcer, subject, MANAGE, f"org:{organization_id}")
    return AgentUser(subject, tuple(groups), manager)


def required_permissions(record: Mapping[str, Any]) -> list[int]:
    """
    :return: The IDs of the agent permissions that let someone run a
        workflow (its record's), any one of them; none when anyone may.
        A chat agent's record kept them too before its nodes had rules
        (:func:`as_strict_as_live` reads those).
    """
    access = record.get("access") or {}
    return [int(i) for i in access.get("permission_ids") or []]


# Chat agents' rules ---------------------------------------------------------


async def caller_of(
    session: AsyncSession,
    organization_id: str,
    user: AgentUser,
    wanted: Iterable[int],
) -> Caller:
    """
    :param wanted: The agent permissions a rule names, by ID.
    :return: Who's using the organization's agents, as their rules see
        them: which of those they hold, and the permissions' names (a
        deleted one, or another organization's, has none, and nobody holds it).
    """
    ids = sorted(set(wanted))
    names: dict[int, str] = {}
    if ids:
        found = await session.scalars(
            select(AgentPermission).where(
                AgentPermission.organization_id == organization_id,
                AgentPermission.id.in_(ids),
            )
        )
        names = {p.id: p.key for p in found}
    if user.manager:
        return Caller(frozenset(names), names, everything=True)
    if user.subject is None:
        return Caller(names=names, known=False)
    enforcer = await team_enforcer(session, organization_id)
    held = frozenset(
        i
        for i, key in names.items()
        if holds_any(enforcer, organization_id, user.subject, [key], user.groups)
    )
    return Caller(held, names)


class OrganizationAccess:
    """
    The hosted runtime's check of agents' rules (``RuntimeServices.access``):
    what whoever the current call is for (:func:`current_agent_user`) holds
    of the organization's agent permissions, read as each agent starts.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def __call__(
        self, wanted: frozenset[int], *, organization_id: str | None
    ) -> Caller:
        user = current_agent_user()
        if organization_id is None:
            return Caller(known=user.subject is not None)
        async with self.sessions() as session:
            return await caller_of(session, organization_id, user, wanted)


#: The agents on a canvas, whose nodes may have rules.
AGENT_LIKE = frozenset({"agent", "sub_agent", "intake_agent", "saved_agent"})
#: Where a document keeps the rules its nodes are held to besides their own
#: (``ChatAgentDocument.required_rules``).
REQUIRED = "required_rules"
#: The most ways through a canvas followed, so a tangled one can't hold the
#: request up: a rule nobody holds guards what lies past it.
MAX_WAYS = 2000
#: What a rule that isn't one is read as: one nobody holds.
NOBODY = {"permission": 0}


def _nodes(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [n for n in document.get("nodes") or [] if isinstance(n, dict)]


def _rule_of(node: Mapping[str, Any]) -> Rule | None:
    try:
        return parse_rule((node.get("config") or {}).get(SETTING))
    except RuleError:
        # One that isn't a rule fails the build: kept as it is, it does.
        return (node.get("config") or {}).get(SETTING)


def rules_in(documents: Iterable[Mapping[str, Any]]) -> frozenset[int]:
    """:return: The agent permissions the rules in these documents name, their dependencies' too."""
    found: set[int] = set()
    pending = list(documents)
    while pending:
        document = pending.pop()
        pending.extend((document.get("dependencies") or {}).values())
        for node in _nodes(document):
            rule = _rule_of(node)
            if isinstance(rule, Mapping):
                found |= permissions_of(rule)
        # And those it's held to besides (as_strict_as_live).
        for rule in (document.get(REQUIRED) or {}).values():
            if isinstance(rule, Mapping):
                found |= permissions_of(rule)
    return frozenset(found)


def entry_rule(document: Mapping[str, Any]) -> Rule | None:
    """:return: The rule on a document's chat agent node: who may start a conversation with it."""
    for node in _nodes(document):
        if node.get("kind") == "agent":
            return _rule_of(node)
    return None


def _checked_rule(node: Mapping[str, Any]) -> Rule | None:
    """:return: A node's rule; one that isn't a rule as one nobody holds."""
    try:
        return parse_rule((node.get("config") or {}).get(SETTING))
    except RuleError:
        return NOBODY


def _origin(url: Any) -> str:
    """:return: Where a URL (or a URL template) calls: its scheme and host, as written."""
    text = str(url or "")
    scheme, written, rest = text.partition("://")
    if not written:
        return text
    for mark in "/?#":
        rest = rest.split(mark, 1)[0]
    return f"{scheme.lower()}://{rest.lower()}"


def resources_of(node: Mapping[str, Any]) -> set[str]:
    """
    :return: What a node gives the agents that reach it, by what each is
        rather than by the node's ID (which an older version, or the draft,
        may not share): the agent itself, the organization's agents,
        workflows, MCP servers, credentials, knowledge bases and skills it
        uses, where it calls.
    """
    config = node.get("config") or {}
    kind = node.get("kind")
    found = {f"node:{node.get('id')}"}
    if kind == "agent":
        found.add("entry")
    elif kind == "saved_agent":
        found.add(f"agent:{config.get('agent')}")
    elif kind == "adk_workflow":
        found.add(f"workflow:{config.get('workflow')}")
    elif kind == "mcp":
        found.add(f"mcp:{config.get('server') or _origin(config.get('url'))}")
    elif kind in ("http_tool", "openapi"):
        if config.get("url"):
            found.add(f"http:{_origin(config.get('url'))}")
    elif kind == "knowledge_base":
        found |= {f"knowledge_base:{i}" for i in config.get("knowledge_bases") or []}
    elif kind == "skills":
        found |= {f"skill:{i}" for i in config.get("skills") or []}
    elif kind in ("code_execution", "memory"):
        found.add(str(kind))
    if config.get("credential"):
        found.add(f"credential:{config.get('credential')}")
    return found


def _ways(document: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """:return: What each node's ways out lead to, by its ID."""
    nodes = {str(n.get("id")): n for n in _nodes(document)}
    ways: dict[str, list[dict[str, Any]]] = {}
    for edge in document.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        target = nodes.get(str(edge.get("target")))
        if target is not None:
            ways.setdefault(str(edge.get("source")), []).append(target)
    return ways


def guarded_resources(document: Mapping[str, Any]) -> dict[str, Rule | None]:
    """
    :return: Each resource someone may reach through the document's chat
        agent, and the rule that takes: over every way to it from the chat
        agent (any of them), the rules of the agents along that way (all of
        them). None: anyone may.
    """
    entry = next((n for n in _nodes(document) if n.get("kind") == "agent"), None)
    if entry is None:
        return {}
    ways = _ways(document)
    reached: dict[str, list[Rule | None]] = {}
    seen: set[tuple[str, str]] = set()
    pending: list[tuple[dict[str, Any], Rule | None]] = [(entry, None)]
    while pending:
        node, above = pending.pop()
        rule = (
            all_of(above, _checked_rule(node))
            if node.get("kind") in AGENT_LIKE
            else above
        )
        key = (str(node.get("id")), json.dumps(rule, sort_keys=True))
        if key in seen:
            continue
        if len(seen) >= MAX_WAYS:
            rule = NOBODY
        seen.add(key)
        for resource in resources_of(node):
            reached.setdefault(resource, []).append(rule)
        if node.get("kind") in AGENT_LIKE and len(seen) < MAX_WAYS:
            pending.extend(
                (target, rule) for target in ways.get(str(node.get("id")), [])
            )
    return {resource: any_of(*rules) for resource, rules in reached.items()}


def stricter(
    document: Mapping[str, Any],
    live: Mapping[str, Any] | None,
    legacy: Sequence[int] = (),
) -> dict[str, Rule]:
    """
    What an agent's version (or its draft) is held to, so it's never open to
    more people than its latest published version: each of its agents, to
    the rules the live version takes to reach what that agent itself is, or
    the tools it has (:func:`resources_of`), whatever its node's ID; and its
    chat agent, to the live one's rule and the record's.

    :param live: The agent's latest published version's document, when
        ``document`` isn't it.
    :param legacy: The permissions its record says let someone use it, any
        one of them: from before its nodes had rules.
    :return: The rules its agents' nodes are held to besides their own, by
        node ID (``ChatAgentDocument.required_rules``).
    """
    guards = guarded_resources(live) if live is not None else {}
    ways = _ways(document)
    required: dict[str, Rule] = {}
    for node in _nodes(document):
        if node.get("kind") not in AGENT_LIKE:
            continue
        gives = resources_of(node).union(
            *(
                resources_of(target)
                for target in ways.get(str(node.get("id")), [])
                if target.get("kind") not in AGENT_LIKE
            )
        )
        rule = all_of(*(guards[r] for r in sorted(gives) if r in guards))
        if legacy and node.get("kind") == "agent":
            rule = all_of(rule, {"any": [{"permission": int(i)} for i in legacy]})
        if rule is not None:
            required[str(node.get("id"))] = dict(rule)
    return required


def with_record_rule(
    document: Mapping[str, Any], record: Mapping[str, Any]
) -> dict[str, Any]:
    """
    :return: The document with what its record says lets someone use it
        (from before its nodes had rules) on its chat agent node, when that
        node has no rule of its own: so the builder shows it, and it's
        published with it.
    """
    legacy = required_permissions(record)
    if not legacy or entry_rule(document) is not None:
        return dict(document)
    rule = {"any": [{"permission": int(i)} for i in legacy]}
    return {
        **document,
        "nodes": [
            {**n, "config": {**(n.get("config") or {}), SETTING: rule}}
            if n.get("kind") == "agent"
            else n
            for n in _nodes(document)
        ],
    }


async def as_strict_as_live(
    store: ChatAgentStore,
    organization_id: str,
    record: Mapping[str, Any],
    document: dict[str, Any],
    version: int | str,
) -> tuple[dict[str, Any], str]:
    """
    An agent's version (or its draft) as it runs: never open to more people
    than its latest published version, nor than its record says (from
    before its nodes had rules). Its nodes' own rules stay as they are; what
    it's held to besides is the document's ``required_rules``
    (:func:`stricter`), which the runtime adds to them. Any it came with are
    the runtime's to say, not the document's: they're replaced.

    :param version: Which it is: a published version's number, or ``"draft"``.
    :return: The document, and what it was made stricter by, for cache
        keys: empty when nothing was.
    :raises PyMongoError: The store isn't answering.
    """
    document = {key: value for key, value in document.items() if key != REQUIRED}
    latest = record.get("published_version")
    legacy = required_permissions(record)
    live = None
    if latest is not None and version != latest:
        found = await store.version(organization_id, str(record["_id"]), int(latest))
        live = found["document"] if found is not None else None
    if live is None and not legacy:
        return document, ""
    required = stricter(document, live, legacy)
    if not required:
        return document, ""
    stamp = hashlib.sha256(json.dumps(required, sort_keys=True).encode()).hexdigest()[
        :16
    ]
    return {
        **document,
        REQUIRED: required,
    }, f"{f'live:{latest}' if live is not None else 'old'}:{stamp}"


def _name(record: Mapping[str, Any]) -> str:
    document = record.get("document") or {}
    return str(document.get("name") or record.get("_id") or "This agent")


def _either(keys: Sequence[str]) -> str:
    return keys[0] if len(keys) == 1 else ", ".join(keys[:-1]) + " or " + keys[-1]


async def check_agent_use(
    session: AsyncSession,
    organization_id: str,
    agents: Sequence[Mapping[str, Any]],
    user: AgentUser,
) -> None:
    """
    Check that someone may use every one of an organization's agents.

    :param session: The admin database.
    :param organization_id: The agents' organization.
    :param agents: Their records (``ChatAgentStore``).
    :param user: Who's using them.
    :raises AgentUseRefused: They may not use one: naming it, and the
        permissions that would let them.
    """
    restricted = [(r, ids) for r in agents if (ids := required_permissions(r))]
    if not restricted or user.manager:
        return
    if user.subject is None:
        raise AgentUseRefused(
            f"{_name(restricted[0][0])} is only for those the organization lets "
            "use it: sign in, or send an API key"
        )
    wanted = {i for _, ids in restricted for i in ids}
    keys = {
        p.id: p.key
        for p in await session.scalars(
            select(AgentPermission).where(
                AgentPermission.organization_id == organization_id,
                AgentPermission.id.in_(wanted),
            )
        )
    }
    enforcer = await team_enforcer(session, organization_id)
    for record, ids in restricted:
        needed = [keys[i] for i in ids if i in keys]
        if holds_any(enforcer, organization_id, user.subject, needed, user.groups):
            continue
        raise AgentUseRefused(
            f"{_name(record)} needs {_either(needed)} in the organization"
            if needed
            else f"{_name(record)} needs an agent permission that was deleted: "
            "ask the organization's admins"
        )
