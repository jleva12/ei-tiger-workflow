"""What the assistant has on each screen: screens.yaml, and the agents built on it."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.genai import types
from scripted_llm import (
    BASE,
    SESSIONS,
    USER,
    ScriptedLlm,
    delegate,
    finish,
    run,
    sse_events,
)

from forge_admin.agents import forge, language_models
from forge_admin.agents.page_context import PAGE_CONTEXT, PageContext
from forge_admin.agents.screens import (
    STICKY,
    AssistantToolset,
    ScreenConfig,
    Screens,
)
from forge_admin.config import Settings

Client = Callable[..., TestClient]
ORG = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e03"


def page(
    view: str | None = None, *kinds: str, route: str = "/organizations/$organizationId"
) -> dict:
    """An organization workspace page, as the web console reports it."""
    return {
        "path": f"/organizations/{ORG}",
        "route": route,
        "params": {"organizationId": ORG},
        "search": {"view": view} if view else {},
        "entities": [{"kind": "organization", "id": ORG}]
        + [{"kind": kind, "id": f"{kind}-1"} for kind in kinds],
    }


def context(value: dict | None) -> PageContext | None:
    return None if value is None else PageContext.model_validate(value)


# -- The bundled configuration ---------------------------------------------


def bundled(settings: Settings) -> Screens:
    app = forge.create_app(settings, model=ScriptedLlm())
    screens = app.root_agent.screens  # type: ignore[attr-defined]
    assert isinstance(screens, Screens)
    return screens


def test_the_bundled_screens_name_only_what_exists(settings: Settings) -> None:
    # Loading checks every toolset and tool it names against the real ones.
    screens = bundled(settings)
    assert set(screens.toolsets) == {"access", "administration"}
    names = [screen.name for screen in screens.config.screens]
    # The catch-all organization screen comes after every other organization screen.
    organization = names.index("organization")
    assert all(not n.startswith("organization-") for n in names[organization + 1 :])


BUILDER = "/organizations/$organizationId_/agents/$agentId"


@pytest.mark.parametrize(
    ("where", "screens", "access"),
    [
        (None, [], "some"),
        (page(route="/"), [], "some"),
        (page(), ["organization"], "some"),
        (page("chat-agents"), ["organization"], "some"),
        (page("config"), ["organization-config", "organization"], "all"),
        (page(route=BUILDER), [], "some"),
    ],
)
def test_the_bundled_screens_match_the_organization_workspace(
    settings: Settings, where: dict | None, screens: list[str], access: str
) -> None:
    resolved = bundled(settings).resolve(context(where))

    assert [screen.name for screen in resolved.screens] == screens
    assert ("all" if resolved.toolsets["access"] is None else "some") == access
    assert set(resolved.toolsets) == {"access"}
    assert len(resolved.prompts) <= 6


def test_the_organization_workspace_says_what_no_specialist_reaches(
    settings: Settings,
) -> None:
    resolved = bundled(settings).resolve(context(page()))
    [instruction] = resolved.instructions
    assert "ADK workflows" in instruction and "agentRun" in instruction


# -- Configurations that can't load ----------------------------------------


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "screens.yaml"
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("screens: [", "isn't a valid screen configuration"),
        ("screens:\n  - name: x\n    title: X\n", "when"),
        ("everywhere:\n  toolsets: [nope]\n", "no toolset named nope"),
        (
            "screens:\n  - {name: x, title: X, when: {},"
            " toolsets: [{name: tasks, tools: [nope]}]}\n",
            "tasks has no tools nope",
        ),
        (
            "screens:\n  - {name: x, title: X, when: {}, model: gpt-9}\n",
            "model gpt-9 isn't one",
        ),
        (
            "screens:\n  - {name: x, title: X, when: {}}\n  - {name: x, title: Y, when: {}}\n",
            "must be unique: x",
        ),
        ("screens:\n  - {name: x, title: X, when: {view: a}}\n", "Extra inputs"),
    ],
)
def test_a_configuration_that_names_what_doesnt_exist_fails_to_load(
    tmp_path: Path, text: str, problem: str
) -> None:
    tasks = AssistantToolset("tasks", "Tasks", "", None, tools=frozenset({"list"}))
    with pytest.raises(ValueError, match=problem):
        Screens(ScreenConfig.load(write(tmp_path, text)), [tasks], models={"m"})


def test_a_configuration_file_of_its_own_replaces_the_bundled_one(
    settings: Settings, tmp_path: Path
) -> None:
    path = write(
        tmp_path,
        "screens:\n  - {name: home, title: Home, when: {route: /}, prompts: [Hi]}\n",
    )
    app = forge.create_app(
        settings.model_copy(update={"agent_screens": path}), model=ScriptedLlm()
    )
    resolved = app.root_agent.screens.resolve(context(page(route="/")))  # type: ignore[attr-defined]
    assert [p.text for p in resolved.prompts] == ["Hi"]


# -- The screens, the specialists' toolsets and instructions -----------------


def read_tasks() -> str:
    """Reads the tasks."""
    return "tasks"


def cancel_task() -> str:
    """Cancels a task.

    Args:
        nothing: Nothing.
    """
    return "cancelled"


def search_code() -> str:
    """Searches code."""
    return "code"


class Stub(BaseToolset):
    def __init__(self, *tools: BaseTool) -> None:
        super().__init__()
        self.tools = list(tools)
        self.closed = False

    async def get_tools(
        self, readonly_context: ReadonlyContext | None = None
    ) -> list[BaseTool]:
        return self.tools

    async def close(self) -> None:
        self.closed = True


CONFIG = """
everywhere:
  toolsets: [code]
screens:
  - name: tasks
    title: Tasks
    when: {route: /organizations/$organizationId, search: {view: [tasks, board]}}
    toolsets: [tasks]
    instructions: On the tasks page.
    model: fast
    thinking_level: low
    prompts:
      - {title: "What's running", label: "now?"}
  - name: task
    title: A task
    when: {entity: task}
    toolsets:
      - {name: tasks, tools: [read_tasks]}
  - name: offline
    title: Offline
    when: {route: /offline}
    toolsets: [offline]
"""


@pytest.fixture
def screens(tmp_path: Path) -> Screens:
    return Screens(
        ScreenConfig.load(write(tmp_path, CONFIG)),
        [
            AssistantToolset(
                "tasks",
                "Tasks",
                "Reads and changes tasks.",
                Stub(
                    FunctionTool(read_tasks),
                    FunctionTool(cancel_task, require_confirmation=True),
                ),
                instruction="\nUse the task tools.\n",
                tools=frozenset({"read_tasks", "cancel_task"}),
            ),
            AssistantToolset(
                "code", "Code", "Reads code.", Stub(FunctionTool(search_code))
            ),
            # Named by a screen, but not set up here.
            AssistantToolset("offline", "Offline", "Not here.", None),
        ],
        models={"fast", "slow"},
    )


def names(screens: Screens, state: dict[str, Any]) -> dict[str, list[str]]:
    """Each specialist's tools, for a conversation in ``state``."""
    context = SimpleNamespace(state=state, invocation_id=str(uuid4()))
    return {
        entry.name: sorted(
            tool.name
            for tool in asyncio.run(screens.toolset(entry.name).get_tools(context))  # type: ignore[arg-type]
        )
        for entry in screens.available()
    }


def test_each_specialist_has_its_tools_the_page_and_the_conversation_give(
    screens: Screens,
) -> None:
    # Only the toolsets that are set up have specialists.
    assert [entry.name for entry in screens.available()] == ["tasks", "code"]
    assert names(screens, {}) == {"tasks": [], "code": ["search_code"]}
    assert names(screens, {PAGE_CONTEXT: page("tasks")}) == {
        "tasks": ["cancel_task", "read_tasks"],
        "code": ["search_code"],
    }
    # A task elsewhere gives only the tools its screen names.
    assert names(screens, {PAGE_CONTEXT: page("home", "task")}) == {
        "tasks": ["read_tasks"],
        "code": ["search_code"],
    }
    # What the conversation kept stays, whatever the page.
    kept = {PAGE_CONTEXT: page(route="/"), STICKY: {"tasks": None}}
    assert names(screens, kept)["tasks"] == ["cancel_task", "read_tasks"]
    # A toolset that isn't set up is never offered, kept or not.
    offline = {PAGE_CONTEXT: page(route="/offline"), STICKY: {"offline": None}}
    assert "offline" not in screens.active(offline)
    with pytest.raises(ValueError, match="isn't set up"):
        screens.toolset("offline")


def test_a_conversation_keeps_the_toolsets_it_has_had(
    screens: Screens,
) -> None:
    state: dict[str, Any] = {PAGE_CONTEXT: page("home", "task")}
    written: list[dict[str, Any]] = []

    class State(dict):
        def to_dict(self) -> dict[str, Any]:
            return dict(self)

        def __setitem__(self, key: str, value: Any) -> None:
            written.append({key: value})
            super().__setitem__(key, value)

    callback = SimpleNamespace(state=State(state))
    screens.remember(callback)  # type: ignore[arg-type]
    assert callback.state[STICKY] == {"code": None, "tasks": ["read_tasks"]}

    # Nothing new, nothing written.
    screens.remember(callback)  # type: ignore[arg-type]
    assert len(written) == 1

    # A page with more of a toolset widens what's kept.
    callback.state[PAGE_CONTEXT] = page("tasks")
    screens.remember(callback)  # type: ignore[arg-type]
    assert callback.state[STICKY] == {"code": None, "tasks": None}


def on(where: dict[str, Any]) -> Any:
    return SimpleNamespace(state={PAGE_CONTEXT: where})


def test_the_supervisors_instruction_follows_the_page(screens: Screens) -> None:
    provide = forge.supervisor_instruction(screens)

    home = provide(on(page(route="/")))
    assert "hand it on in turn" in home
    assert "- Tasks: on the Tasks, A task pages." in home

    tasks = provide(on(page("tasks")))
    assert "On the tasks page." in tasks
    assert "Specialists you don't have on this page" not in tasks
    # How to use a toolset is its specialist's business.
    assert "Use the task tools." not in tasks


def test_a_specialists_instruction_is_its_toolsets(screens: Screens) -> None:
    provide = forge.specialist_instruction(screens, screens.toolsets["tasks"])

    tasks = provide(on(page("tasks")))
    assert "Your part: Reads and changes tasks." in tasks
    assert "Use the task tools." in tasks
    assert "On the tasks page." in tasks
    assert "call finish_task" in tasks
    assert "only some of your tools" not in tasks

    # Where the page gives only some of its tools, it says where the rest are.
    task = provide(on(page("home", "task")))
    assert "only some of your tools" in task
    assert "the Tasks, A task pages" in task


def test_a_screen_sets_the_model_until_the_conversation_chooses(
    settings: Settings, screens: Screens
) -> None:
    allowed = settings.model_copy(update={"agent_models": ["fast", "slow"]})
    models = language_models.provider_models(allowed)
    select = forge.model_selection(models, screens)

    def model(state: dict[str, Any]) -> str | None:
        request = LlmRequest()

        class State(dict):
            def to_dict(self) -> dict[str, Any]:
                return dict(self)

        select(SimpleNamespace(state=State(state)), request)  # type: ignore[arg-type]
        return request.model

    assert model({PAGE_CONTEXT: page("tasks")}) == "google/fast"
    assert model({PAGE_CONTEXT: page("tasks"), "model": "slow"}) == "google/slow"
    assert model({PAGE_CONTEXT: page(route="/")}) == "google/gemini-3.5-flash"


def test_described_for_the_person(screens: Screens) -> None:
    described = asyncio.run(
        screens.describe(
            user_id="ada",
            page=context(page("home", "task")),
            sticky={"tasks": ["cancel_task"]},
            tools=True,
        )
    )

    assert described["screens"] == [{"name": "task", "title": "A task"}]
    assert described["toolsets"] == [
        {
            "name": "tasks",
            "agent": "tasks",
            "title": "Tasks",
            "description": "Reads and changes tasks.",
            "fromEarlier": False,
            "tools": [
                {
                    "name": "read_tasks",
                    "description": "Reads the tasks.",
                    "asksFirst": False,
                },
                {
                    "name": "cancel_task",
                    "description": "Cancels a task.",
                    "asksFirst": True,
                },
            ],
        },
        {
            "name": "code",
            "agent": "code",
            "title": "Code",
            "description": "Reads code.",
            "fromEarlier": False,
            "tools": [
                {
                    "name": "search_code",
                    "description": "Searches code.",
                    "asksFirst": False,
                }
            ],
        },
    ]
    assert described["prompts"] == []
    # Every specialist, whatever the page, for its title.
    assert described["specialists"] == [
        {"name": "tasks", "title": "Tasks"},
        {"name": "code", "title": "Code"},
    ]

    here = asyncio.run(screens.describe(user_id="ada", page=context(page("tasks"))))
    assert here["prompts"] == [
        {"title": "What's running", "label": "now?", "prompt": "What's running now?"}
    ]
    assert "tools" not in here["toolsets"][0]


# -- Through the /agents routes ---------------------------------------------


def app_on(screens: Screens, model: ScriptedLlm) -> App:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, mysql_password="x", agent_models=["fast", "slow"]
    )
    models = language_models.provider_models(settings, build=lambda call: model)
    return App(name="forge", root_agent=forge.supervisor(screens, models))


def test_closing_the_runner_closes_every_toolset(screens: Screens) -> None:
    runner = Runner(
        app=app_on(screens, ScriptedLlm()), session_service=InMemorySessionService()
    )
    asyncio.run(runner.close())
    assert all(
        entry.toolset.closed  # type: ignore[attr-defined]
        for entry in screens.available()
    )


def test_the_capabilities_route(agent_client: Client, screens: Screens) -> None:
    client = agent_client(app_on(screens, ScriptedLlm()))
    capabilities = f"{BASE}/apps/forge/users/{USER}/capabilities"

    answer = client.post(
        capabilities, json={"pageContext": page("tasks"), "tools": True}
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["screens"] == [{"name": "tasks", "title": "Tasks"}]
    assert [t["name"] for t in body["toolsets"]] == ["tasks", "code"]
    assert body["prompts"][0]["prompt"] == "What's running now?"

    # Without a page, what it has everywhere.
    bare = client.post(capabilities, json={}).json()
    assert [t["name"] for t in bare["toolsets"]] == ["code"]
    # A malformed page counts as none.
    assert client.post(capabilities, json={"pageContext": {"path": 7}}).json() == bare

    # Only the caller's own, and their own conversations.
    assert (
        client.post(f"{BASE}/apps/forge/users/bob/capabilities", json={}).status_code
        == 403
    )
    missing = client.post(capabilities, json={"sessionId": "nope"})
    assert missing.status_code == 404


def test_the_supervisor_is_offered_the_pages_specialists(
    agent_client: Client, screens: Screens
) -> None:
    llm = ScriptedLlm(turns=[[types.Part(text="Hello.")]])
    client = agent_client(app_on(screens, llm))
    session = client.post(SESSIONS, json={}).json()["id"]

    home = {PAGE_CONTEXT: page(route="/")}
    sse_events(run(client, session, {"text": "Hi"}, stateDelta=home))

    [request] = llm.requests
    assert sorted(request.tools_dict) == ["code"]
    declared = [
        declaration.name
        for tool in request.config.tools or []
        for declaration in tool.function_declarations or []
    ]
    assert declared == ["code"]


def test_a_conversation_keeps_its_specialists_when_the_person_moves_on(
    agent_client: Client, screens: Screens
) -> None:
    llm = ScriptedLlm(
        turns=[
            [delegate("tasks", "Read the tasks")],
            [types.Part(function_call=types.FunctionCall(name="read_tasks", args={}))],
            [finish("There are tasks.")],
            [types.Part(text="On tasks.")],
            [types.Part(text="Home.")],
        ]
    )
    client = agent_client(app_on(screens, llm))
    session = client.post(SESSIONS, json={}).json()["id"]
    message = {"text": "Hi"}

    sse_events(run(client, session, message, stateDelta={PAGE_CONTEXT: page("tasks")}))
    sse_events(
        run(client, session, message, stateDelta={PAGE_CONTEXT: page(route="/")})
    )

    declared = [sorted(request.tools_dict) for request in llm.requests]
    assert declared == [
        ["code", "tasks"],
        ["cancel_task", "finish_task", "read_tasks"],
        ["cancel_task", "finish_task", "read_tasks"],
        ["code", "tasks"],
        ["code", "tasks"],
    ]
    # The screen's model ran the first turn, supervisor and specialist alike;
    # the second has no screen.
    assert [request.model for request in llm.requests][:4] == ["fast"] * 4

    kept = client.post(
        f"{BASE}/apps/forge/users/{USER}/capabilities",
        json={"pageContext": page(route="/"), "sessionId": session},
    ).json()
    assert {t["name"]: t["fromEarlier"] for t in kept["toolsets"]} == {
        "tasks": True,
        "code": False,
    }


# -- The supervisor and its specialists ---------------------------------------


def calls(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Who called what, in order."""
    return [
        (event["author"], part["functionCall"]["name"])
        for event in events
        for part in event.get("content", {}).get("parts", [])
        if "functionCall" in part
    ]


def answered(events: list[dict[str, Any]], name: str) -> list[Any]:
    return [
        part["functionResponse"]["response"]
        for event in events
        for part in event.get("content", {}).get("parts", [])
        if part.get("functionResponse", {}).get("name") == name
    ]


def test_a_specialist_asks_the_person_to_confirm_a_change(
    agent_client: Client, screens: Screens
) -> None:
    cancel = types.FunctionCall(name="cancel_task", args={})
    llm = ScriptedLlm(
        turns=[
            [delegate("tasks", "Cancel task t-1")],
            [types.Part(function_call=cancel)],
            [finish("Cancelled t-1.")],
            [types.Part(text="Task t-1 is cancelled.")],
        ]
    )
    client = agent_client(app_on(screens, llm))
    session = client.post(SESSIONS, json={}).json()["id"]
    tasks = {PAGE_CONTEXT: page("tasks")}

    asked = sse_events(run(client, session, {"text": "Cancel t-1"}, stateDelta=tasks))

    assert calls(asked) == [
        ("forge", "tasks"),
        ("tasks", "cancel_task"),
        ("tasks", "adk_request_confirmation"),
    ]
    assert answered(asked, "cancel_task") == [
        {"error": "This tool call requires confirmation, please approve or reject."}
    ]
    [request] = [
        part["functionCall"]
        for event in asked
        for part in event["content"]["parts"]
        if part.get("functionCall", {}).get("name") == "adk_request_confirmation"
    ]

    # What the confirmation card's Approve sends; the specialist goes on.
    approval = {
        "functionResponse": {
            "id": request["id"],
            "name": "adk_request_confirmation",
            "response": {"confirmed": True},
        }
    }
    done = sse_events(run(client, session, approval))

    assert answered(done, "cancel_task") == [{"result": "cancelled"}]
    assert answered(done, "tasks") == [{"result": "Cancelled t-1."}]
    assert done[-1]["author"] == "forge"
    assert done[-1]["content"]["parts"] == [{"text": "Task t-1 is cancelled."}]


def test_a_specialist_asks_the_person_a_question(
    agent_client: Client, screens: Screens
) -> None:
    llm = ScriptedLlm(
        turns=[
            [delegate("tasks", "Cancel my task")],
            [types.Part(text="Which task: t-1 or t-2?")],
            [finish("They meant t-2; it's running.")],
            [types.Part(text="t-2 is running.")],
            [types.Part(text="You're welcome.")],
        ]
    )
    client = agent_client(app_on(screens, llm))
    session = client.post(SESSIONS, json={}).json()["id"]
    tasks = {PAGE_CONTEXT: page("tasks")}

    question = sse_events(
        run(client, session, {"text": "Cancel my task"}, stateDelta=tasks)
    )
    assert question[-1]["author"] == "tasks"
    assert question[-1]["content"]["parts"] == [{"text": "Which task: t-1 or t-2?"}]

    # The answer goes to the specialist, which reports back to the supervisor.
    reply = sse_events(run(client, session, {"text": "t-2"}))
    specialist = llm.requests[2]
    assert "finish_task" in specialist.tools_dict
    assert [c.parts[0].text for c in specialist.contents][1:] == [
        "Which task: t-1 or t-2?",
        "t-2",
    ]
    assert reply[-1]["author"] == "forge"
    assert reply[-1]["content"]["parts"] == [{"text": "t-2 is running."}]

    # The next message is the supervisor's again.
    sse_events(run(client, session, {"text": "Thanks"}))
    assert "tasks" in llm.requests[-1].tools_dict


def test_the_supervisor_hands_a_request_on_in_turn(
    agent_client: Client, screens: Screens
) -> None:
    search = types.FunctionCall(name="search_code", args={})
    read = types.FunctionCall(name="read_tasks", args={})
    llm = ScriptedLlm(
        turns=[
            [delegate("code", "Find the code")],
            [types.Part(function_call=search)],
            [finish("It's in billing.py.")],
            [delegate("tasks", "Read the tasks about billing.py")],
            [types.Part(function_call=read)],
            [finish("One task changes it.")],
            [types.Part(text="billing.py; one task changes it.")],
        ]
    )
    client = agent_client(app_on(screens, llm))
    session = client.post(SESSIONS, json={}).json()["id"]

    events = sse_events(
        run(
            client,
            session,
            {"text": "Where's billing, and who's changing it?"},
            stateDelta={PAGE_CONTEXT: page("tasks")},
        )
    )

    assert [c for c in calls(events) if c[1] != "finish_task"] == [
        ("forge", "code"),
        ("code", "search_code"),
        ("forge", "tasks"),
        ("tasks", "read_tasks"),
    ]
    # The second specialist sees only its own request.
    assert [c.parts[0].text for c in llm.requests[4].contents] == [
        '{"request": "Read the tasks about billing.py"}'
    ]
    assert events[-1]["content"]["parts"] == [
        {"text": "billing.py; one task changes it."}
    ]
