"""
A ``forge.agent/v1`` document built into one ADK ``Workflow``: each node by its
kind's factory (``factories.FACTORIES``), joined by ADK's edges.

- **Start.** ``START`` leads to the start's hidden input node, which keeps the
  run's input (``factories/start.py``); the graph's own ``input_schema`` is the
  start's, as a Pydantic model, so ADK checks a run's message against it.
- **Routes.** Every way out of a branching step (HTTP, Bash, Approval, If, Switch,
  Match, Loop) is its output ID, and its node emits the route of the way it
  takes (a Match taking every rule that holds, a list of them: ADK takes each
  edge whose route is in it); a Merge "any"'s way out is ``next``. Several ways from one node to
  another are one edge with a list of routes. Other nodes' edges have none:
  ADK takes them whatever comes. ADK's default route isn't used.
- **Loops.** A loop's body (what its Each item way reaches before coming back
  to it) is cut out into a graph of its own, run per item by the loop's node;
  its ways back lead to a hidden node whose output is the item's result, its
  other endings to one that hands on nothing. A for-each's body is cut out
  the same, but nothing leads back to it: each item's way ends where it ends.
- **Endings.** ADK takes one ending node with an output, so every ending of an
  agent's graph leads, through a hidden node tagging it with its step, to a
  hidden finish that collects them all: an End's ``{outcome, result}``, or,
  from any other ending, a success with what it handed on. When ways run at
  once (a Match taking every rule that holds, a step leading to several),
  each that ends is one of the run's endings. An HTTP step's status way that
  leads nowhere is an ending too: a status it skips ends its way there,
  handing on the response, while its other ways lead on.
- **Decision boards.** A board is its own node (what leads into it leads
  there), which leads to a hidden node per choice by the choice's ID; each
  choice's node leads on by its Accepted and Rejected ways, and every one of
  them (and the board's, when it lays nothing out) by Done
  (``factories/people.py``). Its Done way leading nowhere, the board ends its
  way there, once every choice is decided.
- **Merges.** A Merge "all" is a ``JoinNode``, which goes on only once every
  node into it has; ways in from different ways out of one branching step
  would never all come (or, from a Match taking every rule that holds, not
  always), so they're refused. A Merge "any"'s ways in pass
  through hidden nodes that tag what they hand on with the step they came from.

The checks are the builder's: one start, unique ADK names, sub-agent IDs that
are state keys of their own, settings each kind can run with, edges between
nodes and outputs the agent has, every node reached from the start, loop
bodies entered only by their loop, Merge "all"s that can go on, saved agents
that don't run themselves, Pi agents and Bash steps only where the start allows a file system. A document that doesn't pass is refused with
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

from forge_task_adk_workflows.graph.catalog import NODE_CATALOG
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
from forge_task_adk_workflows.graph.factories.logic import back, ending, finish, stop, via
from forge_task_adk_workflows.graph.factories.people import DONE, BoardParts, board_parts
from forge_task_adk_workflows.graph.factories.start import input_model
from forge_task_adk_workflows.graph.names import (
    BODY_KINDS,
    GRAPH_NAME,
    READ_NAMES,
    RESERVED_NAMES,
    adk_name,
    body_name,
    choice_name,
    ending_name,
    via_name,
)
from forge_task_adk_workflows.graph.schemas import is_model, problems
from forge_task_adk_workflows.graph.services import ModelCallbacks, Resolve, RunServices
from forge_task_adk_workflows.graph.uses import bare_id

KINDS = frozenset(FACTORIES)
# A sub-agent's ID: an LLM sub-agent's answer is kept in the state under it.
SUB_AGENT_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Steps that can pause a run: a loop whose body has one runs its items one at
# a time.
PAUSING = frozenset({"approval", "human_input", "decision_board", "delay", "saved"})
#: A Decision board: built as hidden nodes, a choice's ways leaving its own.
BOARD = "decision_board"


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
    """:return: A node's ways out, by ID, in order: its kind's (``NODE_CATALOG``),
    after one per row of its settings for a kind whose rows are ways out (or
    each of the row's ways, ``<row>:<way>``)."""
    kind = NODE_CATALOG.find(node.get("kind"))
    if kind is None:
        return ["next"]
    rows = []
    if kind.rows is not None:
        found = items(config_of(node).get(kind.rows.setting))
        rows = [text(row.get("id")) for row in found if isinstance(row, dict)]
        rows = [row for row in rows if row]
        if kind.rows.ways:
            rows = [f"{row}:{way.id}" for row in rows for way in kind.rows.ways]
    return [*rows, *(output.id for output in kind.outputs)]


def _routed(node: dict[str, Any]) -> bool:
    # Whether a node's edges carry its ways' output IDs as routes.
    return outputs_of(node) != ["next"] or (node.get("kind") == "merge" and config_of(node).get("mode") == "any")


def _pauses(node: dict[str, Any]) -> bool:
    kind = node.get("kind")
    if kind in PAUSING:
        return True
    if kind != "llm":
        return False
    config = config_of(node)
    # An agent asking back, an agent from the Agents page (its tools may ask
    # a person), or a tool a person confirms.
    return (
        config.get("mode") == "task"
        or config.get("source") == "agent"
        or any(_confirms(tool) for tool in _tools_within(config))
    )


def _tools_within(config: dict[str, Any]) -> list[Any]:
    """:return: An LLM agent's tools, and its sub-agents'."""
    found = list(items(config.get("tools")))
    for sub in items(config.get("sub_agents")):
        if isinstance(sub, dict):
            found.extend(_tools_within(config_of(sub)))
    return found


def _confirms(tool: Any) -> bool:
    return isinstance(tool, dict) and bool(config_of(tool).get("confirm"))


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
        _check_file_system(self.nodes, self.start)
        _check_names(self.nodes)
        _check_items(self.nodes)
        self.by_id = {node["id"]: node for node in self.nodes}
        self.edges = self._checked_edges(items(document.get("edges")))
        if not any(source == self.start["id"] for source, _, _ in self.edges):
            raise AgentBuildError("The start leads nowhere: connect it to a node.")
        self._check_reached()
        self.bodies = {node["id"]: self._body_of(node["id"]) for node in self.nodes if node["kind"] in BODY_KINDS}
        self._check_bodies()
        self._check_merges()
        self.names = {node["id"]: adk_name(text(node.get("name"))) for node in self.nodes if node is not self.start}
        steps = {INPUT_NODE: StepInfo(self.start["id"], "start")}
        for node in self.nodes:
            if node is not self.start:
                parse = answer_parser(config_of(node)) if node["kind"] == "llm" else None
                steps[self.names[node["id"]]] = StepInfo(node["id"], node["kind"], parse)
                # A board's choices hand on the board as it stands: they're its.
                for choice in _choice_ids(node):
                    steps[choice_name(self.names[node["id"]], choice)] = StepInfo(node["id"], node["kind"])
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
        loops = [step for step in ordered if self.by_id[step]["kind"] in BODY_KINDS]
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
        boards = {step: self._board(step, ctx) for step in ordered if self.by_id[step]["kind"] == BOARD}
        built: dict[str, BaseNode] = {
            step: boards[step].opener if step in boards else self._node(step, ctx) for step in ordered
        }
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
        for parts in boards.values():
            edges.extend(
                Edge(from_node=parts.opener, to_node=asks, route=choice) for choice, asks in parts.choices.items()
            )
        for (source, target), outputs in ways.items():
            node = self.by_id[source]
            if source in boards:
                for leaving, route in _board_routes(boards[source], outputs):
                    edges.extend(self._lead(leaving, source, target, route, built))
                continue
            route = self._route(node, outputs) if node is not self.start else None
            edges.extend(self._lead(built[source], source, target, route, built))
        edges.extend(self._endings(members, built, ways, boards, loop=loop))
        return edges

    def _lead(self, leaving: BaseNode, source: str, target: str, route: Any, built: dict[str, BaseNode]) -> list[Edge]:
        # The edges of a way from a step's node to another step: through a
        # hidden node tagging it with its step, into a Merge "any".
        target_node = self.by_id.get(target)
        if target_node is not None and target_node["kind"] == "merge" and config_of(target_node).get("mode") == "any":
            tag = via(via_name(self.names[target], leaving.name), source)
            return [Edge(from_node=leaving, to_node=tag, route=route), Edge(from_node=tag, to_node=built[target])]
        return [Edge(from_node=leaving, to_node=built[target], route=route)]

    def _board(self, step: str, ctx: BuildContext) -> BoardParts:
        node = self.by_id[step]
        try:
            return board_parts(node, ctx)
        except AgentBuildError:
            raise
        except ValueError as error:
            raise AgentBuildError(f"{where_of(node)}: ADK refuses it: {error}") from None

    def _endings(
        self,
        members: AbstractSet[str],
        built: dict[str, BaseNode],
        ways: dict[tuple[str, str], list[str]],
        boards: dict[str, BoardParts],
        *,
        loop: str | None,
    ) -> list[Edge]:
        # The graph's endings, led to its hidden ending nodes: ADK takes one
        # ending with an output. A step nothing leads on from ends its way
        # whichever way it takes; an HTTP step's status way nothing leads on
        # from ends its way too (it's skipped), the others leading on; a
        # board whose Done way leads nowhere ends its way at Done.
        leading = {source for source, _ in ways}
        ends: dict[str, Any] = {
            step: None for step in built if step in members and step not in leading and step not in boards
        }
        for step in built:
            if step in members and step in leading and (left := self._open_status_ways(step, ways)):
                ends[step] = left[0] if len(left) == 1 else left
        done = {
            step: parts
            for step, parts in boards.items()
            if step in members and not any(source == step and DONE in outputs for (source, _), outputs in ways.items())
        }
        if not ends and not done:
            return []
        # Each way to an ending: from the step's node, or from each of a board's.
        leaving = [(step, built[step], route) for step, route in ends.items()]
        for step, parts in done.items():
            leaving.extend((step, part, DONE) for part in (parts.opener, *parts.choices.values()))
        if loop is not None:
            sink = stop()
            return [Edge(from_node=node, to_node=sink, route=route) for _, node, route in leaving]
        # Each ending tagged with its step on the way, so the finish can
        # collect every way that ended.
        collector = finish(top=self.top)
        edges = []
        tags: dict[str, BaseNode] = {}
        for step, node, route in leaving:
            if step not in tags:
                tags[step] = ending(ending_name(built[step].name), self.by_id[step])
                edges.append(Edge(from_node=tags[step], to_node=collector))
            edges.append(Edge(from_node=node, to_node=tags[step], route=route))
        return edges

    def _open_status_ways(self, step: str, ways: dict[tuple[str, str], list[str]]) -> list[str]:
        # An HTTP step's status ways nothing leads on from, by ID.
        node = self.by_id[step]
        if node.get("kind") != "http":
            return []
        taken = {output for (source, _), outputs in ways.items() if source == step for output in outputs}
        found = items(config_of(node).get("statuses"))
        return [way for row in found if isinstance(row, dict) and (way := text(row.get("id"))) and way not in taken]

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

    def _saved_agent(self, ref: str, name: str, where: str) -> Workflow:
        # A version of it running another of itself is still itself running itself.
        agent_id = bare_id(ref)
        if agent_id in self.within:
            cycle = " → ".join([*self.within[self.within.index(agent_id) :], agent_id])
            raise AgentBuildError(f"{where}: agent {agent_id} would run itself: {cycle}.")
        found = self._saved.get((ref, name))
        if found is not None:
            return found
        if self.services.resolve is None:
            raise AgentBuildError(f"{where}: it runs agent {ref}, and there's no way to find it here.")
        document = self.services.resolve(ref)
        if not isinstance(document, dict):
            raise AgentBuildError(f"{where}: the organization has no agent {ref}.")
        try:
            inner = _Agent(
                document,
                self.services,
                within=(*self.within, agent_id),
                top=False,
                input_misfit=f"{where}: its input doesn't fit agent {ref}'s start",
            )
            graph = inner.graph(name)
        except AgentBuildError as error:
            raise AgentBuildError(f"{where}: agent {ref} can't be built. {error}") from None
        self._saved[(ref, name)] = graph
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
        # leads into it. A for-each's never comes back to it.
        for loop, body in self.bodies.items():
            node = self.by_id[loop]
            if node["kind"] == "for_each":
                back = next(
                    (s for s, o, t in self.edges if t == loop and (s in body or (s, o) == (loop, "each"))), None
                )
                if back is not None:
                    raise AgentBuildError(
                        f"{where_of(self.by_id[back])}: it leads back to for-each "
                        f"'{text(node.get('name')) or loop}', which takes nothing back; "
                        "use a Loop to collect each item's result."
                    )
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
                # A match taking every rule that holds may take both, but
                # not always: it would wait for good when one doesn't hold.
                # A board's choices too, as people decide them.
                every = (step["kind"] == "match" and config_of(step).get("mode") == "all") or step["kind"] == BOARD
                for index, (first, outs) in enumerate(ways):
                    for second, others in ways[index + 1 :]:
                        if outs.isdisjoint(others):
                            raise AgentBuildError(
                                f"{where_of(merge)}: it waits for all its ways in, but "
                                f"{_way(self.by_id, first)} and {_way(self.by_id, second)} "
                                f"come from different ways out of "
                                f"'{text(step.get('name')) or step['id']}' "
                                f"({', '.join(sorted(outs))} and "
                                f"{', '.join(sorted(others))}), so it "
                                f"{'may' if every else 'would'} never go "
                                'on; make it go on at the first ("any").'
                            )


def _choice_ids(node: dict[str, Any]) -> list[str]:
    """:return: A Decision board's choices' IDs; none for any other step."""
    if node.get("kind") != BOARD:
        return []
    return [
        choice
        for row in items(config_of(node).get("choices"))
        if isinstance(row, dict) and (choice := text(row.get("id")))
    ]


def _board_routes(parts: BoardParts, outputs: list[str]) -> list[tuple[BaseNode, Any]]:
    """
    :param parts: A Decision board's nodes.
    :param outputs: Its ways out that lead to one step.
    :return: The nodes those ways leave from, each with its route there: a
        choice's ways from its node, Done from every one of them.
    """
    found: dict[str, tuple[BaseNode, list[str]]] = {}
    for output in dict.fromkeys(outputs):
        if output == DONE:
            leaving = [parts.opener, *parts.choices.values()]
        else:
            asks = parts.choices.get(output.partition(":")[0])
            leaving = [asks] if asks is not None else []
        for node in leaving:
            found.setdefault(node.name, (node, []))[1].append(output)
    return [(node, routes[0] if len(routes) == 1 else routes) for node, routes in found.values()]


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


def _check_items(nodes: list[dict[str, Any]]) -> None:
    # A loop's, a for-each's or a filter's item is read by its name beside the
    # run's data (graph.data): one called as that data is would hide it.
    for node in nodes:
        if node["kind"] not in BODY_KINDS and node["kind"] != "filter":
            continue
        name = text(config_of(node).get("item_name")) or "item"
        if name in READ_NAMES:
            raise AgentBuildError(
                f"{where_of(node)}: its item can't be called {name}: that's {READ_NAMES[name]}, "
                "which the steps reading the item read too; call it something else."
            )


#: The steps that work in the run's folder, and what they do there.
FOLDER_KINDS = {
    "pi_agent": "a Pi agent works",
    "bash": "a Bash step runs its command",
    "git_clone": "a Clone repository step clones",
    "git_branch": "a Create branch step works",
    "git_push": "a Commit and push step works",
}


def _check_file_system(nodes: list[dict[str, Any]], start: dict[str, Any]) -> None:
    """A step that works in the run's folder (a Pi agent, a Bash step, a git step): its agent's start must allow a file system."""
    if config_of(start).get("allow_file_system") is True:
        return
    for node in nodes:
        doing = FOLDER_KINDS.get(text(node.get("kind")))
        if doing:
            raise AgentBuildError(
                f"{where_of(node)}: {doing} in the run's folder: turn on Allow file system on the start."
            )


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
