"""Who may use each agent on a chat agent's canvas: the rule on its node.

An agent's node (the chat agent, a sub-agent, an intake agent, a saved
agent) may say which of the organization's agent permissions someone needs
to use it (``config.access``): a rule, null for anyone who may call the agent.

- ``{"permission": 12}``: they hold that permission (by its ID);
- ``{"all": [rule, …]}``: every one of the rules holds;
- ``{"any": [rule, …]}``: at least one does.

Groups nest (:data:`MAX_DEPTH` deep), so "invoices:approve, or both
finance:read and finance:write" is ``{"any": [{"permission": 1}, {"all":
[{"permission": 2}, {"permission": 3}]}]}``.

The runtime checks the rules as the agents run, for whoever the call is for
(:class:`AccessCheck`, :attr:`RuntimeServices.access
<forge_agent_runtime.services.RuntimeServices.access>`), so one conversation
may use a coordinator that lets someone in and a sub-agent that doesn't:

- an agent whose rule doesn't hold answers in its own place, saying what it
  needs (:func:`agent_guard`, its ``before_agent_callback``);
- an agent handing off to one, or calling it as a tool, is answered that
  instead (:func:`hand_off_guard`, its ``before_tool_callback``), so the
  conversation stays with it;
- an agent giving one a task (``task`` and ``single_turn`` hand-offs, which
  ADK sends on itself, past tool callbacks) says what it needs in place of
  the call (:func:`task_guard`, its ``after_model_callback``), so no task is
  left open waiting on an agent that won't take it.

An agent's rule holds together with the rules of the agents above it, those
that hand off to it or call it: a conversation that was handed to a
sub-agent goes straight to it on later turns, so someone whose access to its
coordinator was taken away mustn't carry on there. And the chat agent's own
rule is checked as each turn starts, before anything runs
(:meth:`AgentExecutor.refusal <forge_agent_runtime.executor.AgentExecutor.refusal>`).

Without a check (``access`` None), nobody holds anything: every agent with a
rule refuses.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from google.genai import types

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_response import LlmResponse
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext

#: A node's rule: ``{"permission": id}``, or ``{"all"|"any": [rule, …]}``.
Rule = Mapping[str, Any]

#: How deep groups may nest: a group of groups of groups.
MAX_DEPTH = 3
#: The most permissions one rule may name.
MAX_PERMISSIONS = 50

#: The setting on an agent's node.
SETTING = "access"
#: ADK's tool handing the conversation to another agent.
TRANSFER = "transfer_to_agent"


class RuleError(ValueError):
    """A rule that isn't one: what's wrong with it."""


class AccessRefused(PermissionError):
    """
    Whoever a call is for may not use the agent it calls: refused before the
    turn runs, what it needs said as :func:`refusal` says it.
    """


def parse_rule(value: Any, *, where: str = "access") -> Rule | None:
    """
    :param value: A node's ``access`` setting.
    :return: Its rule, or None when it has none (anyone may use it).
    :raises RuleError: It isn't a rule.
    """
    if value is None:
        return None
    count = [0]
    _check(value, where, 0, count)
    return value


def _check(value: Any, where: str, depth: int, count: list[int]) -> None:
    if not isinstance(value, Mapping) or len(value) != 1:
        raise RuleError(f'{where} should be {{"permission": <id>}}, {{"all": [...]}} or {{"any": [...]}}')
    ((key, inner),) = value.items()
    if key == "permission":
        if not isinstance(inner, int) or isinstance(inner, bool) or inner < 1:
            raise RuleError(f"{where}.permission should be a permission's ID")
        count[0] += 1
        if count[0] > MAX_PERMISSIONS:
            raise RuleError(f"{where} names more than {MAX_PERMISSIONS} permissions")
        return
    if key not in ("all", "any"):
        raise RuleError(f'{where} should be {{"permission": <id>}}, {{"all": [...]}} or {{"any": [...]}}')
    if depth >= MAX_DEPTH:
        raise RuleError(f"{where} nests groups more than {MAX_DEPTH} deep")
    if not isinstance(inner, list) or not inner:
        raise RuleError(f"{where}.{key} should list at least one rule")
    for index, item in enumerate(inner):
        _check(item, f"{where}.{key}[{index}]", depth + 1, count)


def permissions_of(rule: Rule | None) -> frozenset[int]:
    """:return: The IDs of the permissions a rule names."""
    if rule is None:
        return frozenset()
    if "permission" in rule:
        return frozenset({int(rule["permission"])})
    return frozenset().union(*(permissions_of(item) for item in _items(rule)))


def holds(rule: Rule | None, held: frozenset[int]) -> bool:
    """:return: Whether someone holding these permissions satisfies the rule."""
    if rule is None:
        return True
    if "permission" in rule:
        return int(rule["permission"]) in held
    test = all if "all" in rule else any
    return test(holds(item, held) for item in _items(rule))


def all_of(*rules: Rule | None) -> Rule | None:
    """
    :return: A rule holding when every one of these does; None when none is
        a rule. Groups of ``all`` among them are opened into it, and a rule
        given twice counts once, so combining rules nests no deeper than they do.
    """
    present: list[Rule] = []
    for rule in rules:
        if rule is None:
            continue
        for item in rule.get("all", [rule]):
            if item not in present:
                present.append(item)
    if not present:
        return None
    return present[0] if len(present) == 1 else {"all": present}


def any_of(*rules: Rule | None) -> Rule | None:
    """
    :return: A rule holding when at least one of these does; None (anyone
        may) when one of them is None, or none is given. Groups of ``any``
        among them are opened into it.
    """
    if not rules or any(rule is None for rule in rules):
        return None
    present: list[Rule] = []
    for rule in rules:
        assert rule is not None
        for item in rule.get("any", [rule]):
            if item not in present:
                present.append(item)
    return present[0] if len(present) == 1 else {"any": present}


def node_rule(config: Mapping[str, Any], extra: Rule | None = None, *, where: str = "access") -> Rule | None:
    """
    :param config: An agent's node's settings.
    :param extra: A rule the runtime holds the node to besides its own
        (``ChatAgentDocument.required_rules``): already checked, and not held
        to the format's limits, as it may combine several rules.
    :return: The rule someone needs to use the agent: its own and ``extra``.
    :raises RuleError: Its own isn't a rule.
    """
    return all_of(parse_rule(config.get(SETTING), where=where), extra)


def _items(rule: Rule) -> Sequence[Rule]:
    return rule.get("all") or rule.get("any") or ()


def words(
    rule: Rule, names: Mapping[int, str], *, unnamed: str = "a deleted permission (#{})", nested: bool = False
) -> str:
    """
    :param names: The permissions' names by ID (``invoices:approve``).
    :param unnamed: What a permission without a name is called, its ID filled in.
    :return: The rule as people say it: "invoices:approve, or both finance:read and finance:write".
    """
    if "permission" in rule:
        number = int(rule["permission"])
        return names.get(number) or unnamed.format(number)
    every = "all" in rule
    parts = [words(item, names, unnamed=unnamed, nested=True) for item in _items(rule)]
    if len(parts) == 1:
        return parts[0]
    joined = _joined(parts, "and" if every else "or")
    if not nested:
        return joined
    if every:
        return f"both {joined}" if len(parts) == 2 else f"all of {joined}"
    return f"either {joined}" if len(parts) == 2 else f"one of {joined}"


def _joined(parts: Sequence[str], conjunction: str) -> str:
    if len(parts) == 2:
        return f"{parts[0]} {conjunction} {parts[1]}"
    return ", ".join(parts[:-1]) + f", {conjunction} {parts[-1]}"


@dataclass(frozen=True)
class Caller:
    """
    Whoever a call is for, as far as an agent's rule goes.

    :param held: The permissions they hold, of those asked about.
    :param names: Those permissions' names, by ID, which refusals say them by.
    :param everything: They may use every agent, whatever its rule (they
        manage the organization's agents).
    :param known: Someone the runtime knows (a sign-in, an API key); False
        for an anonymous call.
    :param checked: The runtime can tell what anyone holds; False where it
        has no :class:`AccessCheck`.
    """

    held: frozenset[int] = frozenset()
    names: Mapping[int, str] = field(default_factory=dict)
    everything: bool = False
    known: bool = True
    checked: bool = True

    def may(self, rule: Rule | None) -> bool:
        """:return: Whether they may use an agent with this rule."""
        return self.everything or holds(rule, self.held)


def caller_json(caller: Caller) -> dict[str, Any]:
    """:return: What's known of a caller as JSON: what a workflow's run carries to its worker."""
    return {
        "held": sorted(caller.held),
        "names": {str(key): name for key, name in sorted(caller.names.items())},
        "everything": caller.everything,
        "known": caller.known,
    }


def caller_from_json(value: Mapping[str, Any]) -> Caller:
    """:return: A caller from :func:`caller_json`'s JSON."""
    names = value.get("names") or {}
    return Caller(
        held=frozenset(int(i) for i in value.get("held") or []),
        names={int(key): str(name) for key, name in names.items()},
        everything=bool(value.get("everything")),
        known=bool(value.get("known", True)),
    )


#: Someone a runtime without a check calls for: nobody it can tell holds anything.
UNCHECKED = Caller(known=False, checked=False)


class AccessCheck(Protocol):
    """What whoever the current call is for holds."""

    async def __call__(self, wanted: frozenset[int], *, organization_id: str | None) -> Caller:
        """
        :param wanted: The permissions an agent's rule names, by ID.
        :param organization_id: The agent's organization, whose permissions they are.
        :return: Who's calling: which of them they hold, and the permissions' names.
        """
        ...


@dataclass(frozen=True)
class HeldAccess:
    """A check answering with what's already known of the caller: a run's, taken as it started."""

    caller: Caller

    async def __call__(self, wanted: frozenset[int], *, organization_id: str | None) -> Caller:
        return self.caller


def refusal(label: str, rule: Rule, caller: Caller) -> str:
    """:return: What an agent someone may not use says in its place: what it needs."""
    # A runtime that can't check them doesn't know their names either.
    needs = words(
        rule, caller.names, unnamed="a deleted permission (#{})" if caller.checked else "permission #{}"
    )
    if not caller.checked:
        return f"{label} needs {needs}, which this runtime can't check, so it can't be used here."
    if not caller.known:
        return f"{label} is only for people its organization lets use it: it needs {needs}. Sign in, or send an API key, to use it."
    return f"You can't use {label}: it needs {needs}. Ask your organization's admins for access."


@dataclass(frozen=True)
class Guarded:
    """An agent with a rule: what refusals call it (its node's name), and the rule."""

    label: str
    rule: Rule


async def refused(guarded: Guarded, check: AccessCheck | None, *, organization_id: str | None) -> str | None:
    """:return: Why whoever the call is for may not use the agent; None when they may."""
    if check is None:
        caller = UNCHECKED
    else:
        caller = await check(permissions_of(guarded.rule), organization_id=organization_id)
    return None if caller.may(guarded.rule) else refusal(guarded.label, guarded.rule, caller)


def agent_guard(guarded: Guarded, check: AccessCheck | None, *, organization_id: str | None) -> Any:
    """
    :return: An agent's ``before_agent_callback``: when whoever the call is
        for may not use it, it answers with what it needs and goes no further.
    """

    async def guard(callback_context: CallbackContext) -> types.Content | None:
        why = await refused(guarded, check, organization_id=organization_id)
        if why is None:
            return None
        return types.Content(role="model", parts=[types.Part(text=why)])

    return guard


def hand_off_guard(
    guarded: MutableMapping[str, Guarded], check: AccessCheck | None, *, organization_id: str | None
) -> Any:
    """
    :param guarded: The agents with rules, by their ADK names: the whole
        tree's, as it's built (read as the agent runs).
    :return: An agent's ``before_tool_callback``: a hand-off to an agent the
        caller may not use, or a call of one as a tool, is answered with what
        it needs instead, so the conversation stays with this agent.
    """

    async def guard(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext) -> dict[str, Any] | None:
        target = args.get("agent_name") if tool.name == TRANSFER else tool.name
        found = guarded.get(str(target or ""))
        if found is None:
            return None
        why = await refused(found, check, organization_id=organization_id)
        if why is None:
            return None
        return {"error": f"{why} Tell the person so, and what it needs."}

    return guard


def task_guard(
    guarded: Mapping[str, Guarded],
    tasks: Collection[str],
    check: AccessCheck | None,
    *,
    organization_id: str | None,
) -> Any:
    """
    :param guarded: The agents with rules, by their ADK names (the whole tree's).
    :param tasks: The agents this one gives tasks to, by their ADK names:
        its ``task`` and ``single_turn`` sub-agents and intake agents.
    :return: An agent's ``after_model_callback``: a call giving a task to an
        agent the caller may not use becomes what that agent needs, said in
        the call's place.
    """

    async def guard(callback_context: CallbackContext, llm_response: LlmResponse) -> LlmResponse | None:
        content = llm_response.content
        if content is None or not content.parts:
            return None
        parts: list[types.Part] = []
        changed = False
        for part in content.parts:
            name = part.function_call.name if part.function_call else None
            found = guarded.get(name) if name is not None and name in tasks else None
            if found is not None:
                why = await refused(found, check, organization_id=organization_id)
                if why is not None:
                    parts.append(types.Part(text=why))
                    changed = True
                    continue
            parts.append(part)
        if not changed:
            return None
        return llm_response.model_copy(update={"content": types.Content(role=content.role, parts=parts)})

    return guard
