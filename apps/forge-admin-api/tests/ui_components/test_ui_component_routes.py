"""An organization's team UI components: written, previewed, published as
versions nothing changes, disabled and archived, and offered to its agents
in its catalog, never another organization's.

MongoDB keeps the components (a database of each test's own, dropped
afterwards), as it keeps the agents that pick them; make admin-test-mysql
starts it. The rest runs without MySQL: the organizations on SQLite, their
access in memory (the default grants, ui_components:manage its admins').

The people: an organization admin, a member and a viewer of it, an admin of
another organization, and someone with no roles at all.
"""

import copy
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from casbin.persist.adapter import load_policy_line
from casbin.persist.adapters.asyncio import AsyncAdapter
from fastapi.testclient import TestClient
from forge_agent_runtime import AgentExecutor, BuildError, build_app, parse_document
from forge_agent_runtime.present import PresentTool
from forge_agent_runtime.refs import AgentRef
from forge_agent_runtime.services import RuntimeServices
from forge_common.adk.models import ProviderModels
from forge_common.model_provider import parse_model_provider_yaml
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from pydantic import SecretStr
from pymongo import AsyncMongoClient, MongoClient
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.adk_workflows.resources import RunResources
from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.auth.authorization import get_enforcer, new_enforcer
from forge_admin.auth.tokens import mint_subject_token
from forge_admin.chat_agents.bundle import bundle
from forge_admin.chat_agents.source import MongoAgentSource
from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.config import Settings
from forge_admin.db.base import Base
from forge_admin.db.session import get_session
from forge_admin.instruction_writer.brief import Brief, render, resolve
from forge_admin.models import Organization
from forge_admin.ui_components.library import (
    OrganizationUiComponents,
    UiComponentStamps,
)
from forge_admin.ui_components.store import UiComponentStore

pytestmark = pytest.mark.mysql

API = "/api/v1"
ORG = "7a1c0000-0000-4000-8000-000000000001"
OTHER_ORG = "7a1c0000-0000-4000-8000-000000000002"
ADMIN, MEMBER, VIEWER = "admin-1", "member-1", "viewer-1"
NEIGHBOUR, STRANGER = "neighbour-admin-1", "stranger-1"
KEY = "org.shipment-summary"


def components(org: str = ORG) -> str:
    return f"/organizations/{org}/ui-components"


GRANTS = {
    "org:admin": ("organizations:read", "agents:manage", "ui_components:manage"),
    "org:member": ("organizations:read", "agents:manage"),
    "org:viewer": ("organizations:read",),
}
ROLES = {
    ADMIN: ("org:admin", ORG),
    MEMBER: ("org:member", ORG),
    VIEWER: ("org:viewer", ORG),
    NEIGHBOUR: ("org:admin", OTHER_ORG),
}

#: The design brief's shipment summary, smaller.
SHIPMENT: dict[str, Any] = {
    "format": "forge.ui_component/v1",
    "key": KEY,
    "display_name": "Shipment summary",
    "description": "Show an order's shipping status and its line items.",
    "input_schema": {
        "type": "object",
        "properties": {
            "order_number": {"type": "string", "maxLength": 80},
            "status": {"type": "string", "enum": ["Packed", "Shipped", "Delivered"]},
            "line_items": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 120},
            },
        },
        "required": ["order_number", "status", "line_items"],
        "additionalProperties": False,
    },
    "template": {
        "_type": "Card",
        "title": {"$format": "Order {/order_number}"},
        "children": [
            {"_type": "Badge", "value": {"$bind": "/status"}},
            {
                "_type": "Col",
                "children": {
                    "$each": "/line_items",
                    "template": {"_type": "Text", "value": {"$bind": ""}},
                },
            },
            {
                "_type": "Button",
                "label": "Track",
                "_action": {
                    "type": "reply",
                    "text": {"$format": "Track {/order_number}"},
                },
            },
        ],
    },
    "examples": [{"order_number": "A-1", "status": "Shipped", "line_items": ["Lamp"]}],
}
DRAWN = {
    "_type": "Card",
    "title": "Order A-1",
    "children": [
        {"_type": "Badge", "value": "Shipped"},
        {"_type": "Col", "children": [{"_type": "Text", "value": "Lamp"}]},
        {
            "_type": "Button",
            "label": "Track",
            "_action": {"type": "reply", "text": "Track A-1"},
        },
    ],
}


def shipment(**changes: Any) -> dict[str, Any]:
    return copy.deepcopy(SHIPMENT) | changes


class Grants(AsyncAdapter):
    def __init__(self) -> None:
        self.lines = [
            f"p, {role}, {permission.replace(':', ', ')}"
            for role, permissions in GRANTS.items()
            for permission in permissions
        ] + [f"g, {user}, {role}, org:{org}*" for user, (role, org) in ROLES.items()]

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


@dataclass
class Workspace:
    client: TestClient
    tokens: dict[str, str]
    #: The test's own database, to write agents into as their store keeps them.
    mongo: Any

    def call(
        self,
        method: str,
        path: str,
        user: str = ADMIN,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return self.client.request(
            method,
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self.tokens[user]}", **(headers or {})},
            json=body,
        )

    def add(
        self, document: dict[str, Any] | None = None, org: str = ORG
    ) -> dict[str, Any]:
        user = ADMIN if org == ORG else NEIGHBOUR
        made = self.call(
            "POST", components(org), user, {"document": document or shipment()}
        )
        assert made.status_code == 201, made.json()
        return dict(made.json())

    def publish(self, made: dict[str, Any], org: str = ORG) -> dict[str, Any]:
        user = ADMIN if org == ORG else NEIGHBOUR
        published = self.call(
            "POST",
            f"{components(org)}/{made['id']}/publish",
            user,
            {"revision": made["revision"]},
        )
        assert published.status_code == 200, published.json()
        return dict(published.json())

    def run(self, work: Any) -> Any:
        assert self.client.portal is not None
        return self.client.portal.call(work)

    @property
    def stores(self) -> tuple[ChatAgentStore, UiComponentStore]:
        state: Any = self.client.app.state  # type: ignore[attr-defined]
        return state.chat_agents, state.ui_components

    def catalog(self, org: str = ORG, user: str = VIEWER) -> dict[str, Any]:
        got = self.call("GET", f"{components(org)}/catalog", user)
        assert got.status_code == 200, got.json()
        return dict(got.json())


def mongo_uri() -> str:
    uri = os.environ.get("FORGE_ADMIN_MONGO_URI")
    if not uri:
        try:
            configured = Settings(migrate_on_start=False)  # pyright: ignore[reportCallIssue]
        except ValueError:
            configured = None
        if configured is not None and configured.mongo_uri is not None:
            uri = configured.mongo_uri.get_secret_value()
    if not uri:
        pytest.skip("needs MongoDB: set FORGE_ADMIN_MONGO_URI (make env)")
    return uri


def sqlite_tables() -> MetaData:
    tables = MetaData()
    for name in ("organizations", "users"):
        table = Base.metadata.tables[name].to_metadata(tables)
        for column in table.columns:
            column.server_default = None
    return tables


@pytest.fixture
def workspace(settings: Settings) -> Iterator[Workspace]:
    uri = mongo_uri()
    database = f"forge_admin_test_{uuid4().hex[:8]}"
    configured = settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 40), "local_user_id": None}
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    enforcer = new_enforcer(Grants())

    async def session() -> AsyncIterator[AsyncSession]:
        async with sessions() as made:
            yield made

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_enforcer] = lambda: enforcer

    async def create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(sqlite_tables().create_all)
        async with sessions() as made:
            made.add_all(
                [
                    Organization(id=ORG, name="Acme", description=""),
                    Organization(id=OTHER_ORG, name="Globex", description=""),
                ]
            )
            await made.commit()

    tokens = {
        user: mint_subject_token(configured, user, lifetime=timedelta(minutes=5))
        for user in (ADMIN, MEMBER, VIEWER, NEIGHBOUR, STRANGER)
    }
    mongo: MongoClient = MongoClient(uri)
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(create)
        # Set after the app started, so it builds no runtime around them.
        shared: AsyncMongoClient = AsyncMongoClient(uri, tz_aware=True)
        app.state.ui_components = UiComponentStore(shared, database)
        app.state.chat_agents = ChatAgentStore(shared, database)
        yield Workspace(client, tokens, mongo[database])
        client.portal.call(engine.dispose)
    mongo.drop_database(database)
    mongo.close()


# ------------------------------------------------------------ writing and publishing


def test_an_admin_writes_previews_and_publishes_one_and_agents_are_offered_it(
    workspace: Workspace,
) -> None:
    made = workspace.add()
    assert made["key"] == KEY and made["status"] == "draft" and made["revision"] == 1
    assert made["draft_version"] == 1 and made["published_version"] is None
    assert made["document"]["key"] == KEY and "version" not in made["document"]
    # A draft isn't offered to agents.
    assert workspace.catalog()["team"] == []

    preview = workspace.call(
        "POST",
        f"{components()}/preview",
        body={"definition": made["document"], "inputs": SHIPMENT["examples"][0]},
    )
    assert preview.status_code == 200, preview.json()
    assert preview.json()["tree"] == DRAWN and preview.json()["diagnostics"] == []
    assert preview.json()["capabilities"] == {
        "has_choices": False,
        "has_actions": True,
        "has_forms": False,
    }

    published = workspace.publish(made)
    assert published["status"] == "published" and published["published_version"] == 1
    assert published["used_by"] == []

    catalog = workspace.catalog()
    [team] = catalog["team"]
    assert team["key"] == KEY and team["version"] == 1 and team["id"] == made["id"]
    assert team["input_schema"] == SHIPMENT["input_schema"]
    assert team["base_components"] == ["Badge", "Button", "Card", "Col", "Text"]
    # Inputs and when to use it, never the layout.
    assert "template" not in team and "examples" not in team
    assert [group["id"] for group in catalog["base"]][:2] == ["layout", "text"]
    assert len(catalog["base_digest"]) == 64

    # The catalog's ETag is its digest: unchanged, it isn't sent again.
    etag = f'"{catalog["digest"]}"'
    again = workspace.call(
        "GET", f"{components()}/catalog", VIEWER, headers={"If-None-Match": etag}
    )
    assert again.status_code == 304 and again.headers["ETag"] == etag


def test_who_may_read_and_who_may_change_components(workspace: Workspace) -> None:
    made = workspace.add()
    one = f"{components()}/{made['id']}"
    preview = {"definition": SHIPMENT}
    # The catalog: everyone in the organization.
    for user in (ADMIN, MEMBER, VIEWER):
        assert workspace.call("GET", f"{components()}/catalog", user).status_code == 200
    # Everything else: its admins, not members (who manage agents) or viewers.
    for user in (MEMBER, VIEWER):
        assert workspace.call("GET", components(), user).status_code == 403
        assert workspace.call("GET", one, user).status_code == 403
        assert (
            workspace.call(
                "POST", components(), user, {"document": shipment()}
            ).status_code
            == 403
        )
        assert (
            workspace.call("POST", f"{components()}/preview", user, preview).status_code
            == 403
        )
        assert (
            workspace.call("POST", f"{one}/publish", user, {"revision": 1}).status_code
            == 403
        )
        assert workspace.call("PATCH", one, user, {"disabled": True}).status_code == 403
        assert workspace.call("DELETE", one, user).status_code == 403
    # Nobody outside it, another organization's admin included.
    for user in (NEIGHBOUR, STRANGER):
        assert workspace.call("GET", f"{components()}/catalog", user).status_code == 403
        assert workspace.call("GET", one, user).status_code == 403
        assert (
            workspace.call("POST", f"{one}/publish", user, {"revision": 1}).status_code
            == 403
        )
    # It's untouched.
    assert workspace.call("GET", one).json()["status"] == "draft"


def test_each_organization_has_its_own_components_under_the_same_key(
    workspace: Workspace,
) -> None:
    ours = workspace.publish(workspace.add())
    theirs = workspace.publish(
        workspace.add(shipment(description="Globex's own shipments."), OTHER_ORG),
        OTHER_ORG,
    )
    assert ours["id"] != theirs["id"] and ours["key"] == theirs["key"] == KEY
    [mine] = workspace.catalog()["team"]
    [other] = workspace.catalog(OTHER_ORG, NEIGHBOUR)["team"]
    assert mine["id"] == ours["id"] and mine["description"] == SHIPMENT["description"]
    assert (
        other["id"] == theirs["id"]
        and other["description"] == "Globex's own shipments."
    )
    assert (
        workspace.catalog()["digest"]
        != workspace.catalog(OTHER_ORG, NEIGHBOUR)["digest"]
    )
    # One organization's component isn't found through another's.
    assert workspace.call("GET", f"{components()}/{theirs['id']}").status_code == 404
    assert (
        workspace.call(
            "POST", f"{components()}/{theirs['id']}/publish", body={"revision": 1}
        ).status_code
        == 404
    )
    assert workspace.call("DELETE", f"{components()}/{theirs['id']}").status_code == 404
    assert [each["id"] for each in workspace.call("GET", components()).json()] == [
        ours["id"]
    ]

    # An organization deleted takes its components with it, versions and all.
    store: UiComponentStore = workspace.client.app.state.ui_components  # type: ignore[attr-defined]
    assert workspace.client.portal is not None
    workspace.client.portal.call(store.delete_organization, OTHER_ORG)
    assert (
        workspace.mongo["ui_components"].count_documents({"organization_id": OTHER_ORG})
        == 0
    )
    assert (
        workspace.mongo["ui_component_versions"].count_documents(
            {"organization_id": OTHER_ORG}
        )
        == 0
    )
    assert workspace.catalog()["team"] == [mine]


def test_drafts_keep_their_revision_and_key_and_only_whole_ones_publish(
    workspace: Workspace,
) -> None:
    made = workspace.add()
    one = f"{components()}/{made['id']}"

    edited = shipment(description="Show where an order is.")
    saved = workspace.call(
        "PUT", f"{one}/draft", body={"document": edited, "revision": 1}
    )
    assert saved.status_code == 200 and saved.json()["revision"] == 2
    stale = workspace.call(
        "PUT", f"{one}/draft", body={"document": edited, "revision": 1}
    )
    assert stale.status_code == 409
    renamed = workspace.call(
        "PUT",
        f"{one}/draft",
        body={"document": shipment(key="org.other"), "revision": 2},
    )
    assert renamed.status_code == 422
    assert renamed.json()["detail"]["diagnostics"][0]["code"] == "key_immutable"
    broken = {key: value for key, value in edited.items() if key != "template"}
    unsaved = workspace.call(
        "PUT", f"{one}/draft", body={"document": broken, "revision": 2}
    )
    assert unsaved.status_code == 422
    assert unsaved.json()["detail"]["diagnostics"] == [
        {"code": "definition_invalid", "path": "template", "message": "Field required"}
    ]

    # A draft that can't be drawn yet is kept, but not published.
    unbound = shipment()
    unbound["input_schema"]["required"] = ["order_number", "line_items"]
    kept = workspace.call(
        "PUT", f"{one}/draft", body={"document": unbound, "revision": 2}
    )
    assert kept.status_code == 200
    refused = workspace.call("POST", f"{one}/publish", body={"revision": 3})
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["msg"] == "It can't be published yet"
    assert detail["diagnostics"][0]["code"] == "binding_missing"
    assert detail["diagnostics"][0]["path"] == "template.children[0].value.$bind"
    assert workspace.call("GET", one).json()["published_version"] is None

    fixed = workspace.call(
        "PUT", f"{one}/draft", body={"document": edited, "revision": 3}
    )
    assert fixed.json()["revision"] == 4
    assert (
        workspace.call("POST", f"{one}/publish", body={"revision": 3}).status_code
        == 409
    )
    first = workspace.publish(fixed.json())
    assert first["published_version"] == 1 and first["has_draft"] is False

    # Published versions don't change: a new one is a draft of its own.
    closed = workspace.call(
        "PUT", f"{one}/draft", body={"document": edited, "revision": 5}
    )
    assert closed.status_code == 409 and closed.json()["detail"]["code"] == "NO_DRAFT"
    second_draft = workspace.call("POST", f"{one}/versions", body={})
    assert second_draft.status_code == 201 and second_draft.json()["draft_version"] == 2
    newer = shipment(description="Show where an order is, version two.")
    saved = workspace.call(
        "PUT",
        f"{one}/draft",
        body={"document": newer, "revision": second_draft.json()["revision"]},
    )
    # While the next version is a draft, agents keep the published one.
    [offered] = workspace.catalog()["team"]
    assert (
        offered["version"] == 1 and offered["description"] == "Show where an order is."
    )
    second = workspace.publish(saved.json())
    assert second["published_version"] == 2
    assert workspace.catalog()["team"][0]["description"] == newer["description"]

    versions = workspace.call("GET", f"{one}/versions").json()
    assert [each["version"] for each in versions] == [2, 1]
    old = workspace.call("GET", f"{one}/versions/1").json()
    assert old["document"]["version"] == 1
    assert old["document"]["description"] == "Show where an order is."
    assert workspace.call("GET", f"{one}/versions/9").status_code == 404

    # Restoring version 1 is a new version copied from it; discarding it goes back to 2.
    restored = workspace.call(
        "POST", f"{one}/versions", body={"from_version": 1}
    ).json()
    assert restored["draft_version"] == 3
    assert restored["document"]["description"] == "Show where an order is."
    assert workspace.call("POST", f"{one}/versions", body={}).status_code == 409
    discarded = workspace.call("DELETE", f"{one}/draft").json()
    assert discarded["has_draft"] is False and discarded["published_version"] == 2
    assert discarded["document"]["description"] == newer["description"]


def test_a_key_is_one_components_for_good(workspace: Workspace) -> None:
    made = workspace.add()
    taken = workspace.call("POST", components(), body={"document": shipment()})
    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "KEY_TAKEN"
    assert (
        workspace.call(
            "POST", components(), body={"document": shipment(key="Shipment")}
        ).status_code
        == 422
    )

    workspace.publish(made)
    assert workspace.call("DELETE", f"{components()}/{made['id']}").status_code == 204
    assert workspace.call("GET", f"{components()}/{made['id']}").status_code == 404
    assert workspace.catalog()["team"] == []
    # Archived, it keeps its key: conversations that drew it name it still.
    again = workspace.call("POST", components(), body={"document": shipment()})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "KEY_TAKEN"
    assert workspace.call("DELETE", f"{components()}/{made['id']}").status_code == 404


def agent(agent_id: str, name: str, picks: list[str], **record: Any) -> dict[str, Any]:
    """An agent's record, as its store keeps it, whose Rich answers pick these."""
    return {
        "_id": agent_id,
        "organization_id": ORG,
        "revision": 1,
        "document": {
            "name": name,
            "nodes": [
                {"id": "agent", "kind": "llm_agent", "config": {}},
                {"id": "rich", "kind": "rich_answers", "config": {"components": picks}},
            ],
        },
        "has_draft": False,
        "draft_version": None,
        "published_version": 1,
        "updated_at": datetime.now(UTC),
        "deleted_at": None,
        **record,
    }


def version(
    agent_id: str, number: int, picks: list[str], org: str = ORG
) -> dict[str, Any]:
    return {
        "_id": f"{agent_id}@{number}",
        "agent_id": agent_id,
        "organization_id": org,
        "version": number,
        "document": agent(agent_id, "", picks)["document"],
    }


def test_disabling_one_says_which_agents_pick_it_and_takes_it_out_of_the_catalog(
    workspace: Workspace,
) -> None:
    made = workspace.publish(workspace.add())
    one = f"{components()}/{made['id']}"
    workspace.mongo["chat_agents"].insert_many(
        [
            # What runs picks it, and no draft beside it.
            agent("ca_runs", "Runs it", ["Card", KEY]),
            # A draft picks it; what runs (version 1) doesn't.
            agent("ca_drafting", "Drafting", [KEY], has_draft=True, draft_version=2),
            # What runs (version 2) picks it, beside a draft that doesn't.
            agent(
                "ca_both",
                "Both",
                ["Card"],
                has_draft=True,
                draft_version=3,
                published_version=2,
            ),
            # Only an old version picked it: not what runs.
            agent(
                "ca_old",
                "Old",
                ["Card"],
                has_draft=True,
                draft_version=3,
                published_version=2,
            ),
            agent("ca_gone", "Deleted", [KEY], deleted_at=datetime.now(UTC)),
            agent("ca_other", "Not ours", [KEY]) | {"organization_id": OTHER_ORG},
        ]
    )
    workspace.mongo["chat_agent_versions"].insert_many(
        [
            version("ca_drafting", 1, ["Card"]),
            version("ca_both", 2, [KEY]),
            version("ca_old", 1, [KEY]),
            version("ca_old", 2, ["Card"]),
            version("ca_other", 1, [KEY], OTHER_ORG),
        ]
    )
    uses = [
        {"id": "ca_both", "name": "Both", "in_draft": False, "in_published": True},
        {
            "id": "ca_drafting",
            "name": "Drafting",
            "in_draft": True,
            "in_published": False,
        },
        {"id": "ca_runs", "name": "Runs it", "in_draft": False, "in_published": True},
    ]
    assert workspace.call("GET", one).json()["used_by"] == uses
    assert workspace.call("GET", components()).json()[0]["used_by"] == 3

    disabled = workspace.call("PATCH", one, body={"disabled": True})
    assert disabled.status_code == 200, disabled.json()
    assert disabled.json()["disabled"] is True and disabled.json()["used_by"] == uses
    # Not a change to the component: its revision and versions stay.
    assert disabled.json()["revision"] == made["revision"]
    assert workspace.catalog()["team"] == []
    enabled = workspace.call("PATCH", one, body={"disabled": False}).json()
    assert enabled["disabled"] is False
    assert [each["key"] for each in workspace.catalog()["team"]] == [KEY]
    assert (
        workspace.call(
            "PATCH", f"{components()}/uic_nosuch000", body={"disabled": True}
        ).status_code
        == 404
    )


def test_preview_draws_any_definition_and_says_what_publishing_would_refuse(
    workspace: Workspace,
) -> None:
    preview = f"{components()}/preview"
    # The first example, by default.
    drawn = workspace.call("POST", preview, body={"definition": SHIPMENT}).json()
    assert drawn["tree"] == DRAWN and drawn["diagnostics"] == []

    wrong = workspace.call(
        "POST",
        preview,
        body={
            "definition": SHIPMENT,
            "inputs": {"order_number": "A-1", "status": "Lost"},
        },
    ).json()
    assert wrong["tree"] is None
    assert [(each["code"], each["path"]) for each in wrong["diagnostics"]] == [
        ("component_input_invalid", "inputs"),
        ("component_input_invalid", "inputs.status"),
    ]

    unbound = shipment(examples=[])
    unbound["template"]["children"].append({"_type": "Gauge"})
    broken = workspace.call("POST", preview, body={"definition": unbound}).json()
    assert broken["tree"] is None and broken["capabilities"] is None
    assert broken["diagnostics"] == [
        {
            "code": "base_component_unsupported",
            "path": "template.children[3]._type",
            "message": "'Gauge' isn't a base component",
        }
    ]
    # Compiles, but publishing needs an example to draw.
    bare = workspace.call(
        "POST", preview, body={"definition": shipment(examples=[])}
    ).json()
    assert bare["tree"] is None and bare["capabilities"] is not None
    assert [each["code"] for each in bare["diagnostics"]] == ["definition_invalid"]


# ------------------------------------------------------------ agents using them

#: The builder's example chat agent, kept with the runtime package's tests.
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
NOTE = shipment(key="org.note", display_name="Note", description="A short note.")


class Idle(BaseLlm):
    """Agents here are built, never run."""

    model: str = "gpt-test"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        raise AssertionError("not run here")
        yield  # pragma: no cover


MODELS = ProviderModels(parse_model_provider_yaml(PROVIDERS), build=lambda _: Idle())


def agent_picking(*components: str, **fields: Any) -> dict[str, Any]:
    """The example agent, offline (no tools that reach the network), its Rich
    answers picking these."""
    doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    doc["nodes"] = [n for n in doc["nodes"] if n["id"] in ("agent", "billing")]
    doc["edges"] = [
        e
        for e in doc["edges"]
        if e["source"] in ("agent", "billing") and e["target"] in ("agent", "billing")
    ]
    doc["nodes"].append(
        {
            "id": "rich",
            "kind": "rich_answers",
            "name": "Rich answers",
            "config": {"components": list(components)},
            "outputs": [],
        }
    )
    doc["edges"].append(
        {"id": "r", "source": "agent", "source_output": "tools", "target": "rich"}
    )
    return doc | fields


def present_of(agent: Any) -> PresentTool:
    [tool] = [tool for tool in agent.tools if isinstance(tool, PresentTool)]
    return tool


def test_the_hosted_runtime_builds_with_what_agents_may_use_now(
    workspace: Workspace,
) -> None:
    published = workspace.publish(workspace.add())
    workspace.publish(workspace.add(NOTE))
    chat, ui = workspace.stores

    def made(doc: dict[str, Any]) -> dict[str, Any]:
        async def create() -> dict[str, Any]:
            return await chat.create_agent(
                ORG, doc, by=ADMIN, by_name=None, max_bytes=1_000_000
            )

        return cast(dict[str, Any], workspace.run(create))

    # What a hosted agent carries is never kept: it uses its organization's.
    shipping = made(
        agent_picking("Card", KEY, ui_components={KEY: {**NOTE, "key": KEY}})
    )
    assert "ui_components" not in shipping["document"]
    noting = made(agent_picking("org.note"))
    source = MongoAgentSource(chat, ui_stamps=UiComponentStamps(ui))

    def key(record: dict[str, Any]) -> str:
        async def get() -> str:
            return (
                await source.get(AgentRef.parse(f"{record['_id']}@draft"))
            ).cache_key

        return cast(str, workspace.run(get))

    first, other = key(shipping), key(noting)
    assert "#ui:" in first and "#ui:" in other
    # Base components alone: nothing to stamp.
    assert "#ui:" not in key(made(agent_picking("Card")))

    # The next version's draft changes nothing agents use; publishing it does,
    # for the agents that pick it alone.
    one = f"{components()}/{published['id']}"
    draft = workspace.call("POST", f"{one}/versions", body={}).json()
    saved = workspace.call(
        "PUT",
        f"{one}/draft",
        body={
            "document": shipment(description="Version two."),
            "revision": draft["revision"],
        },
    ).json()
    assert key(shipping) == first
    workspace.publish(saved)
    second = key(shipping)
    assert second != first and key(noting) == other

    executor = AgentExecutor(
        MongoAgentSource(chat, ui_stamps=UiComponentStamps(ui)),
        models=MODELS,
        sessions=InMemorySessionService(),
        services=RuntimeServices(ui_components=OrganizationUiComponents(ui)),
    )
    try:

        async def build() -> Any:
            runner, _ = await executor.runner(f"{shipping['_id']}@draft")
            return runner.agent

        tool = present_of(workspace.run(build))
        assert tool.components == ("Card",)
        assert (
            tool.team[KEY].version == 2 and tool.team[KEY].description == "Version two."
        )

        # Disabled, it's out of every build: the agent says so rather than drawing without it.
        workspace.call("PATCH", one, body={"disabled": True})
        assert key(shipping) not in (first, second)
        with pytest.raises(BuildError, match="was archived or disabled"):
            workspace.run(build)
        workspace.call("PATCH", one, body={"disabled": False})
        assert key(shipping) == second
    finally:
        workspace.run(executor.close)


def test_exports_and_workflow_runs_carry_the_components_their_agents_pick(
    workspace: Workspace,
) -> None:
    workspace.publish(workspace.add())
    gone = workspace.publish(workspace.add(NOTE))
    assert workspace.call("DELETE", f"{components()}/{gone['id']}").status_code == 204
    chat, ui = workspace.stores

    async def find(keys: Any) -> Any:
        return await ui.find_live(ORG, keys)

    async def made() -> tuple[dict[str, Any], dict[str, Any]]:
        both = await chat.create_agent(
            ORG,
            agent_picking(KEY, "org.note"),
            by=ADMIN,
            by_name=None,
            max_bytes=1_000_000,
        )
        one = await chat.create_agent(
            ORG, agent_picking("Card", KEY), by=ADMIN, by_name=None, max_bytes=1_000_000
        )
        published = await chat.publish(
            ORG, one["_id"], one["revision"], by=ADMIN, by_name=None
        )
        assert published is not None
        return both, published

    both, published = workspace.run(made)

    async def bundled() -> tuple[Any, Any]:
        return (
            await bundle(chat, ORG, both["document"], ui_components=find),
            await bundle(chat, ORG, both["document"]),
        )

    (document, notes), (_, unbundled) = workspace.run(bundled)
    assert list(document["ui_components"]) == [KEY]
    carried = document["ui_components"][KEY]
    assert carried["version"] == 1 and carried["template"] == SHIPMENT["template"]
    assert notes == [
        "it picks a team UI component that was archived or disabled, or isn't published"
    ]
    assert unbundled == [
        "its Rich answers pick the organization's team UI components, which aren't bundled"
    ]

    # A workflow run's agent carries them, and builds with no store to ask.
    resources = RunResources(
        ORG,
        chat_agents=chat,
        sessions=cast(Any, None),
        user=None,
        workflows=None,
        ui_components=ui,
    )

    async def run_agent() -> Any:
        document = await resources.chat_agent(published["_id"], None)
        app = await build_app(parse_document(document), models=MODELS)
        return document, app.root_agent

    document, root = workspace.run(run_agent)
    assert document["ui_components"][KEY]["version"] == 1
    assert present_of(root).team[KEY].version == 1


def test_the_instruction_writer_says_what_each_team_component_is_for(
    workspace: Workspace,
) -> None:
    workspace.publish(workspace.add())
    _, ui = workspace.stores
    brief = Brief.model_validate(
        {
            "where": "chat",
            "role": "chat_agent",
            "name": "Order helper",
            "tools": [
                {
                    "kind": "rich_answers",
                    "name": "Rich answers",
                    "config": {"components": ["Card", KEY, "org.gone"]},
                }
            ],
        }
    )

    async def written(store: Any) -> str:
        return render(
            brief, await resolve(brief, ORG, cast(Any, None), ui_components=store)
        )

    text = workspace.run(lambda: written(ui))
    assert "of the components its Rich answers node picks (Card)" in text
    assert f'`{KEY}` ("Shipment summary"): Show an order\'s shipping status' in text
    assert (
        "`org.gone`: not one of the organization's team components it can use" in text
    )
    # Without the store, none is said to be gone.
    unread = workspace.run(lambda: written(None))
    assert "`org.gone`: one of the organization's team components." in unread
