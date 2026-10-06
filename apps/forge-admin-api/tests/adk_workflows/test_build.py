"""Agents built into Google ADK's objects through ``adk_workflows.build`` (the names it
kept from phase 1): the graph each document is, the checks, and how it runs,
without a model. ``test_agent_graph.py`` tests each kind and runs them."""

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from google.adk import Event, Workflow
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, BaseNode
from google.genai import types

from forge_admin.adk_workflows.build import (
    DEFAULT_MODEL,
    AgentBuildError,
    adk_name,
    build_agent,
)
from forge_admin.adk_workflows.documents import schema_problems

# The web console's example agent, as its builder exports it.
# The example ADK workflow, kept with the package that runs it.
EXAMPLE = (
    Path(__file__).parents[4]
    / "packages/python/adk-workflows/tests/fixtures/example.agent.json"
)


def example() -> dict[str, Any]:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def by_name(graph: Workflow) -> dict[str, BaseNode]:
    assert graph.graph is not None
    return {node.name: node for node in graph.graph.nodes}


def routes(graph: Workflow) -> dict[tuple[str, str], Any]:
    assert graph.graph is not None
    return {
        (edge.from_node.name, edge.to_node.name): edge.route
        for edge in graph.graph.edges
    }


def node(node_id: str, kind: str, name: str, config: dict[str, Any]) -> dict[str, Any]:
    cases = [case["id"] for case in config.get("cases", [])]
    return {
        "id": node_id,
        "kind": kind,
        "name": name,
        "config": config,
        "outputs": [*cases, "default"] if kind == "switch" else ["next"],
    }


def edge(source: str, target: str, output: str = "next") -> dict[str, Any]:
    return {
        "id": f"{source}:{output}->{target}",
        "source": source,
        "source_output": output,
        "target": target,
    }


def agent(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    agent_id: str = "ag_numbers01",
    name: str = "Numbers",
) -> dict[str, Any]:
    return {
        "format": "forge.agent/v1",
        "id": agent_id,
        "name": name,
        "description": "",
        "organization_id": "3f6c0000-0000-4000-8000-000000000001",
        "nodes": nodes,
        "edges": edges,
        "layout": {},
    }


START_NODE = node("start", "start", "Start", {"input_schema": {}})


def function(node_id: str, name: str, expression: str) -> dict[str, Any]:
    return node(
        node_id, "transform", name, {"expression": expression, "output_schema": {}}
    )


# Multiplies a number by the session's factor, then switches on its size: big
# (over 5) or small.
NUMBERS = agent(
    [
        START_NODE,
        function("double", "Double", '{"n": previous.n * state.factor}'),
        node(
            "size",
            "switch",
            "Size",
            {
                "value": 'previous.n > 5 ? "big" : "small"',
                "cases": [
                    {"id": "big", "value": "big"},
                    {"id": "small", "value": "small"},
                ],
            },
        ),
        function("big", "Big", '{"size": "big", "n": previous.n}'),
        function("small", "Small", '{"size": "small", "n": previous.n}'),
    ],
    [
        edge("start", "double"),
        edge("double", "size"),
        edge("size", "big", "big"),
        edge("size", "small", "small"),
    ],
)


def test_these_tests_agents_are_agents() -> None:
    for document in (NUMBERS, INNER, outer()):
        assert schema_problems(document) == []


def test_a_switchs_edges_take_its_cases_ids() -> None:
    assert routes(build_agent(NUMBERS)) == {
        (START.name, "__input__"): None,
        ("__input__", "double"): None,
        ("double", "size"): None,
        ("size", "big"): "big",
        ("size", "small"): "small",
        # The endings that aren't End steps lead to the one hidden finish.
        ("big", "__ended__"): None,
        ("small", "__ended__"): None,
        ("__ended__", "__finish__"): None,
    }


def test_a_node_in_several_edges_is_one_adk_node() -> None:
    graph = build_agent(example())
    assert graph.graph is not None
    merges = {
        id(edge.to_node)
        for edge in graph.graph.edges
        if edge.to_node.name == "read_the_message"
    } | {
        id(edge.from_node)
        for edge in graph.graph.edges
        if edge.from_node.name == "read_the_message"
    }
    assert len(merges) == 1


def test_routes_to_the_same_node_are_one_edge() -> None:
    document = copy.deepcopy(NUMBERS)
    document["nodes"][2]["config"]["cases"].append({"id": "huge", "value": "huge"})
    document["edges"].append(edge("size", "big", "huge"))
    assert routes(build_agent(document))[("size", "big")] == ["big", "huge"]
    # The default is a route like any other: its output ID.
    document["edges"].append(edge("size", "big", "default"))
    assert routes(build_agent(document))[("size", "big")] == ["big", "huge", "default"]


def test_the_model_given_runs_every_llm_agent_with_its_callback() -> None:
    settings: list[dict[str, Any]] = []

    def callbacks(config: dict[str, Any]) -> Any:
        settings.append(config)

        def choose(context: Any, request: Any) -> None:
            return None

        return choose

    graph = build_agent(example(), model="forge/model", model_callbacks=callbacks)
    agents = [
        agent
        for node in by_name(graph).values()
        for agent in _agents(node)
        if isinstance(agent, LlmAgent)
    ]
    assert len(agents) == len(settings) == 9
    assert {agent.model for agent in agents} == {"forge/model"}
    assert all(agent.before_model_callback is not None for agent in agents)
    assert all("thinking_level" in config for config in settings)


def test_an_llm_agent_without_a_model_runs_on_the_default() -> None:
    built = by_name(build_agent(example()))["triage"]
    assert isinstance(built, LlmAgent)
    assert built.model == DEFAULT_MODEL
    assert built.generate_content_config is None


def test_an_agents_own_model_and_generation_settings() -> None:
    document = example()
    triage = next(n for n in document["nodes"] if n["id"] == "triage")["config"]
    triage["model"] = {"provider": "google", "name": "gemini-2.5-pro"}
    triage["max_output_tokens"] = 256
    built = by_name(build_agent(document))["triage"]
    assert isinstance(built, LlmAgent)
    assert built.model == "gemini-2.5-pro"
    assert built.generate_content_config == types.GenerateContentConfig(
        max_output_tokens=256
    )


def _agents(node: BaseNode) -> list[Any]:
    found: list[Any] = [node]
    for agent in getattr(node, "sub_agents", []):
        found.extend(_agents(agent))
    return found


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Billing specialist", "billing_specialist"),
        ("  Read issue! ", "read_issue"),
        ("2nd pass", "nd_pass"),
        ("Résumé", "re_sume"),
        ("123", ""),
        ("x" * 60, "x" * 48),
    ],
)
def test_adk_names_are_the_builders(name: str, expected: str) -> None:
    assert adk_name(name) == expected


def refused(document: dict[str, Any], **options: Any) -> str:
    with pytest.raises(AgentBuildError) as raised:
        build_agent(document, **options)
    return str(raised.value)


def test_two_nodes_with_one_adk_name_are_refused() -> None:
    document = example()
    next(n for n in document["nodes"] if n["id"] == "sentiment")["name"] = "Triage!"
    assert refused(document) == (
        "Node 'Triage!': its ADK name, triage, is Node 'Triage''s too; rename one "
        "of them."
    )


def test_a_sub_agent_with_a_nodes_adk_name_is_refused() -> None:
    document = example()
    polish = next(n for n in document["nodes"] if n["id"] == "polish")
    polish["config"]["sub_agents"][0]["name"] = "Billing"
    assert refused(document).startswith(
        "Node 'Polish the reply', sub-agent 'Billing': its ADK name, billing, is "
        "Node 'Billing''s too"
    )


def test_adks_name_for_the_person_is_refused() -> None:
    document = example()
    document["nodes"][2]["name"] = "User"
    assert "Node 'User': its ADK name, user, is ADK's for the person" in refused(
        document
    )


def test_a_name_that_makes_no_adk_name_is_refused() -> None:
    document = example()
    document["nodes"][2]["name"] = "42"
    assert "Node '42': its name makes no ADK name" in refused(document)


@pytest.mark.parametrize("starts", [0, 2])
def test_an_agent_needs_one_start(starts: int) -> None:
    document = copy.deepcopy(NUMBERS)
    document["nodes"] = [
        *[{**START_NODE, "id": f"start{i}"} for i in range(starts)],
        *document["nodes"][1:],
    ]
    assert refused(document).startswith(f"The agent has {starts or 'no'} start")


def test_a_kind_of_node_forge_doesnt_have_is_refused() -> None:
    document = copy.deepcopy(NUMBERS)
    document["nodes"][1]["kind"] = "teleport"
    assert refused(document) == "Node 'Double': Forge has no kind of node 'teleport'."


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        (
            edge("ghost", "double"),
            "An edge leaves node 'ghost', which the agent doesn't have.",
        ),
        (
            edge("double", "ghost"),
            "Node 'Double': an edge from it leads to node 'ghost', which the agent "
            "doesn't have.",
        ),
        (edge("double", "size", "medium"), "Node 'Double': it has no output 'medium'."),
        (edge("size", "big", "medium"), "Node 'Size': it has no output 'medium'."),
        (
            edge("double", "start"),
            "Node 'Double': an edge from it leads into the start.",
        ),
    ],
)
def test_an_edge_the_nodes_dont_have_is_refused(
    changed: dict[str, Any], message: str
) -> None:
    document = copy.deepcopy(NUMBERS)
    document["edges"].append(changed)
    assert refused(document) == message


def test_a_node_nothing_leads_to_is_refused() -> None:
    document = copy.deepcopy(NUMBERS)
    document["edges"] = document["edges"][:3]
    assert refused(document) == "Node 'Small': nothing leads to it from the start."
    document["edges"] = []
    assert refused(document) == "The start leads nowhere: connect it to a node."


def test_a_cycle_adk_refuses_is_refused_as_the_agents() -> None:
    document = copy.deepcopy(NUMBERS)
    document["edges"].append(edge("big", "double"))
    document["edges"].append(edge("double", "big"))
    assert refused(document).startswith("ADK can't build the agent's graph:")


def saved_node(agent_id: str, name: str = "Check the number") -> dict[str, Any]:
    return node("check", "saved", name, {"agent": agent_id, "version": None})


# Adds one: run inside another agent.
INNER = agent(
    [START_NODE, function("add", "Add one", '{"n": previous.n + 1}')],
    [edge("start", "add")],
    agent_id="ag_inner0001",
    name="Add one",
)


def outer(runs: str = "ag_inner0001") -> dict[str, Any]:
    return agent(
        [START_NODE, saved_node(runs), function("done", "Done", "previous")],
        [edge("start", "check"), edge("check", "done")],
        agent_id="ag_outer0001",
        name="Outer",
    )


def test_a_saved_agent_is_built_and_nested_whole() -> None:
    asked: list[str] = []

    def resolve(agent_id: str) -> dict[str, Any] | None:
        asked.append(agent_id)
        return INNER if agent_id == INNER["id"] else None

    graph = build_agent(outer(), resolve=resolve)
    assert asked == ["ag_inner0001"]
    nested = by_name(graph)["check_the_number"]
    assert isinstance(nested, Workflow)
    assert set(by_name(nested)) == {
        START.name,
        "__input__",
        "add_one",
        "__ended__",
        "__finish__",
    }
    assert routes(graph)[("check_the_number", "done")] is None


def test_a_saved_agent_that_isnt_there_is_refused() -> None:
    assert refused(outer(), resolve=lambda agent_id: None) == (
        "Node 'Check the number': the organization has no agent ag_inner0001."
    )
    assert "there's no way to find it here" in refused(outer())


def test_an_agent_that_would_run_itself_is_refused() -> None:
    assert refused(outer("ag_outer0001"), resolve=lambda agent_id: outer()) == (
        "Node 'Check the number': agent ag_outer0001 would run itself: "
        "ag_outer0001 → ag_outer0001."
    )
    # Through another agent.
    inner = agent(
        [START_NODE, saved_node("ag_outer0001", "Back out")],
        [edge("start", "check")],
        agent_id="ag_inner0001",
        name="Inner",
    )
    documents = {"ag_outer0001": outer(), "ag_inner0001": inner}
    message = refused(outer(), resolve=documents.get)
    assert message.startswith(
        "Node 'Check the number': agent ag_inner0001 can't be built. "
        "Node 'Back out': agent ag_outer0001 would run itself: "
        "ag_outer0001 → ag_inner0001 → ag_outer0001."
    )


async def started(
    graph: Workflow, state: dict[str, Any] | None = None
) -> tuple[Runner, str]:
    """A session of a graph through ADK's runner: the runner, and its ID."""
    sessions = InMemorySessionService()
    runner = Runner(node=graph, session_service=sessions)
    session = await sessions.create_session(
        app_name=runner.app_name, user_id="ada", state=state or {}
    )
    return runner, session.id


async def turn(runner: Runner, session_id: str, message: types.Content) -> list[Event]:
    """What the graph does when the person sends a message."""
    return [
        event
        async for event in runner.run_async(
            user_id="ada", session_id=session_id, new_message=message
        )
    ]


def run(
    graph: Workflow, message: types.Content, state: dict[str, Any] | None = None
) -> list[Event]:
    """Run a graph through ADK's runner, as the person sends the message."""

    async def main() -> list[Event]:
        return await turn(*await started(graph, state), message)

    return asyncio.run(main())


def said(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


@pytest.mark.parametrize(
    ("n", "route", "output"),
    [
        (4, "big", {"size": "big", "n": 8}),
        (2, "small", {"size": "small", "n": 4}),
    ],
)
def test_a_graph_of_functions_runs_the_branch_its_router_takes(
    n: int, route: str, output: dict[str, Any]
) -> None:
    # A run's start hands on the person's message: its text, parsed when it's
    # JSON, is the run's input, and what the first node is handed.
    events = run(build_agent(NUMBERS), said(json.dumps({"n": n})), {"factor": 2})
    outputs = {
        event.node_info.path.rpartition("/")[2]: event.output
        for event in events
        if event.output is not None
    }
    assert outputs == {
        "__input__@1": {"n": n},
        "double@1": {"n": n * 2},
        # The switch hands on what came into it.
        "size@1": {"n": n * 2},
        f"{route}@1": output,
        "__ended__@1": {"outcome": "succeeded", "result": output},
        "__finish__@1": {"outcome": "succeeded", "result": output},
    }
    assert [event.actions.route for event in events if event.actions.route] == [route]


def test_a_person_is_asked_and_their_answer_is_handed_on() -> None:
    document = agent(
        [
            START_NODE,
            node(
                "ask",
                "human_input",
                "Ask",
                {
                    "message": "Is {{ input.n }} right?",
                    "response_schema": {"type": "object"},
                },
            ),
            function("after", "After", '{"answer": previous, "factor": state.factor}'),
        ],
        [edge("start", "ask"), edge("ask", "after")],
    )

    async def main() -> tuple[list[types.FunctionCall], list[Event]]:
        runner, session_id = await started(build_agent(document), {"factor": 2})
        asked = [
            part.function_call
            for event in await turn(runner, session_id, said('{"n": 8}'))
            for part in (event.content.parts if event.content else None) or []
            if part.function_call
        ]
        answer = types.FunctionResponse(
            id=asked[0].id, name=asked[0].name, response={"right": True}
        )
        answered = await turn(
            runner,
            session_id,
            types.Content(role="user", parts=[types.Part(function_response=answer)]),
        )
        return asked, answered

    asked, answered = asyncio.run(main())
    assert len(asked) == 1
    assert asked[0].args is not None
    assert asked[0].args["message"] == "Is 8 right?"
    # Asked under the step's ID and where it is in the run.
    assert asked[0].id == "ask@numbers@1/ask@1"
    assert asked[0].args["payload"] == {"kind": "human_input", "step": "ask"}
    assert asked[0].args["response_schema"] == {
        "additionalProperties": True,
        "type": "object",
    }
    # Their answer is what it hands on.
    outputs = [
        event.output
        for event in answered
        if event.output is not None and event.node_info.path.endswith("/after@1")
    ]
    assert outputs == [{"answer": {"right": True}, "factor": 2}]
