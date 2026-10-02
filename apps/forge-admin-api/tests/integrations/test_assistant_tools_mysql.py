"""The assistant's toolsets against the real routes, Casbin and MySQL.

Each toolset's unit tests check what its tools send; here a sample of each
toolset's tools runs on the real application, as the assistant runs them:
in the process, with a token minted for the person. The app acts as nobody
by itself, so every call is whoever its token names, and the routes decide
what they may read and change.
"""

from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from google.adk.events.event_actions import EventActions
from pydantic import SecretStr

from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.assistant import access_tools, admin_tools
from forge_admin.config import Settings

pytestmark = pytest.mark.mysql

API = "/api/v1"
MODULES = (access_tools, admin_tools)


@pytest.fixture
def forge(
    site_admin: str,
    client_as: Callable[[str], TestClient],
    mysql_settings: Settings,
) -> Iterator[dict[str, Any]]:
    """An organization with an admin, and the app."""
    admin = client_as(site_admin)
    tag = uuid4().hex[:8]
    organization = admin.post(
        f"{API}/organizations", json={"name": f"Org {tag}"}
    ).json()["id"]
    org_admin = f"admin-{tag}"
    assigned = admin.put(
        f"{API}/scopes/org:{organization}/members/{org_admin}/roles/org:admin"
    )
    assert assigned.status_code == 200

    settings = mysql_settings.model_copy(
        update={
            "local_user_id": f"nobody-{tag}",
            "jwt_secret": SecretStr("t" * 32),
            "embedding_redis_url": None,
        }
    )
    client = TestClient(ApiServer(settings, routers=ROUTERS).create_app())
    client.__enter__()
    toolsets = {
        module.__name__.rsplit(".", 1)[1]: module.toolset(
            client.app, confirm_changes=False
        )  # type: ignore[arg-type]
        for module in MODULES
    }

    def use(user: str, module: str, name: str, /, **args: Any) -> Any:
        async def run() -> Any:
            tools = {tool.name: tool for tool in await toolsets[module].get_tools()}
            context = MagicMock(user_id=user, actions=EventActions())
            return await tools[name].run_async(args=args, tool_context=context)

        assert client.portal is not None
        return client.portal.call(run)

    yield {
        "use": use,
        "organization": organization,
        "org_admin": org_admin,
        "admin": site_admin,
    }
    client.__exit__(None, None, None)
    admin.delete(f"{API}/organizations/{organization}")


def ok(answer: Any) -> Any:
    assert answer["status"] == "success", answer
    return answer["payload"]


def test_each_toolset_reads_as_the_person_through_the_real_routes(forge) -> None:
    use, organization, lead = forge["use"], forge["organization"], forge["org_admin"]
    scope = f"org:{organization}"

    access = ok(use(lead, "access_tools", "get_my_access", scope=scope))
    assert "org:admin" in str(access)
    assert organization in str(ok(use(lead, "access_tools", "list_my_organizations")))
    members = ok(use(lead, "access_tools", "list_scope_members", scope=scope))
    assert lead in str(members)

    assert (
        ok(use(lead, "admin_tools", "get_organization", organization_id=organization))[
            "id"
        ]
        == organization
    )
    assert ok(use(lead, "admin_tools", "list_role_definitions"))


def test_changes_go_through_the_real_routes_and_their_checks(forge) -> None:
    use, organization, admin = forge["use"], forge["organization"], forge["admin"]

    renamed = ok(
        use(
            admin,
            "admin_tools",
            "update_organization",
            organization_id=organization,
            description="Buys what we need",
        )
    )
    assert renamed["description"] == "Buys what we need"


def test_someone_outside_the_organization_reads_and_changes_nothing(forge) -> None:
    use, organization = forge["use"], forge["organization"]
    outsider = f"outsider-{uuid4().hex[:8]}"
    for module, name, args in (
        ("admin_tools", "get_organization", {"organization_id": organization}),
        (
            "admin_tools",
            "update_organization",
            {"organization_id": organization, "name": "Taken over"},
        ),
    ):
        refused = use(outsider, module, name, **args)
        assert refused["status"] == "failed", (name, refused)
        assert refused["reason"].startswith("Requires "), (name, refused)
