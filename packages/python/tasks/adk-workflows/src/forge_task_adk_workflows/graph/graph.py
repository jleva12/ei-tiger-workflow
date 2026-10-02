"""
A ``forge.agent/v1`` document built into one ADK ``Workflow``: each node by its
kind's factory (``factories.FACTORIES``), joined by ADK's edges.

- **Start.** ``START`` leads to the start's hidden input node, which keeps the
  run's input (``factories/start.py``); the graph's own ``input_schema`` is the
  start's, as a Pydantic model, so ADK checks a run's message against it.
- **Routes.** Every way out of a branching step (HTTP, Approval, If, Switch,
  Match, Loop) is its output ID, and its node emits the route of the way it
  takes; a Merge "any"'s way out is ``next``. Several ways from one node to
  another are one edge with a list of routes. Other nodes' edges have none:
  ADK takes them whatever comes. ADK's default route isn't used.
- **Loops.** A loop's body (what its Each item way reaches before coming back
  to it) is cut out into a graph of its own, run per item by the loop's node;
  its ways back lead to a hidden node whose output is the item's result, its
  other endings to one that hands on nothing.
- **Endings.** ADK takes one ending node with an output, so every ending of an
  agent's graph leads to a hidden finish: an End's ``{outcome, result}``, or,
  from any other ending, its run succeeded with what it handed on.
- **Merges.** A Merge "all" is a ``JoinNode``, which goes on only once every
  node into it has; ways in from different ways out of one branching step
  would never all come, so they're refused. A Merge "any"'s ways in pass
  through hidden nodes that tag what they hand on with the step they came from.

The checks are the builder's: one start, unique ADK names, sub-agent IDs that
are state keys of their own, settings each kind can run with, edges between
nodes and outputs the agent has, every node reached from the start, loop
bodies entered only by their loop, Merge "all"s that can go on, saved agents
that don't run themselves. A document that doesn't pass is refused with
:class:`AgentBuildError`, naming the node.
"""

import functools
import re
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import replace
from typing import Any

from google.adk import Workflow
from google.adk.models import BaseLlm
from google.adk.workflow import START, BaseNode, Edge
from pydantic import PrivateAttr, ValidationError

from forge_task_adk_workflows.graph.data import INPUT_NODE, Scope, StepInfo
from forge_task_adk_workflows.graph.errors import AgentBuildError, RunFailed
from forge_task_adk_workflows.graph.factories import FACTORIES, Body, BuildContext
from forge_task_adk_workflows.graph.factories.agents import answer_parser
from forge_task_adk_workflows.graph.factories.base import (
    config_of,
    items,
    text,
    where_of,
)
from forge_task_adk_workflows.graph.factories.logic import back, ended, finish, stop, via
from forge_task_adk_workflows.graph.factories.start import input_model
from forge_task_adk_workflows.graph.names import (
    GRAPH_NAME,
    RESERVED_NAMES,
    adk_name,
    body_name,
    via_name,
)
from forge_task_adk_workflows.graph.schemas import is_model, problems
from forge_task_adk_workflows.graph.services import ModelCallbacks, Resolve, RunServices

KINDS = frozenset(FACTORIES)
# The ways out of the steps that branch, when they're fixed.
BRANCHES = {
    "http": ("success", "error"),
    "approval": ("approved", "rejected"),
    "if": ("true", "false"),
    "loop": ("each", "done"),
}
# A sub-agent's ID: an LLM sub-agent's answer is kept in the state under it.
SUB_AGENT_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Steps that can pause a run: a loop whose body has one runs its items one at
# a time.
PAUSING = frozenset({"approval", "human_input", "delay", "saved"})


class AgentGraph(Workflow):
    """ADK's ``Workflow``, refusing input that doesn't fit its start as Forge
    does (:class:`RunFailed`)."""

    _input_misfit: str = PrivateAttr(default="The run's input doesn't fit its start")

    def _validate_input_data(self, data: Any) -> Any:
        try:
            return super()._validate_input_data(data)
        except ValidationError as error:
            raise RunFailed(f"{self._input_misfit}: {problems(error, 'input')}") from None


def build_agent(
    document: dict[str, Any],
    *,
    services: RunServices | None = None,
    model: str | BaseLlm | None = None,
    model_callbacks: ModelCallbacks | None = None,
    resolve: Resolve | None = None,
) -> Workflow:
    """
    Build an agent into ADK's objects, ready to run.

    The model every LLM agent runs on is ``model`` when given (e.g. the shared
    ``forge_common.adk.models.ProviderModels``), else its own setting's model
    name, else ``DEFAULT_MODEL``. Choosing the agent's model and thinking
    level from its settings on each request is ``model_callbacks``' part: it
    makes each LLM agent's ``before_model_callback`` from its settings
    (``ProviderModels.select`` with its ``model`` and ``thinking_level``).

    :param document: A ``forge.agent/v1`` document.
    :param services: What its nodes use (HTTP, the clock, where HTTP may go…);
        the worker's defaults when None.
    :param model: The model every LLM agent runs on, when given.
    :param model_callbacks: Makes each LLM agent's ``before_model_callback``
        from its settings; none when None.
    :param resolve: Finds the organization's other agents, for its saved-agent
        nodes; without it, a document with one isn't built.
    :return: The agent's graph, named after it.
    :raises AgentBuildError: When it can't be built, naming the node: no start
        or more than one, a name that makes no ADK name or another's, a kind of
        node it doesn't know, settings it can't run with, a saved agent that
        isn't there or runs itself, an edge from or to a node or output it
        doesn't have, a node the start doesn't lead to, a Merge "all" that
        would never go on, and anything else ADK refuses.
    """
    services = (services or RunServices()).but(model=model, model_callbacks=model_callbacks, resolve=resolve)
    agent_id = document.get("id")
    name = adk_name(text(document.get("name")))
    agent = _Agent(
        document,
        services,
        within=(agent_id,) if isinstance(agent_id, str) else (),
        top=True,
        input_misfit="The run's input doesn't fit its start",
    )
    return agent.graph(name if name and name not in RESERVED_NAMES else GRAPH_NAME)


def outputs_of(node: dict[str, Any]) -> list[str]:
    """:return: A node's ways out, by ID, in order."""
    kind = node.get("kind")
    config = config_of(node)
    if kind == "end":
        return []
    if kind in BRANCHES:
        return list(BRANCHES[kind])
    if kind == "switch":
        cases = [text(case.get("id")) for case in items(config.get("cases"))]
        return [*(case for case in cases if case), "default"]
    if kind == "match":
        arms = [text(arm.get("id")) for arm in items(config.get("arms"))]
        return [*(arm for arm in arms if arm), "otherwise"]
    return ["next"]


def _routed(node: dict[str, Any]) -> bool:
    # Whether a node's edges carry its ways' output IDs as routes.
    return outputs_of(node) != ["next"] or (node.get("kind") == "merge" and config_of(node).get("mode") == "any")


def _pauses(node: dict[str, Any]) -> bool:
    kind = node.get("kind")
    return kind in PAUSING or (kind == "llm" and config_of(node).get("mode") == "task")


class _Agent:
    """One document being built: its checked graph, and how its graphs (its
    own, its loops' bodies) are made."""

    def __init__(
        self,
        document: dict[str, Any],
        services: RunServices,
        *,
        within: tuple[str, ...],
        top: bool,
        input_misfit: str,
    ) -> None:
        self.document = document
        self.services = services
        # The IDs of the agents being built, outermost first: a saved agent
        # among them would run itself.
        self.within = within
        self.top = top
        self.input_misfit = input_misfit
        self.nodes = [n for n in items(document.get("nodes")) if isinstance(n, dict)]
        ids: set[str] = set()
        for node in self.nodes:
            if node.get("kind") not in KINDS:
                raise AgentBuildError(f"{where_of(node)}: Forge has no kind of node {node.get('kind')!r}.")
            if not isinstance(node.get("id"), str) or node["id"] in ids:
                raise AgentBuildError(f"{where_of(node)}: its ID isn't its own.")
            ids.add(node["id"])
        starts = [node for node in self.nodes if node["kind"] == "start"]
        if len(starts) != 1:
            raise AgentBuildError(
                f"The agent has {len(starts) or 'no'} start{'s' if len(starts) > 1 else ''}; it needs one."
            )
        self.start = starts[0]
        _check_names(self.nodes)
        self.by_id = {node["id"]: node for node in self.nodes}
        self.edges = self._checked_edges(items(document.get("edges")))
        if not any(source == self.start["id"] for source, _, _ in self.edges):
            raise AgentBuildError("The start leads nowhere: connect it to a node.")
        self._check_reached()
        self.bodies = {node["id"]: self._body_of(node["id"]) for node in self.nodes if node["kind"] == "loop"}
        self._check_bodies()
        self._check_merges()
        self.names = {node["id"]: adk_name(text(node.get("name"))) for node in self.nodes if node is not self.start}
        steps = {INPUT_NODE: StepInfo(self.start["id"], "start")}
        for node in self.nodes:
            if node is not self.start:
                parse = answer_parser(config_of(node)) if node["kind"] == "llm" else None
                steps[self.names[node["id"]]] = StepInfo(node["id"], node["kind"], parse)
        self.scope = Scope(steps=steps, start=self.start["id"])
        self.connected = {
            node["id"]: frozenset(output for source, output, _ in self.edges if source == node["id"])
            for node in self.nodes
        }
        self._saved: dict[tuple[str, str], Workflow] = {}

    # ------------------------------------------------------------------ graphs

    def graph(self, name: str) -> Workflow:
        """The agent's own graph."""
        ctx = BuildContext(
            services=self.services,
            scope=self.scope,
            names=self.names,
            connected=self.connected,
            top=self.top,
            input_misfit=self.input_misfit,
            saved=self._saved_agent,
        )
        region = {node["id"] for node in self.nodes if node is not self.start}
        edges = self._assemble(region - _within(self.bodies, region), ctx, loop=None)
        settings: dict[str, Any] = {
            "name": name,
            "description": text(self.document.get("description")),
            "edges": edges,
        }
        model = input_model(self.start)
        if is_model(model):
            settings["input_schema"] = model
        try:
            graph = AgentGraph(**settings)
        except ValueError as error:
            raise AgentBuildError(f"ADK can't build the agent's graph: {error}") from None
        graph._input_misfit = self.input_misfit
        return graph

    def _body(self, loop_id: str, ctx: BuildContext, bindings: Any) -> Workflow:
        # A loop's body graph, for an item.
        body = self.bodies[loop_id]
        inner = replace(ctx, scope=ctx.scope.inside(), bindings=dict(bindings))
        edges: list[Any] = self._assemble(body - _within(self.bodies, body), inner, loop=loop_id)
        try:
            return Workflow(name=body_name(self.names[loop_id]), edges=edges)
        except ValueError as error:
            where = where_of(self.by_id[loop_id])
            raise AgentBuildError(f"{where}: ADK can't build its body's graph: {error}") from None

    def _assemble(self, members: AbstractSet[str], ctx: BuildContext, *, loop: str | None) -> list[Edge]:
        """
        The ADK edges of one graph: the agent's (``loop`` None) or a loop's
        body's, of its ``members`` (its steps but those in its loops' bodies).
        """
        # In the document's order, so the graph is the same each time.
        ordered = [node["id"] for node in self.nodes if node["id"] in members]
        loops = [step for step in ordered if self.by_id[step]["kind"] == "loop"]
        ctx = replace(
            ctx,
            bodies={
                step: Body(
                    build=functools.partial(self._body, step, ctx),
                    pauses=any(_pauses(self.by_id[n]) for n in self.bodies[step]),
                )
                for step in loops
            },
        )
        built: dict[str, BaseNode] = {step: self._node(step, ctx) for step in ordered}
        # What leads into the graph: the start's input node, or the loop's
        # Each item way; what leads back to the loop.
        entries: list[str] = []
        if loop is None:
            built[self.start["id"]] = self._node(self.start["id"], ctx)
            entries = [self.start["id"]]
        else:
            entries = [t for s, o, t in self.edges if s == loop and o == "each"]
            if any(target == loop for target in entries) or any(s in members and t == loop for s, _, t in self.edges):
                built[loop] = back()
        ways: dict[tuple[str, str], list[str]] = {}
        for source, output, target in self.edges:
            if source not in built or target not in built or source == loop:
                continue
            if source in loops and output == "each":
                continue  # the body's, cut out
            ways.setdefault((source, target), []).append(output)
        edges = [Edge(from_node=START, to_node=built[entry]) for entry in dict.fromkeys(entries) if entry in built]
        for (source, target), outputs in ways.items():
            node = self.by_id[source]
            route = self._route(node, outputs) if node is not self.start else None
            target_node = self.by_id.get(target)
            if (
                target_node is not None
                and target_node["kind"] == "merge"
                and config_of(target_node).get("mode") == "any"
            ):
                tag = via(via_name(self.names[target], built[source].name), source)
                edges.append(Edge(from_node=built[source], to_node=tag, route=route))
                edges.append(Edge(from_node=tag, to_node=built[target]))
                continue
            edges.append(Edge(from_node=built[source], to_node=built[target], route=route))
        edges.extend(self._endings(members, built, ways, loop=loop))
        return edges

    def _endings(
        self,
        members: AbstractSet[str],
        built: dict[str, BaseNode],
        ways: dict[tuple[str, str], list[str]],
        *,
        loop: str | None,
    ) -> list[Edge]:
        # The graph's endings, led to its hidden ending nodes: ADK takes one
        # ending with an output.
        leading = {source for source, _ in ways}
        ends = [step for step in built if step in members and step not in leading]
        if not ends:
            return []
        if loop is not None:
            sink = stop()
            return [Edge(from_node=built[step], to_node=sink) for step in ends]
        collector = finish(top=self.top)
        edges = [Edge(from_node=built[step], to_node=collector) for step in ends if self.by_id[step]["kind"] == "end"]
        others = [step for step in ends if self.by_id[step]["kind"] != "end"]
        if others:
            wrapper = ended()
            edges.extend(Edge(from_node=built[step], to_node=wrapper) for step in others)
            edges.append(Edge(from_node=wrapper, to_node=collector))
        return edges

    @staticmethod
    def _route(node: dict[str, Any], outputs: list[str]) -> Any:
        # The route of ADK's one edge from a node to another, for every way the
        # document leads there.
        if not _routed(node):
            return None
        routes = list(dict.fromkeys(outputs))
        return routes[0] if len(routes) == 1 else routes

    def _node(self, step: str, ctx: BuildContext) -> BaseNode:
        node = self.by_id[step]
        try:
            return FACTORIES[node["kind"]](node, ctx)
        except AgentBuildError:
            raise
        except ValueError as error:
            raise AgentBuildError(f"{where_of(node)}: ADK refuses it: {error}") from None

    def _saved_agent(self, agent_id: str, name: str, where: str) -> Workflow:
        if agent_id in self.within:
            cycle = " → ".join([*self.within[self.within.index(agent_id) :], agent_id])
            raise AgentBuildError(f"{where}: agent {agent_id} would run itself: {cycle}.")
        found = self._saved.get((agent_id, name))
        if found is not None:
            return found
        if self.services.resolve is None:
            raise AgentBuildError(f"{where}: it runs agent {agent_id}, and there's no way to find it here.")
        document = self.services.resolve(agent_id)
        if not isinstance(document, dict):
            raise AgentBuildError(f"{where}: the organization has no agent {agent_id}.")
        try:
            inner = _Agent(
                document,
                self.services,
                within=(*self.within, agent_id),
                top=False,
                input_misfit=f"{where}: its input doesn't fit agent {agent_id}'s start",
            )
            graph = inner.graph(name)
        except AgentBuildError as error:
            raise AgentBuildError(f"{where}: agent {agent_id} can't be built. {error}") from None
        self._saved[(agent_id, name)] = graph
        return graph

    # ------------------------------------------------------------------ checks

    def _checked_edges(self, edges: list[Any]) -> list[tuple[str, str, str]]:
        checked: dict[tuple[str, str, str], None] = {}
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source = self.by_id.get(edge.get("source"))
            if source is None:
                raise AgentBuildError(f"An edge leaves node {edge.get('source')!r}, which the agent doesn't have.")
            where = where_of(source)
            target = self.by_id.get(edge.get("target"))
            if target is None:
                raise AgentBuildError(
                    f"{where}: an edge from it leads to node {edge.get('target')!r}, which the agent doesn't have."
                )
            if target is self.start:
                raise AgentBuildError(f"{where}: an edge from it leads into the start.")
            output = edge.get("source_output")
            if output not in outputs_of(source):
                raise AgentBuildError(f"{where}: it has no output {output!r}.")
            checked[(source["id"], output, target["id"])] = None
        return list(checked)

    def _check_reached(self) -> None:
        # ADK runs only what the start leads to, and refuses a node it doesn't.
        reached = _reach(self.edges, [self.start["id"]])
        for node in self.nodes:
            if node is not self.start and node["id"] not in reached:
                raise AgentBuildError(f"{where_of(node)}: nothing leads to it from the start.")

    def _body_of(self, loop: str) -> frozenset[str]:
        # A loop's body: what its Each item way reaches before coming back to it.
        starts = [t for s, o, t in self.edges if s == loop and o == "each"]
        return frozenset(_reach(self.edges, starts, stop={loop}) - {loop})

    def _check_bodies(self) -> None:
        # A loop's body runs per item, inside the loop: only its Each item way
        # leads into it.
        for loop, body in self.bodies.items():
            for source, output, target in self.edges:
                if (
                    target in body
                    and source not in body
                    and (source, output)
                    != (
                        loop,
                        "each",
                    )
                ):
                    raise AgentBuildError(
                        f"{where_of(self.by_id[target])}: it's in the body of loop "
                        f"'{text(self.by_id[loop].get('name')) or loop}', and "
                        f"{where_of(self.by_id[source]).lower()}, outside it, leads "
                        "into it; only the loop's Each item way may."
                    )

    def _check_merges(self) -> None:
        # ADK's JoinNode goes on once every node into it has: ways in from
        # different ways out of one branching step never all come.
        for merge in self.nodes:
            if merge["kind"] != "merge" or config_of(merge).get("mode") == "any":
                continue
            ways_in = [(s, o) for s, o, t in self.edges if t == merge["id"]]
            for step in self.nodes:
                outputs = outputs_of(step)
                if len(outputs) < 2:
                    continue
                came: dict[tuple[str, str], set[str]] = {}
                for output in outputs:
                    targets = [t for s, o, t in self.edges if s == step["id"] and o == output]
                    reach = _reach(self.edges, targets, stop={step["id"], merge["id"]})
                    for way in ways_in:
                        if way == (step["id"], output) or (way[0] != step["id"] and way[0] in reach):
                            came.setdefault(way, set()).add(output)
                ways = list(came.items())
                for index, (first, outs) in enumerate(ways):
                    for second, others in ways[index + 1 :]:
                        if outs.isdisjoint(others):
                            raise AgentBuildError(
                                f"{where_of(merge)}: it waits for all its ways in, but "
                                f"{_way(self.by_id, first)} and {_way(self.by_id, second)} "
                                f"come from different ways out of "
                                f"'{text(step.get('name')) or step['id']}' "
                                f"({', '.join(sorted(outs))} and "
                                f"{', '.join(sorted(others))}), so it would never go "
                                'on; make it go on at the first ("any").'
                            )


def _way(by_id: dict[str, dict[str, Any]], way: tuple[str, str]) -> str:
    source = by_id[way[0]]
    return f"'{text(source.get('name')) or source['id']}'"


def _within(bodies: dict[str, frozenset[str]], region: Iterable[str]) -> set[str]:
    # The steps in the bodies of the loops in a region.
    inside: set[str] = set()
    for step in region:
        inside |= bodies.get(step, frozenset())
    return inside


def _reach(
    edges: list[tuple[str, str, str]],
    starts: Iterable[str],
    *,
    stop: set[str] | frozenset[str] = frozenset(),
) -> set[str]:
    # What the starts lead to (themselves too), not going on past ``stop``.
    reached: set[str] = set()
    waiting = list(starts)
    while waiting:
        at = waiting.pop()
        if at in reached:
            continue
        reached.add(at)
        if at in stop:
            continue
        waiting.extend(t for s, _, t in edges if s == at and t not in reached)
    return reached


def _check_names(nodes: list[dict[str, Any]]) -> None:
    # Every node's and sub-agent's ADK name is one, and only theirs; every
    # sub-agent's ID is one its answer can be kept under, and only its.
    named: dict[str, str] = {}
    keys: dict[str, str] = {}

    def check(item: dict[str, Any], where: str) -> None:
        name = adk_name(text(item.get("name")))
        if not name:
            raise AgentBuildError(f"{where}: its name makes no ADK name; give it one that starts with a letter.")
        if name in RESERVED_NAMES:
            raise AgentBuildError(f"{where}: its ADK name, {name}, is ADK's for the person; rename it.")
        if name in named:
            raise AgentBuildError(f"{where}: its ADK name, {name}, is {named[name]}'s too; rename one of them.")
        named[name] = where

    def check_key(item: dict[str, Any], where: str) -> None:
        key = item.get("id")
        if not isinstance(key, str) or not SUB_AGENT_ID.match(key):
            raise AgentBuildError(f"{where}: its ID, {key!r}, can't be a state key its answer is kept under.")
        if key == "input":
            raise AgentBuildError(f"{where}: its ID can't be input: the run's input is kept there.")
        if key in keys:
            raise AgentBuildError(f"{where}: its ID, {key}, is {keys[key]}'s too.")
        keys[key] = where

    def check_sub_agents(config: dict[str, Any], where: str) -> None:
        for agent in items(config.get("sub_agents")):
            if isinstance(agent, dict):
                label = text(agent.get("name")) or text(agent.get("id"))
                at = f"{where}, sub-agent '{label}'"
                check(agent, at)
                check_key(agent, at)
                check_sub_agents(config_of(agent), at)

    for node in nodes:
        where = where_of(node)
        if node["kind"] != "start":
            check(node, where)
        check_sub_agents(config_of(node), where)
