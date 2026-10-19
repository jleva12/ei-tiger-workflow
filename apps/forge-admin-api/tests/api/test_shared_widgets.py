"""Widgets shared with an organization: what may be shared, and, against
MySQL, who shares, sees and takes them away."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from forge_admin.api.routes import shared_widgets
from forge_admin.api.routes.shared_widgets import SharedWidgetIn
from forge_admin.api.server import ApiServer
from forge_admin.auth.authorization import API_KEY_SUBJECT_PREFIX
from forge_admin.config import Settings

API = "/api/v1"
SPEND = {
    "type": "custom",
    "title": "Agent spend leaders",
    "settings": {"measure": "cost", "split": "subject", "show": "ranking"},
}


def test_a_custom_widget_may_be_shared() -> None:
    shared = SharedWidgetIn.model_validate({**SPEND, "title": "  Spend  "})
    assert shared.title == "Spend"
    assert (
        SharedWidgetIn.model_validate({"type": "custom", "title": "T"}).settings == {}
    )


@pytest.mark.parametrize(
    "widget",
    [
        pytest.param({**SPEND, "type": "tokens"}, id="a dashboard's own widget"),
        pytest.param({**SPEND, "title": "   "}, id="no title"),
        pytest.param({**SPEND, "title": "t" * 121}, id="long title"),
        pytest.param({**SPEND, "settings": {"split": {"by": 1}}}, id="nested"),
        pytest.param(
            {**SPEND, "settings": {f"s{i}": i for i in range(33)}},
            id="too many settings",
        ),
        pytest.param({**SPEND, "x": 0}, id="a place"),
    ],
)
def test_only_widgets_people_define_are_shared(widget: dict) -> None:
    with pytest.raises(ValidationError):
        SharedWidgetIn.model_validate(widget)


def test_an_api_key_shares_nothing(settings: Settings) -> None:
    key = f"{API_KEY_SUBJECT_PREFIX}{uuid4()}"
    local = settings.model_copy(update={"local_user_id": key})
    path = (
        f"{API}/organizations/{uuid4()}/dashboards/organization-overview/shared-widgets"
    )
    app = ApiServer(local, routers=[shared_widgets.router]).create_app()
    with TestClient(app) as client:
        assert client.get(path).status_code == 403
        assert client.post(path, json=SPEND).status_code == 403
        assert client.delete(f"{path}/{uuid4()}").status_code == 403


@dataclass
class Org:
    admin: TestClient
    id: str
    other: str
    tag: str

    def path(self, organization: str | None = None) -> str:
        return (
            f"{API}/organizations/{organization or self.id}"
            "/dashboards/organization-overview/shared-widgets"
        )

    def assign(self, user: str, role: str, organization: str | None = None) -> None:
        response = self.admin.put(
            f"{API}/scopes/org:{organization or self.id}/members/{user}/roles/{role}"
        )
        assert response.status_code == 200


@pytest.fixture
def org(site_admin: str, client_as: Callable[[str], TestClient]) -> Iterator[Org]:
    admin = client_as(site_admin)
    tag = uuid4().hex[:8]
    acme, globex = (
        admin.post(f"{API}/organizations", json={"name": f"{name} {tag}"}).json()["id"]
        for name in ("Acme", "Globex")
    )
    yield Org(admin, acme, globex, tag)
    # Its shared widgets and roles go with each organization.
    for organization in (acme, globex):
        admin.delete(f"{API}/organizations/{organization}")


@pytest.mark.mysql
def test_members_share_see_and_take_away_widgets(
    org: Org, client_as: Callable[[str], TestClient]
) -> None:
    mo, may, ada = (f"{name}-{org.tag}" for name in ("mo", "may", "ada"))
    org.assign(mo, "org:member")
    org.assign(may, "org:viewer")
    org.assign(ada, "org:admin")
    as_mo, as_may, as_ada = client_as(mo), client_as(may), client_as(ada)

    assert as_mo.get(org.path()).json() == []
    shared = as_mo.post(org.path(), json=SPEND)
    assert shared.status_code == 201
    widget = shared.json()
    assert widget["title"] == "Agent spend leaders"
    assert widget["settings"] == SPEND["settings"]
    assert widget["shared_by"] == mo
    assert widget["shared_at"].endswith("Z") or "+00:00" in widget["shared_at"]
    assert widget["can_remove"] is True

    # Everyone who sees the organization sees it; only some may take it away.
    seen = {
        who: client.get(org.path()).json()
        for who, client in (("mo", as_mo), ("may", as_may), ("ada", as_ada))
    }
    assert [w["id"] for w in seen["may"]] == [widget["id"]]
    assert [w["can_remove"] for w in seen["mo"]] == [True]
    assert [w["can_remove"] for w in seen["may"]] == [False]
    assert [w["can_remove"] for w in seen["ada"]] == [True]
    later = as_may.post(org.path(), json={**SPEND, "title": "Runs"}).json()
    assert [w["title"] for w in as_mo.get(org.path()).json()] == [
        "Runs",
        "Agent spend leaders",
    ]

    # Not another organization's, nor theirs to see.
    assert as_mo.get(org.path(org.other)).status_code == 403
    org.assign(mo, "org:member", org.other)
    assert as_mo.get(org.path(org.other)).json() == []
    elsewhere = f"{org.path(org.other)}/{widget['id']}"
    assert as_mo.delete(elsewhere).status_code == 404

    assert as_may.delete(f"{org.path()}/{widget['id']}").status_code == 403
    assert as_mo.delete(f"{org.path()}/{later['id']}").status_code == 403
    # Its sharer, and an administrator, may.
    assert as_mo.delete(f"{org.path()}/{widget['id']}").status_code == 204
    assert as_ada.delete(f"{org.path()}/{later['id']}").status_code == 204
    assert as_mo.get(org.path()).json() == []
    assert as_mo.delete(f"{org.path()}/{widget['id']}").status_code == 404
