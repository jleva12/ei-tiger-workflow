"""Organizations' chat agents, against MySQL and MongoDB: a draft is saved as
it's edited, publishing freezes it as a version nothing changes, a new
version is a draft beside the published ones, and the runtime runs each by ID.

A site administrator builds an organization. Each test keeps its agents in a
MongoDB database of its own, dropped afterwards. make admin-test-mysql starts
both admin-mysql and MongoDB.
"""

import copy
import io
import json
import zipfile
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from forge_agent_runtime import AgentExecutor
from forge_common.adk.models import ProviderModels
from forge_common.model_provider import parse_model_provider_yaml
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field
from pymongo import MongoClient

from forge_admin.api.app import PUBLIC_ROUTERS, ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.chat_agents.source import MongoAgentSource
from forge_admin.config import Settings

pytestmark = pytest.mark.mysql

API = "/api/v1"
RUNTIME = f"{API}/runtime"
# The builder's example chat agent, kept with the runtime package's tests.
EXAMPLE = (
    Path(__file__).parents[4]
    / "packages/python/agent-runtime/tests/fixtures/support-assistant.chat-agent.json"
)
PROVIDERS = """
version: 1
default: {provider: openai, model: gpt-test}
providers:
  openai:
    baseUrl: https://api.openai.com/v1
    api: openai-responses
    auth: {type: apiKey, key: sk-test}
    models: [{id: gpt-test}]
"""


class ScriptedLlm(BaseLlm):
    model: str = "gpt-test"
    turns: list[Any] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        yield LlmResponse(content=types.Content(role="model", parts=self.turns.pop(0)))


def example(instruction: str = "Help {{ request.userId }}.") -> dict[str, Any]:
    doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    # Tools that would reach the network: left out, so its versions build offline.
    kept = {"agent", "billing"}
    doc["nodes"] = [n for n in doc["nodes"] if n["id"] in kept]
    doc["edges"] = [
        e for e in doc["edges"] if e["source"] in kept and e["target"] in kept
    ]
    doc["nodes"][0]["config"]["instruction"] = instruction
    return doc


@dataclass
class Org:
    id: str
    member: TestClient
    viewer: TestClient
    outsider: TestClient
    llm: ScriptedLlm

    @property
    def agents(self) -> str:
        return f"{API}/organizations/{self.id}/chat-agents"


@pytest.fixture
def org(site_admin: str, mysql_settings: Settings, client_as: Any) -> Iterator[Org]:
    if mysql_settings.mongo_uri is None:
        pytest.skip("needs MongoDB: set FORGE_ADMIN_MONGO_URI (make env)")
    database = f"forge_admin_test_{uuid4().hex[:8]}"
    clients: list[TestClient] = []
    llm = ScriptedLlm()
    models = ProviderModels(parse_model_provider_yaml(PROVIDERS), build=lambda _: llm)

    def client_for(user: str) -> TestClient:
        settings = mysql_settings.model_copy(
            update={
                "local_user_id": user,
                "mongo_database": database,
                # Generated projects pin the runtime: no wheels needed here.
                "starter_runtime": "pypi:0.1.0",
            }
        )
        client = TestClient(
            ApiServer(
                settings, routers=ROUTERS, public_routers=PUBLIC_ROUTERS
            ).create_app()
        )
        client.__enter__()
        state = client.app.state  # type: ignore[attr-defined]
        # A scripted model, and conversations in memory.
        state.agent_executor = AgentExecutor(
            MongoAgentSource(state.chat_agents),
            models=models,
            sessions=InMemorySessionService(),
        )
        clients.append(client)
        return client

    admin = client_for(site_admin)
    tag = uuid4().hex[:8]
    made = admin.post(f"{API}/organizations", json={"name": f"Chat {tag}"})
    organization_id = made.json()["id"]
    member, viewer = f"member-{tag}", f"viewer-{tag}"
    for user, role in ((member, "org:member"), (viewer, "org:viewer")):
        assigned = admin.put(
            f"{API}/scopes/org:{organization_id}/members/{user}/roles/{role}"
        )
        assert assigned.status_code == 200
    yield Org(
        organization_id,
        client_for(member),
        client_for(viewer),
        client_for(f"outsider-{tag}"),
        llm,
    )
    admin.delete(f"{API}/organizations/{organization_id}")
    for client in clients:
        client.__exit__(None, None, None)
    mongo: MongoClient = MongoClient(mysql_settings.mongo_uri.get_secret_value())
    mongo.drop_database(database)
    mongo.close()


def create(org: Org, doc: dict[str, Any] | None = None) -> dict[str, Any]:
    made = org.member.post(org.agents, json={"document": doc or example()})
    assert made.status_code == 201, made.json()
    return made.json()


def events(response: Any) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    return [
        json.loads(m.removeprefix("data: "))
        for m in response.text.split("\n\n")
        if m.startswith("data: ")
    ]


def talk(org: Org, app: str, text: str, user: str = "ada") -> str:
    """One turn with an agent, as any caller of the public runtime would: the reply."""
    client = org.outsider  # The runtime is public: anyone can talk to an agent.
    session = client.post(f"{RUNTIME}/apps/{app}/users/{user}/sessions", json={})
    assert session.status_code == 201, session.text
    got = events(
        client.post(
            f"{RUNTIME}/run_sse",
            json={
                "appName": app,
                "userId": user,
                "sessionId": session.json()["id"],
                "newMessage": {"role": "user", "parts": [{"text": text}]},
            },
        )
    )
    return got[-1]["content"]["parts"][0]["text"]


def a2a(org: Org, app: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """One A2A JSON-RPC call to an agent, as any caller of the public runtime would."""
    response = org.outsider.post(
        f"{RUNTIME}/a2a/{app}",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"A2A-Version": "1.0"} if method[0].isupper() else {},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_published_agent_answers_over_a2a_too(org: Org) -> None:
    agent = create(org)["id"]
    org.member.post(f"{org.agents}/{agent}/publish", json={"revision": 1})

    card = org.outsider.get(f"{RUNTIME}/a2a/{agent}/.well-known/agent-card.json")
    assert card.status_code == 200, card.text
    assert card.json()["name"] == "Support assistant"
    assert card.json()["url"].endswith(f"{RUNTIME}/a2a/{agent}")

    org.llm.turns = [[types.Part(text="Hello over A2A.")]]
    context = uuid4().hex
    sent = a2a(
        org,
        agent,
        "SendMessage",
        {
            "message": {
                "messageId": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": "Hi"}],
                "contextId": context,
            }
        },
    )
    task = sent["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED", task
    assert task["artifacts"][0]["parts"][0]["text"] == "Hello over A2A."
    # Kept in MySQL: found again, and only through its own agent.
    found = a2a(org, agent, "GetTask", {"id": task["id"]})["result"]
    assert found["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "error" in a2a(org, f"{agent}@1", "ListTasks", {})

    # A2A 0.3 on the same URL, and the run API's conversation is the same one.
    org.llm.turns = [[types.Part(text="Still here.")]]
    old = a2a(
        org,
        f"{agent}@1",
        "message/send",
        {
            "message": {
                "kind": "message",
                "messageId": "m2",
                "role": "user",
                "parts": [{"kind": "text", "text": "Again"}],
                "contextId": context,
            }
        },
    )
    assert old["result"]["status"]["state"] == "completed"
    session = org.outsider.get(
        f"{RUNTIME}/apps/{agent}/users/A2A_USER_{context}/sessions/{context}"
    ).json()
    said = [
        part["text"]
        for event in session["events"]
        for part in event["content"]["parts"]
        if "text" in part
    ]
    assert said == ["Hi", "Hello over A2A.", "Again", "Still here."]

    missing = org.outsider.get(f"{RUNTIME}/a2a/ca_nobody/.well-known/agent-card.json")
    assert missing.status_code == 404


def test_a_draft_is_saved_then_published_as_a_version_nothing_changes(org: Org) -> None:
    made = create(org)
    agent = made["id"]
    assert agent.startswith("ca_")
    assert (made["status"], made["draft_version"], made["published_version"]) == (
        "draft",
        1,
        None,
    )

    doc = copy.deepcopy(made["document"])
    doc["description"] = "Answers customers."
    saved = org.member.put(
        f"{org.agents}/{agent}/draft", json={"document": doc, "revision": 1}
    )
    assert saved.status_code == 200, saved.json()
    stale = org.member.put(
        f"{org.agents}/{agent}/draft", json={"document": doc, "revision": 1}
    )
    assert stale.status_code == 409

    stale_publish = org.member.post(
        f"{org.agents}/{agent}/publish", json={"revision": 1}
    )
    assert stale_publish.status_code == 409
    published = org.member.post(f"{org.agents}/{agent}/publish", json={"revision": 2})
    assert published.status_code == 200, published.json()
    assert (published.json()["status"], published.json()["published_version"]) == (
        "published",
        1,
    )

    # Nothing changes a published version: there's no draft to save to.
    frozen = org.member.put(
        f"{org.agents}/{agent}/draft",
        json={"document": doc, "revision": published.json()["revision"]},
    )
    assert frozen.status_code == 409
    assert frozen.json()["detail"]["code"] == "NO_DRAFT"
    again = org.member.post(
        f"{org.agents}/{agent}/publish", json={"revision": published.json()["revision"]}
    )
    assert again.json()["detail"]["code"] == "NO_DRAFT"

    version = org.viewer.get(f"{org.agents}/{agent}/versions/1")
    assert version.status_code == 200
    assert version.json()["document"]["version"] == 1
    assert version.json()["document"]["description"] == "Answers customers."
    detail = org.viewer.get(f"{org.agents}/{agent}").json()
    assert [v["version"] for v in detail["versions"]] == [1]


def test_a_new_version_is_drafted_while_the_published_one_runs(org: Org) -> None:
    agent = create(org, example("Version one for {{ request.userId }}."))["id"]
    published = org.member.post(
        f"{org.agents}/{agent}/publish", json={"revision": 1}
    ).json()

    drafted = org.member.post(f"{org.agents}/{agent}/versions", json={})
    assert drafted.status_code == 200, drafted.json()
    assert (drafted.json()["status"], drafted.json()["draft_version"]) == (
        "published+draft",
        2,
    )
    twice = org.member.post(f"{org.agents}/{agent}/versions", json={})
    assert twice.json()["detail"]["code"] == "DRAFT_EXISTS"

    doc = drafted.json()["document"]
    doc["nodes"][0]["config"]["instruction"] = "Version two for {{ request.userId }}."
    org.member.put(
        f"{org.agents}/{agent}/draft",
        json={"document": doc, "revision": drafted.json()["revision"]},
    )

    org.llm.turns = [[types.Part(text=reply)] for reply in ("one", "two", "pinned")]
    assert talk(org, agent, "hi") == "one"
    assert org.llm.requests[-1].config.system_instruction.startswith(
        "Version one for ada."
    )
    assert talk(org, f"{agent}@draft", "hi") == "two"
    assert org.llm.requests[-1].config.system_instruction.startswith(
        "Version two for ada."
    )
    assert talk(org, f"{agent}@1", "hi") == "pinned"

    # Publishing the new version makes it the one the agent's ID runs.
    current = org.member.get(f"{org.agents}/{agent}").json()
    second = org.member.post(
        f"{org.agents}/{agent}/publish", json={"revision": current["revision"]}
    )
    assert second.json()["published_version"] == 2
    org.llm.turns = [[types.Part(text="two again")]]
    assert talk(org, agent, "hi") == "two again"
    assert org.llm.requests[-1].config.system_instruction.startswith("Version two")
    assert published["published_version"] == 1


def test_a_draft_that_cant_run_isnt_published(org: Org) -> None:
    doc = example()
    doc["nodes"][0]["name"] = "User"  # ADK keeps that name for the person.
    agent = create(org, doc)["id"]
    refused = org.member.post(f"{org.agents}/{agent}/publish", json={"revision": 1})
    assert refused.status_code == 422
    assert "keeps that name" in json.dumps(refused.json())
    assert org.outsider.get(f"{RUNTIME}/apps/{agent}").status_code == 404
    assert org.outsider.get(f"{RUNTIME}/apps/{agent}@draft").status_code == 422


def test_viewers_read_outsiders_dont_and_discarding_returns_to_published(
    org: Org,
) -> None:
    agent = create(org)["id"]
    assert org.viewer.post(org.agents, json={"document": example()}).status_code == 403
    assert org.outsider.get(f"{org.agents}/{agent}").status_code in (403, 404)
    assert org.viewer.get(org.agents).json()[0]["id"] == agent

    no_publish_yet = org.member.delete(f"{org.agents}/{agent}/draft")
    assert no_publish_yet.json()["detail"]["code"] == "NOT_PUBLISHED"
    org.member.post(f"{org.agents}/{agent}/publish", json={"revision": 1})
    org.member.post(f"{org.agents}/{agent}/versions", json={})
    discarded = org.member.delete(f"{org.agents}/{agent}/draft")
    assert (discarded.json()["status"], discarded.json()["has_draft"]) == (
        "published",
        False,
    )

    copy_ = org.member.post(f"{org.agents}/{agent}/duplicate", json={})
    assert copy_.status_code == 201
    assert copy_.json()["id"] != agent and copy_.json()["status"] == "draft"
    assert org.member.delete(f"{org.agents}/{agent}").status_code == 204
    assert org.outsider.get(f"{RUNTIME}/apps/{agent}").status_code == 404


def test_an_export_bundles_the_saved_agents_it_uses(org: Org) -> None:
    helper = create(org, example("I'm the helper."))["id"]
    org.member.post(f"{org.agents}/{helper}/publish", json={"revision": 1})

    doc = example()
    doc["nodes"].append(
        {
            "id": "saved",
            "kind": "saved_agent",
            "name": "Helper",
            "config": {"agent": helper},
            "outputs": [],
        }
    )
    doc["edges"].append(
        {"id": "s", "source": "agent", "source_output": "tools", "target": "saved"}
    )
    agent = create(org, doc)["id"]
    published = org.member.post(f"{org.agents}/{agent}/publish", json={"revision": 1})
    assert published.status_code == 200, published.json()

    exported = org.viewer.get(f"{org.agents}/{agent}/versions/1/export")
    assert exported.status_code == 200
    assert list(exported.json()["dependencies"]) == [f"{helper}@1"]
    assert exported.json()["version"] == 1


def test_a_version_and_the_draft_become_standalone_projects(org: Org) -> None:
    agent = create(org)["id"]
    org.member.post(f"{org.agents}/{agent}/publish", json={"revision": 1})
    org.member.post(f"{org.agents}/{agent}/versions", json={})

    preview = org.viewer.post(
        f"{org.agents}/{agent}/standalone/preview", json={"version": 1}
    )
    assert preview.status_code == 200, preview.json()
    shown = preview.json()
    assert shown["name"] == "support-assistant" and shown["runtime"] == "pypi:0.1.0"
    assert "OPENAI_API_KEY" in shown["env"] or "GOOGLE_API_KEY" in shown["env"]
    assert {
        "main.py",
        "web/src/agent.tsx",
        "agent/support-assistant.chat-agent.json",
    } <= set(shown["files"])

    zipped = org.viewer.post(
        f"{org.agents}/{agent}/standalone", json={"version": 1, "name": "Shop helper"}
    )
    assert zipped.status_code == 200
    assert zipped.headers["content-type"] == "application/zip"
    assert 'filename="shop-helper.zip"' in zipped.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        exported = json.loads(
            archive.read("shop-helper/agent/shop-helper.chat-agent.json")
        )
        assert (exported["id"], exported["version"]) == (agent, 1)
        assert (
            "forge-agent-runtime[server,a2a]>=0.1.0"
            in archive.read("shop-helper/pyproject.toml").decode()
        )

    draft = org.viewer.post(
        f"{org.agents}/{agent}/standalone/preview", json={"version": "draft"}
    )
    assert any("draft" in note for note in draft.json()["notes"])
    missing = org.viewer.post(
        f"{org.agents}/{agent}/standalone/preview", json={"version": 7}
    )
    assert missing.status_code == 404
    outsider = org.outsider.post(
        f"{org.agents}/{agent}/standalone/preview", json={"version": 1}
    )
    assert outsider.status_code in (403, 404)

    # What it's made of: the API alone, in PostgreSQL and S3, asking for a key.
    options = {
        "interface": "api",
        "sessions": "postgresql",
        "artifacts": "s3",
        "api_key": True,
        "code_owners": ["@acme/agents"],
    }
    chosen = org.viewer.post(
        f"{org.agents}/{agent}/standalone/preview",
        json={"version": 1, "options": options},
    ).json()
    assert "compose.yaml" in chosen["files"] and ".github/CODEOWNERS" in chosen["files"]
    assert not any(path.startswith("web/") for path in chosen["files"])
    assert "AGENT_API_KEYS" in chosen["env"]
    zipped = org.viewer.post(
        f"{org.agents}/{agent}/standalone", json={"version": 1, "options": options}
    )
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        main = archive.read("support-assistant/main.py").decode()
        assert '.with_sessions(env("DATABASE_URL"))' in main
    refused = org.viewer.post(
        f"{org.agents}/{agent}/standalone/preview",
        json={"version": 1, "options": {"api_key": True}},
    )
    assert refused.status_code == 422
