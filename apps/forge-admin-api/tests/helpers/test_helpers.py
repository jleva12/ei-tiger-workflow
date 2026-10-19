"""The helper agents: helpers.yaml checked over the kits and toolsets there
are, and one entry point running any of them by name, sync or as a job.

The instruction writer and the skill creator have their own tests
(tests/instruction_writer, tests/skills); these use a helper that's only
configuration (the ``text`` kit), from a file of their own."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from casbin.persist.adapter import load_policy_line
from casbin.persist.adapters.asyncio import AsyncAdapter
from fastapi.testclient import TestClient
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.artifacts import InMemoryArtifactService
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field, SecretStr
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.assistant.runtime import AgentRuntime
from forge_admin.auth.authorization import get_enforcer, new_enforcer
from forge_admin.auth.tokens import mint_subject_token
from forge_admin.config import Settings
from forge_admin.db.base import Base
from forge_admin.db.session import get_session
from forge_admin.helpers import registry
from forge_admin.helpers.config import HelperConfig
from forge_admin.helpers.executor import Helpers
from forge_admin.models import Organization

API = "/api/v1"
ORG = "3f6c0000-0000-4000-8000-000000000001"
MEMBER, VIEWER = "member-1", "viewer-1"
NAMER = f"/organizations/{ORG}/helpers/agent_namer"
LATER = f"/organizations/{ORG}/helpers/later_namer"

HELPERS = """
helpers:
  - name: agent_namer
    title: Agent namer
    description: Names an agent.
    called: the namer
    kit: text
    permission: agents:manage
    seconds: 30
    model: fast-model
    thinking_level: minimal
    instruction: |
      You name agents. Answer with the name only: {{ never filled in }}.
  - name: later_namer
    title: Agent namer, later
    description: Names an agent in the background.
    called: the namer
    kit: text
    run: background
    permission: agents:manage
    instruction: You name agents.
"""


class Grants(AsyncAdapter):
    def __init__(self) -> None:
        self.lines = [
            f"p, org:member, {p.replace(':', ', ')}"
            for p in ("organizations:read", "agents:manage")
        ] + [
            "p, org:viewer, organizations, read",
            f"g, {MEMBER}, org:member, org:{ORG}*",
            f"g, {VIEWER}, org:viewer, org:{ORG}*",
        ]

    async def load_policy(self, model: Any) -> None:
        for line in self.lines:
            load_policy_line(line, model)

    async def save_policy(self, model: Any) -> bool:
        return True

    async def add_policy(self, sec: str, ptype: str, rule: list[str]) -> None:
        pass

    async def remove_policy(self, sec: str, ptype: str, rule: list[str]) -> None:
        pass

    async def remove_filtered_policy(
        self, sec: str, ptype: str, field_index: int, *field_values: str
    ) -> None:
        pass


class Scripted(BaseLlm):
    """Answers each call with the next of ``turns``; records the model and
    thinking level each call chose (as ``ProviderModels.select``)."""

    model: str = "scripted-model"
    turns: list[str] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)
    chosen: list[tuple[object, object]] = Field(default_factory=list)

    def select(
        self, request: LlmRequest, model: object, thinking_level: object
    ) -> None:
        self.chosen.append((model, thinking_level))

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text=self.turns.pop(0))]
            )
        )


@dataclass
class Workspace:
    client: TestClient
    model: Scripted
    tokens: dict[str, str]

    def call(self, method: str, path: str, user: str = MEMBER, body: Any = None) -> Any:
        return self.client.request(
            method,
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self.tokens[user]}"},
            json=body,
        )

    def settle(self) -> None:
        async def wait() -> None:
            app: Any = self.client.app
            tasks = list(getattr(app.state, "helper_tasks", set()))
            if tasks:
                await asyncio.gather(*tasks)

        assert self.client.portal is not None
        self.client.portal.call(wait)


def sqlite_tables() -> MetaData:
    tables = MetaData()
    for name in ("organizations", "users", "helper_jobs"):
        table = Base.metadata.tables[name].to_metadata(tables)
        for column in table.columns:
            column.server_default = None
    return tables


@pytest.fixture
def workspace(settings: Settings, tmp_path: Path) -> Iterator[Workspace]:
    helpers_file = tmp_path / "helpers.yaml"
    helpers_file.write_text(HELPERS)
    configured = settings.model_copy(
        update={
            "jwt_secret": SecretStr("s" * 40),
            "local_user_id": None,
            "agent_helpers": helpers_file,
        }
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    enforcer = new_enforcer(Grants())
    model = Scripted()
    app.state.agents = AgentRuntime.of(
        [App(name="forge", root_agent=LlmAgent(name="forge", model=model))],
        sessions=InMemorySessionService(),
        artifacts=InMemoryArtifactService(),
    )

    async def session() -> AsyncIterator[AsyncSession]:
        async with sessions() as made:
            yield made

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_enforcer] = lambda: enforcer

    async def create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(sqlite_tables().create_all)
        async with sessions() as made:
            made.add(Organization(id=ORG, name="Acme", description=""))
            await made.commit()

    tokens = {
        user: mint_subject_token(configured, user, lifetime=timedelta(minutes=5))
        for user in (MEMBER, VIEWER)
    }
    with TestClient(app) as client:
        assert client.portal is not None
        # Jobs record what they made with the app's sessions: these.
        app.state.sessionmaker = sessions
        client.portal.call(create)
        yield Workspace(client, model, tokens)
        client.portal.call(engine.dispose)


# ---------------------------------------------------------------- the configuration


def test_the_bundled_helpers_name_only_what_there_is() -> None:
    helpers = registry.load()
    writer, creator = helpers.get("instruction_writer"), helpers.get("skill_creator")
    assert writer is not None and creator is not None
    assert (writer.spec.run, writer.kit.name, writer.toolsets) == (
        "sync",
        "instruction_writer",
        (),
    )
    assert creator.spec.run == "background" and creator.spec.hand_in == "save_skill"
    assert [toolset.name for toolset, _ in creator.toolsets] == [
        "organization",
        "skill_vault",
    ]


def config(**changes: Any) -> HelperConfig:
    helper = {
        "name": "namer",
        "title": "Namer",
        "description": "Names things.",
        "called": "the namer",
        "kit": "text",
        "permission": "agents:manage",
        "instruction": "Name it.",
    } | changes
    return HelperConfig.model_validate({"helpers": [helper]})


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"kit": "nothing"}, "no kit named nothing"),
        ({"toolsets": ["nowhere"]}, "no toolset named nowhere"),
        (
            {"toolsets": [{"name": "organization", "tools": ["drop_tables"]}]},
            "organization has no tools drop_tables",
        ),
        ({"hand_in": "save_skill"}, "isn't given its hand-in tool save_skill"),
        (
            {
                "toolsets": [{"name": "organization", "tools": ["list_skills"]}],
                "hand_in": "list_mcp_servers",
            },
            "isn't given its hand-in tool list_mcp_servers",
        ),
        ({"model": "openai/huge"}, "model openai/huge isn't one the assistant may use"),
    ],
)
def test_a_configuration_naming_what_isnt_there_is_refused(
    changes: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        Helpers(
            config(**changes), registry.KITS, registry.TOOLSETS, models={"fast-model"}
        )


def test_a_file_that_isnt_one_is_refused(tmp_path: Path) -> None:
    for text in (
        "helpers: [{name: Bad Name}]",
        HELPERS + HELPERS.split("helpers:\n", 1)[1],  # the same names twice
        "helpers: [{name: x, title: X, description: x, called: x, permission: a:b, "
        "instruction: x, prompts: []}]",
    ):
        path = tmp_path / "helpers.yaml"
        path.write_text(text)
        with pytest.raises(ValueError, match="isn't a valid helper configuration"):
            registry.load(path)


# ---------------------------------------------------------------- the entry point


def test_a_helper_thats_only_configuration_answers_by_name(
    workspace: Workspace,
) -> None:
    workspace.model.turns.append("Order desk")
    answered = workspace.call(
        "POST",
        NAMER,
        body={"ask": "Name my agent", "context": {"description": "Answers orders"}},
    )
    assert answered.status_code == 200, answered.json()
    assert answered.json() == {
        "helper": "agent_namer",
        "result": {"text": "Order desk"},
        "notes": [],
        "model": "scripted-model",
    }
    [request] = workspace.model.requests
    # Its instruction as written: ADK leaves its {{ }} alone.
    assert "Answer with the name only: {{ never filled in }}." in str(
        request.config.system_instruction if request.config else ""
    )
    sent = "".join(p.text or "" for c in request.contents for p in c.parts or ())
    assert '"description": "Answers orders"' in sent
    assert sent.endswith("Name my agent")
    # Its model and thinking level, from the configuration.
    assert workspace.model.chosen == [("fast-model", "minimal")]


def test_the_entry_point_refuses_what_it_should(workspace: Workspace) -> None:
    assert (
        workspace.call(
            "POST", f"/organizations/{ORG}/helpers/nobody", body={}
        ).status_code
        == 404
    )
    assert workspace.call("POST", NAMER, VIEWER, body={"ask": "x"}).status_code == 403
    refused = workspace.call("POST", NAMER, body={"ask": "x", "extra": 1})
    assert refused.status_code == 422
    assert refused.json()["detail"][0]["loc"] == ["body", "extra"]
    assert workspace.call("POST", NAMER, body={"ask": "  "}).status_code == 422
    assert workspace.model.requests == []


def test_a_silent_helper_is_a_bad_gateway(workspace: Workspace) -> None:
    workspace.model.turns.append("   ")
    silent = workspace.call("POST", NAMER, body={"ask": "Name it"})
    assert silent.status_code == 502
    assert silent.json()["detail"] == "The namer answered nothing; try again."


def test_a_background_helper_runs_as_a_job(workspace: Workspace) -> None:
    workspace.model.turns.append("Order desk")
    started = workspace.call("POST", LATER, body={"ask": "Name my agent"})
    assert started.status_code == 202, started.json()
    job = started.json()
    assert (job["helper"], job["status"], job["input"]) == (
        "later_namer",
        "running",
        {"ask": "Name my agent", "context": None},
    )
    workspace.settle()
    [done] = workspace.call("GET", f"{LATER}/jobs", VIEWER).json()
    assert (done["id"], done["status"], done["result"], done["model"]) == (
        job["id"],
        "done",
        {"text": "Order desk"},
        "scripted-model",
    )
    # Each helper lists its own jobs.
    assert workspace.call("GET", f"{NAMER}/jobs").json() == []
    assert workspace.call("DELETE", f"{NAMER}/jobs/{job['id']}").status_code == 404
    assert (
        workspace.call("DELETE", f"{LATER}/jobs/{job['id']}", VIEWER).status_code == 403
    )
    assert workspace.call("DELETE", f"{LATER}/jobs/{job['id']}").status_code == 204
    assert workspace.call("GET", f"{LATER}/jobs").json() == []
