"""A person's dashboard layouts: what a layout may be, who may have one, and,
against MySQL, saving, reading and resetting it."""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from forge_admin.api.routes import dashboards
from forge_admin.api.routes.dashboards import LayoutDocument
from forge_admin.api.server import ApiServer
from forge_admin.auth.authorization import API_KEY_SUBJECT_PREFIX
from forge_admin.config import Settings

PATH = "/api/v1/me/dashboards/organization-overview"


def widget(name: str, x: int, y: int, w: int, h: int = 100, **more: Any) -> dict:
    return {"id": name, "type": name, "x": x, "y": y, "w": w, "h": h, **more}


# The web console's default overview (OVERVIEW_LAYOUT): three rows of thirds.
OVERVIEW = {
    "version": 1,
    "widgets": [
        widget("tokens", 0, 0, 8),
        widget("models", 8, 0, 4),
        widget("activity", 0, 1, 4),
        widget("outcomes", 4, 1, 4),
        widget("spend", 8, 1, 4),
        widget("people", 0, 2, 4),
        widget("steps", 4, 2, 4),
        widget("attention", 8, 2, 4),
    ],
}


def with_widget(**changes: Any) -> dict:
    """The default overview, its first widget changed."""
    first = {**OVERVIEW["widgets"][0], **changes}
    return {**OVERVIEW, "widgets": [first, *OVERVIEW["widgets"][1:]]}


def test_the_default_overview_is_a_layout() -> None:
    LayoutDocument.model_validate(OVERVIEW)


def test_a_widget_may_have_a_title_and_plain_settings() -> None:
    settings = {"split": "subject", "stacked": True, "top": 5, "share": 0.5, "x": None}
    layout = LayoutDocument.model_validate(
        with_widget(title="Spend this week", settings=settings)
    )
    assert layout.widgets[0].title == "Spend this week"
    assert layout.widgets[0].settings == settings


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param(with_widget(x=8, w=6), id="past the last column"),
        pytest.param(with_widget(x=-1), id="before the first column"),
        pytest.param(with_widget(w=0), id="no width"),
        pytest.param(with_widget(w=13), id="wider than the grid"),
        pytest.param(with_widget(h=0), id="no height"),
        pytest.param(with_widget(h=4001), id="too tall"),
        pytest.param(with_widget(y=-5), id="above the top"),
        pytest.param(with_widget(id="models"), id="repeated ID"),
        pytest.param(with_widget(id="has space"), id="ID with a space"),
        pytest.param(with_widget(type="Tokens"), id="type not as named"),
        pytest.param(with_widget(title=""), id="empty title"),
        pytest.param(with_widget(title="t" * 121), id="long title"),
        pytest.param(with_widget(colour="red"), id="unknown widget field"),
        pytest.param({**OVERVIEW, "theme": "dark"}, id="unknown layout field"),
        pytest.param({"widgets": []}, id="no version"),
        pytest.param({**OVERVIEW, "version": 0}, id="version 0"),
        pytest.param(
            {
                "version": 1,
                "widgets": [widget(f"w{i}", 0, i, 12) for i in range(65)],
            },
            id="too many widgets",
        ),
        pytest.param(with_widget(settings={"split": {"by": "type"}}), id="nested"),
        pytest.param(with_widget(settings={"split": ["a"]}), id="list setting"),
        pytest.param(with_widget(settings={"1st": 1}), id="setting name"),
        pytest.param(with_widget(settings={"share": float("nan")}), id="NaN"),
        pytest.param(with_widget(settings={"note": "n" * 501}), id="long text"),
        pytest.param(
            with_widget(settings={f"s{i}": i for i in range(33)}),
            id="too many settings",
        ),
    ],
)
def test_a_layout_stays_on_the_grid_and_within_its_limits(layout: dict) -> None:
    with pytest.raises(ValidationError):
        LayoutDocument.model_validate(layout)


def client_for(settings: Settings, user: str) -> TestClient:
    local = settings.model_copy(update={"local_user_id": user})
    return TestClient(ApiServer(local, routers=[dashboards.router]).create_app())


def test_an_api_key_has_no_layouts(settings: Settings) -> None:
    with client_for(settings, f"{API_KEY_SUBJECT_PREFIX}{uuid4()}") as client:
        assert client.get(PATH).status_code == 403
        assert client.put(PATH, json=OVERVIEW).status_code == 403
        assert client.delete(PATH).status_code == 403


def test_only_known_dashboards_have_layouts(settings: Settings) -> None:
    with client_for(settings, "someone") as client:
        response = client.get("/api/v1/me/dashboards/everything")
    assert response.status_code == 422


@pytest.mark.mysql
def test_a_person_saves_reads_and_resets_their_layout(
    client_as: Callable[[str], TestClient],
) -> None:
    person, other = f"person-{uuid4().hex[:8]}", f"person-{uuid4().hex[:8]}"
    mine, theirs = client_as(person), client_as(other)
    try:
        none = mine.get(PATH)
        assert none.status_code == 200
        assert none.json() == {
            "dashboard": "organization-overview",
            "layout": None,
            "updated_at": None,
        }

        saved = mine.put(PATH, json=OVERVIEW)
        assert saved.status_code == 200
        assert saved.json()["layout"] == OVERVIEW
        assert saved.json()["updated_at"]
        assert mine.get(PATH).json()["layout"] == OVERVIEW

        # A new layout replaces the last, as it was sent.
        moved = with_widget(title="Tokens", settings={"split": "token"})
        assert mine.put(PATH, json=moved).json()["layout"] == moved
        assert mine.get(PATH).json()["layout"] == moved

        # Someone else's layout is their own.
        assert theirs.get(PATH).json()["layout"] is None
        assert theirs.put(PATH, json=OVERVIEW).status_code == 200
        assert mine.get(PATH).json()["layout"] == moved

        refused = mine.put(PATH, json=with_widget(x=8, w=6))
        assert refused.status_code == 422
        assert mine.get(PATH).json()["layout"] == moved

        assert mine.delete(PATH).status_code == 204
        assert mine.get(PATH).json()["layout"] is None
        assert mine.delete(PATH).status_code == 204
        assert theirs.get(PATH).json()["layout"] == OVERVIEW
    finally:
        mine.delete(PATH)
        theirs.delete(PATH)
