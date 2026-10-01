"""The assistant's administration tools, against a stand-in for the admin API.

The stand-in records each call and answers as the routes would, so these check
what the tools send (as whom, where, with what), that every change's body is
one its route's own request model takes, and what the model gets back. None
of the tools deletes anything.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from google.adk.agents import LlmAgent
from google.adk.events.event_actions import EventActions
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel, SecretStr
from scripted_llm import ScriptedLlm

from forge_admin.agents import access_tools, admin_tools, event_tools, workflow_tools
from forge_admin.agents.admin_tools import AdministrationToolset
from forge_admin.agents.person_api import PersonApi
from forge_admin.api.routes.common import NodeCreate, NodeUpdate
from forge_admin.api.routes.permissions import PermissionCreate, PermissionUpdate
from forge_admin.api.routes.roles import RoleCreate, RoleUpdate
from forge_admin.api.routes.users import UserCreate, UserUpdate
from forge_admin.auth.tokens import verify_token
from forge_admin.config import Settings

ORG = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e01"
USER = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e05"
PREFIX = "/api/v1"
AUDIT = {
    "created_at": "2026-09-01T10:00:00Z",
    "created_by": "grace",
    "updated_at": "2026-09-02T10:00:00Z",
    "updated_by": "grace",
}
# A role as the routes answer it: what it grants, in full.
ROLE = {
    "key": "org:admin",
    "level": "org",
    "name": "Admin",
    "description": "Runs the organization",
    "member_count": 4,
    "permissions": [
        {"id": 3, "key": "organizations:read", "resource": "organizations", "action": "read",
         "description": "Read organizations", **AUDIT},
        {"id": 5, "key": "workflows:run", "resource": "workflows", "action": "run",
         "description": "Run workflows", **AUDIT},
    ],
    **AUDIT,
}  # fmt: skip
COLLECTIONS = {
    "organizations",
    "roles",
    "permissions",
    "users",
}

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def signing(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 32), "api_key": SecretStr("k" * 32)}
    )


class Api:
    """Answers each call with ``handler``, and records it."""

    def __init__(self, settings: Settings, handler: Handler) -> None:
        self.calls: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return handler(request)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(record), base_url="http://admin"
        )
        self.person_api = PersonApi(client, settings)

    def body(self, index: int = -1) -> Any:
        return json.loads(self.calls[index].content or b"null")


def answer(body: Any, status: int = 200) -> Handler:
    return lambda request: httpx.Response(status, json=body)


def routes(request: httpx.Request) -> httpx.Response:
    """A collection lists nothing; anything else is the role above."""
    if request.method == "GET" and request.url.path.split("/")[-1] in COLLECTIONS:
        return httpx.Response(200, json=[])
    return httpx.Response(200, json=ROLE)


def context(user_id: str | None = "ada") -> Any:
    return MagicMock(user_id=user_id, actions=EventActions())


def call(
    toolset: AdministrationToolset,
    tool_name: str,
    /,
    as_user: str | None = "ada",
    **args: Any,
) -> Any:
    """Run a tool as ``as_user``; positional, so a tool may take ``name``."""

    async def main() -> Any:
        tools = {tool.name: tool for tool in await toolset.get_tools()}
        return await tools[tool_name].run_async(
            args=args, tool_context=context(as_user)
        )

    return asyncio.run(main())


def tools(
    signing: Settings, handler: Handler = routes
) -> tuple[AdministrationToolset, Api]:
    api = Api(signing, handler)
    return AdministrationToolset(api.person_api, confirm_changes=False), api


# Every tool: its arguments, then the method, path (below the prefix) and body
# it sends, and for a change the route's request model that body must fit.
CASES: list[tuple[str, dict[str, Any], str, str, Any, type[BaseModel] | None]] = [
    ("list_organizations", {}, "GET", "/organizations", None, None),
    (
        "get_organization",
        {"organization_id": ORG},
        "GET",
        f"/organizations/{ORG}",
        None,
        None,
    ),
    (
        "create_organization",
        {"name": "Payments", "description": "Moves money"},
        "POST",
        "/organizations",
        {"name": "Payments", "description": "Moves money"},
        NodeCreate,
    ),
    (
        "update_organization",
        {"organization_id": ORG, "name": "Payments EU"},
        "PATCH",
        f"/organizations/{ORG}",
        {"name": "Payments EU"},
        NodeUpdate,
    ),
    ("list_role_definitions", {}, "GET", "/roles", None, None),
    ("get_role_definition", {"role_key": "org:admin"}, "GET", "/roles/org:admin", None, None),
    (
        "create_role_definition",
        {"key": "org:approver", "name": "Approver", "permission_ids": [3, 5.0]},
        "POST",
        "/roles",
        {"key": "org:approver", "name": "Approver", "permission_ids": [3, 5]},
        RoleCreate,
    ),
    (
        "update_role_definition",
        {"role_key": "org:admin", "name": "Organization admin"},
        "PATCH",
        "/roles/org:admin",
        {"name": "Organization admin"},
        RoleUpdate,
    ),
    ("list_permission_definitions", {}, "GET", "/permissions", None, None),
    ("get_permission_definition", {"permission_id": 7}, "GET", "/permissions/7", None, None),
    (
        "create_permission_definition",
        {"key": "reports:read", "description": "Read reports"},
        "POST",
        "/permissions",
        {"key": "reports:read", "description": "Read reports"},
        PermissionCreate,
    ),
    (
        "update_permission_definition",
        {"permission_id": 7, "key": "reports:view"},
        "PATCH",
        "/permissions/7",
        {"key": "reports:view"},
        PermissionUpdate,
    ),
    ("list_users", {"search": "ada"}, "GET", "/users", None, None),
    ("get_user", {"user_id": "ada@example.com"}, "GET", "/users/ada@example.com", None, None),
    (
        "create_user",
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "msid": "alovelace",
        },
        "POST",
        "/users",
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "msid": "alovelace",
        },
        UserCreate,
    ),
    (
        "update_user",
        {"user_id": USER, "email": "ada@example.org"},
        "PATCH",
        f"/users/{USER}",
        {"email": "ada@example.org"},
        UserUpdate,
    ),
]  # fmt: skip


def test_every_tool_is_covered_read_or_change_and_none_is_taken() -> None:
    assert {case[0] for case in CASES} == admin_tools.TOOL_NAMES
    assert AdministrationToolset.tool_names() == admin_tools.TOOL_NAMES
    reads, changes = set(admin_tools.READ_TOOLS), set(admin_tools.CHANGE_TOOLS)
    assert not reads & changes
    # Everything that sends a body changes something; every read only reads.
    assert changes == {case[0] for case in CASES if case[2] != "GET"}
    assert not admin_tools.TOOL_NAMES & (
        access_tools.TOOL_NAMES | event_tools.TOOL_NAMES | workflow_tools.TOOL_NAMES
    )
    assert not any(name.startswith(("delete", "remove")) for name in changes)


@pytest.mark.parametrize(
    ("name", "args", "method", "path", "body", "model"),
    CASES,
    ids=[case[0] for case in CASES],
)
def test_each_tool_sends_what_its_route_takes(
    signing: Settings,
    name: str,
    args: dict[str, Any],
    method: str,
    path: str,
    body: Any,
    model: type[BaseModel] | None,
) -> None:
    toolset, api = tools(signing)
    got = call(toolset, name, **args)

    assert got["status"] == "success", got
    sent = api.calls[-1]
    assert (sent.method, sent.url.path, dict(sent.url.params)) == (
        method,
        f"{PREFIX}{path}",
        {},
    )
    assert api.body() == body
    if model is not None:
        # The route's own model takes it, and ignores none of it.
        model.model_validate(body)
        assert set(body) <= set(model.model_fields)


def test_no_tool_ever_deletes(signing: Settings) -> None:
    toolset, api = tools(signing)
    for name, args, *_ in CASES:
        call(toolset, name, **args)
    assert len(api.calls) == len(CASES)
    assert "DELETE" not in {request.method for request in api.calls}


def test_changes_wait_for_confirmation_and_reads_dont(signing: Settings) -> None:
    api = Api(signing, routes)
    toolset = AdministrationToolset(api.person_api)

    async def main() -> dict[str, bool]:
        return {
            tool.name: await tool.check_require_confirmation({}, context())
            for tool in await toolset.get_tools()
        }

    asks = asyncio.run(main())
    assert {name for name, asked in asks.items() if asked} == set(
        admin_tools.CHANGE_TOOLS
    )


def test_every_call_is_the_conversations_person_with_the_api_key(
    signing: Settings,
) -> None:
    toolset, api = tools(signing)
    assert call(toolset, "list_organizations", as_user="grace")["status"] == "success"

    sent = api.calls[0]
    token = sent.headers["authorization"].removeprefix("Bearer ")
    assert verify_token(signing, token) == "grace"
    assert sent.headers["x-api-key"] == "k" * 32

    # Without a person there's nobody to act as, and nothing is called.
    refused = call(toolset, "list_organizations", as_user=None)
    assert refused["status"] == "failed"
    assert len(api.calls) == 1


def test_granting_and_revoking_keeps_what_the_role_has(signing: Settings) -> None:
    toolset, api = tools(signing)
    got = call(
        toolset,
        "update_role_definition",
        role_key="org:admin",
        grant_permission_ids=[7, 3.0],
        revoke_permission_ids=[5],
    )

    assert got["status"] == "success"
    assert [(r.method, r.url.path) for r in api.calls] == [
        ("GET", f"{PREFIX}/roles/org:admin"),
        ("PATCH", f"{PREFIX}/roles/org:admin"),
    ]
    # The route replaces what it grants: 3 stays, 5 goes, 7 comes.
    assert api.body() == {"permission_ids": [3, 7]}
    RoleUpdate.model_validate(api.body())


def test_a_permission_both_granted_and_revoked_is_refused(signing: Settings) -> None:
    toolset, api = tools(signing)
    got = call(
        toolset,
        "update_role_definition",
        role_key="org:admin",
        grant_permission_ids=[7],
        revoke_permission_ids=[7],
    )
    assert got["status"] == "failed"
    assert "[7]" in got["reason"]
    assert api.calls == []


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("update_organization", {"organization_id": ORG}),
        ("update_role_definition", {"role_key": "org:admin"}),
        (
            "update_role_definition",
            {"role_key": "org:admin", "grant_permission_ids": []},
        ),
        ("update_permission_definition", {"permission_id": 7}),
        ("update_user", {"user_id": USER}),
    ],
)
def test_an_update_with_nothing_to_change_sends_nothing(
    signing: Settings, name: str, args: dict[str, Any]
) -> None:
    toolset, api = tools(signing)
    got = call(toolset, name, **args)
    assert got["status"] == "failed"
    assert "Nothing to change" in got["reason"]
    assert api.calls == []


def test_roles_list_what_they_grant_by_key_and_one_role_by_id_too(
    signing: Settings,
) -> None:
    toolset, _ = tools(signing, answer([ROLE]))
    [listed] = call(toolset, "list_role_definitions")["payload"]
    assert listed == {
        "key": "org:admin",
        "level": "org",
        "name": "Admin",
        "description": "Runs the organization",
        "member_count": 4,
        "permissions": ["organizations:read", "workflows:run"],
    }

    toolset, _ = tools(signing, answer(ROLE))
    got = call(toolset, "get_role_definition", role_key="org:admin")["payload"]
    assert got["permissions"] == [
        {"id": 3, "key": "organizations:read", "description": "Read organizations"},
        {"id": 5, "key": "workflows:run", "description": "Run workflows"},
    ]
    assert got["updated_by"] == "grace"


def test_the_hierarchy_comes_without_its_casbin_domains(signing: Settings) -> None:
    organization = {
        "id": ORG,
        "name": "Payments",
        "description": "",
        "domain": f"org:{ORG}",
        **AUDIT,
    }
    toolset, _ = tools(signing, answer([organization]))
    assert call(toolset, "list_organizations")["payload"] == [
        {"id": ORG, "name": "Payments", "description": ""}
    ]


def test_users_are_found_by_search_and_limited(signing: Settings) -> None:
    people = [
        {"id": f"u{n}", "first_name": first, "last_name": last, "email": email,
         "msid": msid, **AUDIT}
        for n, (first, last, email, msid) in enumerate(
            [
                ("Ada", "Lovelace", "ada@example.com", "alovelace"),
                ("Grace", "Hopper", "grace@example.com", "ghopper"),
                ("Adam", "Smith", "adam@example.com", "asmith"),
            ]
        )
    ]  # fmt: skip
    toolset, api = tools(signing, answer(people))

    got = call(toolset, "list_users", search="ADA", limit=1)["payload"]
    assert got == {
        "items": [
            {
                "id": "u0",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "email": "ada@example.com",
                "msid": "alovelace",
            }
        ],
        "total": 2,
    }
    # A full name matches too; the search never reaches the route.
    assert call(toolset, "list_users", search="grace hopper")["payload"]["total"] == 1
    assert all(dict(request.url.params) == {} for request in api.calls)


def test_an_uppercase_id_is_sent_as_the_routes_write_it(signing: Settings) -> None:
    toolset, api = tools(signing)
    call(toolset, "get_organization", organization_id=ORG.upper())
    assert api.calls[0].url.path == f"{PREFIX}/organizations/{ORG}"


@pytest.mark.parametrize(
    ("status", "body", "fix"),
    [
        (403, {"detail": "Requires users:create"}, "site administrators"),
        (404, {"detail": "User not found"}, "Check the IDs"),
        (409, {"detail": "A user with this email exists"}, "is taken"),
        (
            422,
            {"detail": [{"loc": ["body", "email"], "msg": "not an email"}]},
            "Fix the arguments",
        ),
        (503, {"detail": "The database is unavailable"}, "try later"),
    ],
)
def test_refusals_come_back_failed_with_the_reason_and_how_to_go_on(
    signing: Settings, status: int, body: dict[str, Any], fix: str
) -> None:
    toolset, _ = tools(signing, answer(body, status))
    got = call(toolset, "update_user", user_id=USER, email="ada@example.org")

    assert got["status"] == "failed"
    reason = body["detail"]
    assert got["reason"] == (
        reason if isinstance(reason, str) else "email: not an email"
    )
    assert fix in got["suggested_fixes"][0]


@pytest.mark.parametrize(
    ("name", "args", "argument"),
    [
        ("get_organization", {"organization_id": f"{ORG}/roles"}, "organization_id"),
        ("get_organization", {"organization_id": "../users"}, "organization_id"),
        ("get_organization", {"organization_id": f"{ORG}\n"}, "organization_id"),
        ("update_organization", {"organization_id": f"{ORG}/..", "name": "X"}, "organization_id"),
        ("get_role_definition", {"role_key": "org:admin/../../users"}, "role_key"),
        ("get_role_definition", {"role_key": "admin"}, "role_key"),
        ("get_role_definition", {"role_key": "team:lead"}, "role_key"),
        ("get_role_definition", {"role_key": "org:admin\n"}, "role_key"),
        ("update_role_definition", {"role_key": "..", "name": "X"}, "role_key"),
        ("get_permission_definition", {"permission_id": "7/../../users"}, "permission_id"),
        ("get_permission_definition", {"permission_id": 1.5}, "permission_id"),
        ("get_permission_definition", {"permission_id": -1}, "permission_id"),
        ("get_permission_definition", {"permission_id": True}, "permission_id"),
        ("update_permission_definition", {"permission_id": 2**31, "key": "a:b"}, "permission_id"),
        ("get_user", {"user_id": ".."}, "user_id"),
        ("get_user", {"user_id": "."}, "user_id"),
        ("get_user", {"user_id": "ada%2F..%2Froles"}, "user_id"),
        ("update_user", {"user_id": "ada/../../roles", "email": "a@b.c"}, "user_id"),
        (
            "create_role_definition",
            {"key": "org:x", "name": "X", "permission_ids": ["all"]},
            "permission_ids",
        ),
        (
            "update_role_definition",
            {"role_key": "org:admin", "grant_permission_ids": [0]},
            "grant_permission_ids",
        ),
    ],
)  # fmt: skip
def test_no_argument_reaches_another_route(
    signing: Settings, name: str, args: dict[str, Any], argument: str
) -> None:
    toolset, api = tools(signing)
    got = call(toolset, name, **args)

    assert got["status"] == "failed"
    assert argument in got["reason"]
    assert api.calls == []


def test_without_a_jwt_secret_there_are_no_administration_tools(
    settings: Settings,
) -> None:
    app = FastAPI()
    app.state.settings = settings.model_copy(update={"jwt_secret": None})
    assert admin_tools.toolset(app) is None
    app.state.settings = settings.model_copy(update={"jwt_secret": SecretStr("s" * 32)})
    assert isinstance(admin_tools.toolset(app), AdministrationToolset)


def test_a_change_waits_for_the_person_to_confirm_it(signing: Settings) -> None:
    api = Api(signing, routes)
    toolset = AdministrationToolset(api.person_api)
    llm = ScriptedLlm(
        turns=[
            [
                types.Part.from_function_call(
                    name="update_role_definition",
                    args={"role_key": "org:admin", "revoke_permission_ids": [5]},
                )
            ],
            [types.Part.from_text(text="Waiting for you to confirm.")],
        ]
    )
    agent = LlmAgent(name="forge", model=llm, tools=[toolset])
    runner = InMemoryRunner(agent=agent, app_name="forge")

    async def main() -> list[Any]:
        session = await runner.session_service.create_session(
            app_name="forge", user_id="ada"
        )
        message = types.Content(
            role="user", parts=[types.Part.from_text(text="Leads can't submit")]
        )
        return [
            event
            async for event in runner.run_async(
                user_id="ada", session_id=session.id, new_message=message
            )
        ]

    events = asyncio.run(main())
    asked = [
        call.name
        for event in events
        for call in event.get_function_calls()
        if call.name == "adk_request_confirmation"
    ]
    assert asked == ["adk_request_confirmation"]
    # Not even the role is read until the person says so.
    assert api.calls == []
