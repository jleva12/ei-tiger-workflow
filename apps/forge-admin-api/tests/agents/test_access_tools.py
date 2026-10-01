"""The assistant's access tools, against a stand-in for the admin API.

The stand-in records each call and answers as the routes would, so these check
what the tools send (as whom, where, with what) and what the model gets back.
Every call is also matched against the real routes (``me``, ``members``,
``roles``, ``authz``, ``users``) and its path and query checked with their own
parameter models, so a tool can't drift from the route it wraps.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.dependencies.utils import request_params_to_args
from fastapi.routing import APIRoute
from google.adk.agents import LlmAgent
from google.adk.events.event_actions import EventActions
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import SecretStr
from scripted_llm import ScriptedLlm
from starlette.routing import Match

from forge_admin.agents import access_tools
from forge_admin.agents.access_tools import AccessToolset
from forge_admin.agents.person_api import PersonApi
from forge_admin.api.routes import authz, me, members, roles, users
from forge_admin.auth.tokens import verify_token
from forge_admin.config import Settings

ORG = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e01"
SCOPE = f"org:{ORG}"
ADA = "5d1c9a52-0000-4d7a-8f30-6a1c2b3d4e10"
GRACE = "5d1c9a52-0000-4d7a-8f30-6a1c2b3d4e11"
STAMP = "2026-09-01T12:00:00Z"
AUDIT = {
    "created_at": STAMP,
    "created_by": GRACE,
    "updated_at": STAMP,
    "updated_by": GRACE,
}

Handler = Callable[[httpx.Request], httpx.Response]


def _permission(pid: int, resource: str, action: str) -> dict[str, Any]:
    return {
        "id": pid,
        "key": f"{resource}:{action}",
        "resource": resource,
        "action": action,
        "description": f"May {action} {resource}",
        **AUDIT,
    }


USERS = [
    {
        "id": ADA,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "msid": "alovelace",
        **AUDIT,
    },
    {
        "id": GRACE,
        "first_name": "Grace",
        "last_name": "Hopper",
        "email": "grace@example.com",
        "msid": "ghopper",
        **AUDIT,
    },
]
ROLES = [
    {
        "key": "site:auditor",
        "level": "site",
        "name": "Auditor",
        "description": "Reads every organization",
        "permissions": [_permission(1, "organizations", "read")],
        "member_count": 0,
        **AUDIT,
    },
    {
        "key": "site:admin",
        "level": "site",
        "name": "Administrator",
        "description": "Everything",
        "permissions": [_permission(2, "*", "*")],
        "member_count": 1,
        **AUDIT,
    },
    {
        "key": "org:admin",
        "level": "org",
        "name": "Admin",
        "description": "Runs the organization",
        "permissions": [
            _permission(3, "members", "read"),
            _permission(4, "members", "update"),
            _permission(5, "workflows", "*"),
        ],
        "member_count": 2,
        **AUDIT,
    },
    {
        "key": "org:member",
        "level": "org",
        "name": "Member",
        "description": "Works in the organization",
        "permissions": [
            _permission(1, "organizations", "read"),
            _permission(6, "workflows", "read"),
        ],
        "member_count": 5,
        **AUDIT,
    },
]
ACCESS = {
    "subject": "ada",
    "scope": SCOPE,
    "domain": SCOPE,
    "roles": ["site:auditor", "org:member"],
    "policies": [
        ["site:auditor", "organizations", "read"],
        ["org:member", "workflows", "read"],
        ["org:member", "organizations", "read"],
    ],
}
MEMBERS = [
    {"subject_id": ADA, "role": "site:auditor", "scope": "site", **AUDIT},
    {"subject_id": ADA, "role": "org:member", "scope": SCOPE, **AUDIT},
    {"subject_id": GRACE, "role": "org:admin", "scope": SCOPE, **AUDIT},
    {"subject_id": "svc-ingest", "role": "org:member", "scope": SCOPE, **AUDIT},
]


@pytest.fixture
def signing(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 32), "api_key": SecretStr("k" * 32)}
    )


# The routes the tools wrap, below the API prefix.
ROUTES = [
    route
    for router in (me.router, members.router, roles.router, authz.router, users.router)
    for route in router.routes
    if isinstance(route, APIRoute)
]


def endpoint(request: httpx.Request, prefix: str = "/api/v1") -> str:
    """
    The route a call lands on, by its function's name, after checking its path
    and query parameters with the route's own.
    """
    path = request.url.path.removeprefix(prefix)
    scope = {"type": "http", "path": path, "method": request.method}
    for route in ROUTES:
        match, child = route.matches(scope)
        if match is not Match.FULL:
            continue
        _, path_errors = request_params_to_args(
            route.dependant.path_params, child["path_params"]
        )
        _, query_errors = request_params_to_args(
            route.dependant.query_params, dict(request.url.params)
        )
        assert (path_errors, query_errors) == ([], []), request.url
        return route.endpoint.__name__
    raise AssertionError(f"No route takes {request.method} {request.url.path}")


def stand_in(request: httpx.Request) -> httpx.Response:
    """Answers as the routes would."""
    path = request.url.path.removeprefix("/api/v1")
    if request.method == "DELETE":
        return httpx.Response(204)
    if request.method == "PUT":
        _, scope, _, subject, _, role = path.strip("/").split("/")
        body = {"subject_id": subject, "role": role, "scope": scope, **AUDIT}
        return httpx.Response(200, json=body)
    answers: dict[str, Any] = {
        "/me": {"subject": "ada", "user": USERS[0]},
        "/me/access": ACCESS,
        "/me/memberships": [{"scope": SCOPE, "role": "org:member"}],
        "/me/organizations": [
            {"id": ORG, "name": "Acme", "description": "", "roles": ["org:member"]}
        ],
        "/roles": ROLES,
        "/roles/org:admin": ROLES[2],
        "/users": USERS,
        f"/scopes/{SCOPE}/members": MEMBERS,
        "/scopes/site/members": MEMBERS,
        f"/authz/subjects/{GRACE}/access": {**ACCESS, "subject": GRACE},
    }
    if path not in answers:
        return httpx.Response(404, json={"detail": "Not Found"})
    return httpx.Response(200, json=answers[path])


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


def answer(body: Any, status: int = 200) -> Handler:
    return lambda request: httpx.Response(status, json=body)


def context(user_id: str | None = "ada") -> Any:
    return MagicMock(user_id=user_id, actions=EventActions())


def call(toolset: AccessToolset, name: str, user: str | None = "ada", **args: Any):
    async def main() -> Any:
        tools = {tool.name: tool for tool in await toolset.get_tools()}
        return await tools[name].run_async(args=args, tool_context=context(user))

    return asyncio.run(main())


def tools(signing: Settings, handler: Handler = stand_in) -> tuple[AccessToolset, Api]:
    api = Api(signing, handler)
    return AccessToolset(api.person_api, confirm_changes=False), api


def test_the_tools_are_those_named_and_changes_ask_first(signing: Settings) -> None:
    api = Api(signing, stand_in)

    async def main() -> dict[str, Any]:
        toolset = AccessToolset(api.person_api)
        return {tool.name: tool for tool in await toolset.get_tools()}

    found = asyncio.run(main())
    assert set(found) == access_tools.TOOL_NAMES == AccessToolset.tool_names()
    asks = {name for name, tool in found.items() if tool._require_confirmation}
    assert asks == set(access_tools.CHANGE_TOOLS)


def test_every_call_is_the_conversations_person_with_the_api_key(
    signing: Settings,
) -> None:
    toolset, api = tools(signing)
    assert call(toolset, "get_my_profile", user="grace")["status"] == "success"

    sent = api.calls[0]
    token = sent.headers["authorization"].removeprefix("Bearer ")
    assert verify_token(signing, token) == "grace"
    assert sent.headers["x-api-key"] == "k" * 32

    # Without a person there's nobody to act as, and nothing is called.
    refused = call(toolset, "get_my_profile", user=None)
    assert refused["status"] == "failed"
    assert len(api.calls) == 1


@pytest.mark.parametrize(
    ("tool", "args", "method", "path", "params", "route"),
    [
        ("get_my_profile", {}, "GET", "/me", {}, "get_me"),
        (
            "get_my_access",
            {"scope": SCOPE},
            "GET",
            "/me/access",
            {"scope": SCOPE},
            "get_my_access",
        ),
        (
            "get_my_access",
            {"scope": "site", "permission": "workflows:run"},
            "GET",
            "/me/access",
            {"scope": "site"},
            "get_my_access",
        ),
        ("list_my_memberships", {}, "GET", "/me/memberships", {}, "get_my_memberships"),
        (
            "list_my_organizations",
            {},
            "GET",
            "/me/organizations",
            {},
            "get_my_organizations",
        ),
        ("list_available_roles", {}, "GET", "/roles", {}, "list_roles"),
        (
            "get_available_role",
            {"role": "org:admin"},
            "GET",
            "/roles/org:admin",
            {},
            "get_role",
        ),
        ("find_people", {"search": "ada"}, "GET", "/users", {}, "list_users"),
        (
            "list_scope_members",
            {"scope": SCOPE},
            "GET",
            f"/scopes/{SCOPE}/members",
            {},
            "list_members",
        ),
        (
            "list_scope_members",
            {"scope": "site", "include_above": True, "include_below": True},
            "GET",
            "/scopes/site/members",
            {"above": "true", "below": "true"},
            "list_members",
        ),
        (
            "get_member_access",
            {"member_id": GRACE, "scope": f"org:{ORG}"},
            "GET",
            f"/authz/subjects/{GRACE}/access",
            {"scope": f"org:{ORG}"},
            "get_subject_access",
        ),
        (
            "assign_member_role",
            {"scope": SCOPE, "member_id": GRACE, "role": "org:admin"},
            "PUT",
            f"/scopes/{SCOPE}/members/{GRACE}/roles/org:admin",
            {},
            "assign_role",
        ),
        (
            "assign_member_role",
            {"scope": "site", "member_id": "ada@example.com", "role": "site:admin"},
            "PUT",
            "/scopes/site/members/ada@example.com/roles/site:admin",
            {},
            "assign_role",
        ),
        (
            "remove_member_role",
            {"scope": SCOPE, "member_id": ADA, "role": "org:member"},
            "DELETE",
            f"/scopes/{SCOPE}/members/{ADA}/roles/org:member",
            {},
            "revoke_role",
        ),
    ],
)
def test_each_tool_calls_its_route(
    signing: Settings,
    tool: str,
    args: dict[str, Any],
    method: str,
    path: str,
    params: dict[str, str],
    route: str,
) -> None:
    toolset, api = tools(signing)
    got = call(toolset, tool, **args)

    assert got["status"] == "success", got
    first = api.calls[0]
    assert (first.method, first.url.path, dict(first.url.params)) == (
        method,
        f"/api/v1{path}",
        params,
    )
    assert endpoint(first, signing.api_prefix) == route
    # None of these routes takes a body: a role is given or taken by its path.
    assert first.content == b""


def test_a_member_id_and_role_are_quoted_into_the_path(signing: Settings) -> None:
    toolset, api = tools(signing)
    call(
        toolset,
        "assign_member_role",
        scope=SCOPE,
        member_id="ada+ops@example.com",
        role="org:admin",
    )
    assert (
        api.calls[0].url.raw_path
        == (
            f"/api/v1/scopes/{SCOPE}/members/ada%2Bops%40example.com/roles/org%3Aadmin"
        ).encode()
    )


def test_my_profile_is_who_they_are_without_the_audit_columns(
    signing: Settings,
) -> None:
    toolset, _ = tools(signing)
    got = call(toolset, "get_my_profile")["payload"]
    assert got == {
        "subject": "ada",
        "user": {
            "id": ADA,
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "msid": "alovelace",
        },
    }


def test_access_lists_each_permission_with_the_roles_granting_it(
    signing: Settings,
) -> None:
    toolset, _ = tools(signing)
    got = call(toolset, "get_my_access", scope=SCOPE)["payload"]
    assert got == {
        "subject": "ada",
        "scope": SCOPE,
        "domain": SCOPE,
        "roles": ["site:auditor", "org:member"],
        "permissions": [
            {
                "permission": "organizations:read",
                "grantedBy": ["org:member", "site:auditor"],
            },
            {"permission": "workflows:read", "grantedBy": ["org:member"]},
        ],
    }


@pytest.mark.parametrize(
    ("policies", "permission", "allowed", "granted_by"),
    [
        ([["org:member", "workflows", "read"]], "workflows:run", False, []),
        ([["org:member", "workflows", "run"]], "workflows:run", True, ["org:member"]),
        # Either part of a grant may be "*", as Casbin matches them.
        ([["org:admin", "workflows", "*"]], "workflows:run", True, ["org:admin"]),
        ([["site:admin", "*", "*"]], "members:update", True, ["site:admin"]),
        ([["org:admin", "*", "read"]], "members:update", False, []),
    ],
)
def test_access_checks_one_permission_as_casbin_would(
    signing: Settings,
    policies: list[list[str]],
    permission: str,
    allowed: bool,
    granted_by: list[str],
) -> None:
    toolset, _ = tools(signing, answer({**ACCESS, "policies": policies}))
    got = call(toolset, "get_my_access", scope=SCOPE, permission=permission)["payload"]
    assert got["check"] == {
        "permission": permission,
        "allowed": allowed,
        "grantedBy": granted_by,
    }


def test_someone_elses_access_reads_the_same(signing: Settings) -> None:
    toolset, _ = tools(signing)
    got = call(
        toolset,
        "get_member_access",
        member_id=GRACE,
        scope=SCOPE,
        permission="organizations:read",
    )["payload"]
    assert got["subject"] == GRACE
    assert got["check"] == {
        "permission": "organizations:read",
        "allowed": True,
        "grantedBy": ["org:member", "site:auditor"],
    }


def test_roles_are_listed_with_their_grants_and_filtered(signing: Settings) -> None:
    toolset, _ = tools(signing)

    every = call(toolset, "list_available_roles")["payload"]
    assert every[2] == {
        "key": "org:admin",
        "level": "org",
        "name": "Admin",
        "description": "Runs the organization",
        "permissions": ["members:read", "members:update", "workflows:*"],
        "memberCount": 2,
    }

    # Which roles would give someone workflows:run: "workflows:*" and "*:*" do.
    granting = call(toolset, "list_available_roles", grants="workflows:run")["payload"]
    assert [role["key"] for role in granting] == ["site:admin", "org:admin"]

    organization = call(toolset, "list_available_roles", level="org")["payload"]
    assert [role["key"] for role in organization] == ["org:admin", "org:member"]


def test_one_role_describes_each_grant(signing: Settings) -> None:
    toolset, _ = tools(signing)
    got = call(toolset, "get_available_role", role="org:admin")["payload"]
    assert got["permissions"][1] == {
        "key": "members:update",
        "description": "May update members",
    }


def test_people_are_found_by_name_email_or_msid(signing: Settings) -> None:
    toolset, _ = tools(signing)
    grace = {
        "id": GRACE,
        "name": "Grace Hopper",
        "email": "grace@example.com",
        "msid": "ghopper",
    }
    assert call(toolset, "find_people", search="grace hop")["payload"] == [grace]
    assert call(toolset, "find_people", search="GHOPPER")["payload"] == [grace]
    both = call(toolset, "find_people", search="example.com")["payload"]
    assert [person["id"] for person in both] == [ADA, GRACE]
    assert call(toolset, "find_people", search="example", limit=1)["payload"] == [
        both[0]
    ]


def test_members_are_grouped_by_person_and_named(signing: Settings) -> None:
    toolset, api = tools(signing)
    got = call(toolset, "list_scope_members", scope=SCOPE)["payload"]

    assert [request.url.path for request in api.calls] == [
        f"/api/v1/scopes/{SCOPE}/members",
        "/api/v1/users",
    ]
    assert got == [
        {
            "subjectId": ADA,
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "msid": "alovelace",
            "roles": [
                {
                    "role": "site:auditor",
                    "scope": "site",
                    "assignedBy": GRACE,
                    "assignedAt": STAMP,
                },
                {
                    "role": "org:member",
                    "scope": SCOPE,
                    "assignedBy": GRACE,
                    "assignedAt": STAMP,
                },
            ],
        },
        {
            "subjectId": GRACE,
            "name": "Grace Hopper",
            "email": "grace@example.com",
            "msid": "ghopper",
            "roles": [
                {
                    "role": "org:admin",
                    "scope": SCOPE,
                    "assignedBy": GRACE,
                    "assignedAt": STAMP,
                }
            ],
        },
        # A service isn't a user: it has no name.
        {
            "subjectId": "svc-ingest",
            "roles": [
                {
                    "role": "org:member",
                    "scope": SCOPE,
                    "assignedBy": GRACE,
                    "assignedAt": STAMP,
                }
            ],
        },
    ]


def test_members_are_listed_by_id_when_people_cant_be_read(
    signing: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users"):
            return httpx.Response(503, json={"detail": "Unavailable"})
        return stand_in(request)

    toolset, _ = tools(signing, handler)
    got = call(toolset, "list_scope_members", scope=SCOPE)
    assert got["status"] == "success"
    assert [member["subjectId"] for member in got["payload"]] == [
        ADA,
        GRACE,
        "svc-ingest",
    ]
    assert "name" not in got["payload"][0]


def test_an_empty_scope_reads_no_people(signing: Settings) -> None:
    toolset, api = tools(signing, answer([]))
    got = call(toolset, "list_scope_members", scope="site")
    assert got["payload"] == []
    assert len(api.calls) == 1


def test_giving_and_taking_a_role_say_what_changed(signing: Settings) -> None:
    toolset, _ = tools(signing)
    given = call(
        toolset,
        "assign_member_role",
        scope=SCOPE,
        member_id=ADA,
        role="org:admin",
    )["payload"]
    assert given == {
        "assigned": {
            "subjectId": ADA,
            "role": "org:admin",
            "scope": SCOPE,
            "assignedBy": GRACE,
            "assignedAt": STAMP,
        }
    }

    taken = call(
        toolset,
        "remove_member_role",
        scope=SCOPE,
        member_id=ADA,
        role="org:admin",
    )["payload"]
    assert taken == {"removed": {"subjectId": ADA, "role": "org:admin", "scope": SCOPE}}


@pytest.mark.parametrize("tool", ["assign_member_role", "remove_member_role"])
def test_a_role_of_another_level_isnt_sent(signing: Settings, tool: str) -> None:
    toolset, api = tools(signing)
    got = call(toolset, tool, scope=SCOPE, member_id=ADA, role="site:auditor")

    assert got["status"] == "failed"
    assert "site:auditor is given in site scopes" in got["reason"]
    assert "level='org'" in got["suggested_fixes"][0]
    assert api.calls == []


@pytest.mark.parametrize(
    ("status", "body", "fix"),
    [
        (403, {"detail": "Requires members:update"}, "list_available_roles"),
        (404, {"detail": "Organization not found"}, "Check the scope and IDs"),
        (
            422,
            {"detail": "site:admin is assigned at site scopes, not org"},
            "its own level",
        ),
        (
            422,
            {"detail": [{"loc": ["path", "subject_id"], "msg": "bad"}]},
            "Fix the arguments",
        ),
        (401, {"detail": "No signed-in user"}, "sign in to Forge again"),
        (503, {"detail": "MySQL is unavailable"}, "try later"),
    ],
)
def test_refusals_come_back_failed_with_the_reason_and_how_to_go_on(
    signing: Settings, status: int, body: dict[str, Any], fix: str
) -> None:
    toolset, _ = tools(signing, answer(body, status))
    got = call(
        toolset,
        "assign_member_role",
        scope=SCOPE,
        member_id=ADA,
        role="org:admin",
    )

    assert got["status"] == "failed"
    reason = body["detail"]
    assert got["reason"] == (reason if isinstance(reason, str) else "subject_id: bad")
    assert fix in got["suggested_fixes"][0]


def test_a_refused_read_explains_how_to_find_who_can_help(signing: Settings) -> None:
    toolset, _ = tools(signing, answer({"detail": "Requires members:read"}, 403))
    got = call(toolset, "list_scope_members", scope=SCOPE)
    assert got["status"] == "failed"
    assert got["reason"] == "Requires members:read"
    assert "grants=that permission" in got["suggested_fixes"][0]


@pytest.mark.parametrize(
    ("tool", "args", "argument"),
    [
        ("get_my_access", {"scope": f"{SCOPE}/../../users"}, "scope"),
        ("get_my_access", {"scope": f"org:{ORG.upper()}"}, "scope"),
        ("get_my_access", {"scope": "site\n"}, "scope"),
        ("get_my_access", {"scope": f"team:{ORG}"}, "scope"),
        ("get_my_access", {"scope": f"app:{ORG}"}, "scope"),
        ("get_my_access", {"scope": ORG}, "scope"),
        ("get_my_access", {"scope": "site", "permission": "workflows:*"}, "permission"),
        ("get_my_access", {"scope": "site", "permission": "workflows"}, "permission"),
        ("list_available_roles", {"grants": "*:*"}, "permission"),
        ("get_available_role", {"role": "org:admin/../../users"}, "role"),
        ("get_available_role", {"role": "Org:Admin"}, "role"),
        ("get_available_role", {"role": "admin"}, "role"),
        ("get_available_role", {"role": "team:lead"}, "role"),
        ("find_people", {"search": "   "}, "search"),
        ("list_scope_members", {"scope": "org:"}, "scope"),
        ("list_scope_members", {"scope": "site/members/x/roles"}, "scope"),
        ("get_member_access", {"member_id": "..", "scope": "site"}, "member_id"),
        ("get_member_access", {"member_id": f"{ADA}/x", "scope": "site"}, "member_id"),
        ("get_member_access", {"member_id": "a b", "scope": "site"}, "member_id"),
        ("get_member_access", {"member_id": f"{ADA}\n", "scope": "site"}, "member_id"),
        (
            "assign_member_role",
            {"scope": SCOPE, "member_id": "../me", "role": "org:admin"},
            "member_id",
        ),
        (
            "assign_member_role",
            {"scope": SCOPE, "member_id": ADA, "role": "org:admin/x"},
            "role",
        ),
        (
            "assign_member_role",
            {"scope": f"{SCOPE}?x=1", "member_id": ADA, "role": "org:admin"},
            "scope",
        ),
        (
            "remove_member_role",
            {"scope": "site", "member_id": ".", "role": "site:admin"},
            "member_id",
        ),
        (
            "remove_member_role",
            {"scope": "site", "member_id": ADA, "role": "site:admin#"},
            "role",
        ),
    ],
)
def test_no_argument_reaches_another_route(
    signing: Settings, tool: str, args: dict[str, Any], argument: str
) -> None:
    toolset, api = tools(signing)
    got = call(toolset, tool, **args)

    assert got["status"] == "failed"
    assert argument in got["reason"]
    assert api.calls == []


def test_a_missing_argument_fails_as_adk_says(signing: Settings) -> None:
    toolset, api = tools(signing)
    got = call(toolset, "assign_member_role", scope=SCOPE, member_id=ADA)
    assert got["status"] == "failed"
    assert "role" in got["reason"]
    assert api.calls == []


def test_without_a_jwt_secret_there_are_no_access_tools(settings: Settings) -> None:
    app = FastAPI()
    app.state.settings = settings.model_copy(update={"jwt_secret": None})
    assert access_tools.toolset(app) is None
    app.state.settings = settings.model_copy(update={"jwt_secret": SecretStr("s" * 32)})
    assert isinstance(access_tools.toolset(app), AccessToolset)


def test_a_change_waits_for_the_person_to_confirm_it(signing: Settings) -> None:
    api = Api(signing, stand_in)
    toolset = AccessToolset(api.person_api)
    llm = ScriptedLlm(
        turns=[
            [
                types.Part.from_function_call(
                    name="remove_member_role",
                    args={
                        "scope": SCOPE,
                        "member_id": ADA,
                        "role": "org:admin",
                    },
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
            role="user", parts=[types.Part.from_text(text="Remove Ada as admin")]
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
    # No role is taken away until the person says so.
    assert api.calls == []


def test_the_instruction_says_how_to_explain_a_refusal() -> None:
    for tool in ("get_my_access", "list_available_roles", "list_scope_members"):
        assert tool in access_tools.INSTRUCTION
    assert "members:update" in access_tools.INSTRUCTION
    # The tools' descriptions are what the model reads: every one has them.
    for name in access_tools.TOOL_NAMES:
        assert (getattr(AccessToolset, name).__doc__ or "").strip(), name


def test_json_bodies_are_never_sent(signing: Settings) -> None:
    """Every change here is a path: nothing for a body model to check."""
    toolset, api = tools(signing)
    for tool in access_tools.CHANGE_TOOLS:
        call(toolset, tool, scope=SCOPE, member_id=ADA, role="org:member")
    assert [json.loads(request.content or b"null") for request in api.calls] == [
        None,
        None,
    ]
