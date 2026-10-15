"""A chat page's settings (``chat_agents.page_settings``): what a change
names, and how a page kept before a setting existed reads."""

import pytest
from pydantic import ValidationError

from forge_admin.chat_agents.page_settings import (
    PAGE_DEFAULTS,
    ChatPageSettingsChange,
    page_settings,
)


def test_a_change_names_only_what_it_sets() -> None:
    change = ChatPageSettingsChange.model_validate(
        {
            "suggestions": [" Hi ", "Track my order", "Hi"],
            "welcome_title": "  Welcome  ",
            "accent": "#A1B2C3",
            "hide_history": None,
        }
    )
    # The same prompt twice is offered once; texts trimmed; one spelling of
    # the accent; a null that isn't the accent changes nothing.
    assert change.changes() == {
        "suggestions": ["Hi", "Track my order"],
        "welcome_title": "Welcome",
        "accent": "#a1b2c3",
    }
    # A null accent goes back to Forge's ink.
    assert ChatPageSettingsChange(accent=None).changes() == {"accent": None}
    assert ChatPageSettingsChange().changes() == {}


@pytest.mark.parametrize(
    "wrong",
    [
        {"accent": "#abc"},
        {"accent": "red"},
        {"theme": "sepia"},
        {"bubbles": "round"},
        {"corners": "pill"},
        {"font": "comic"},
        {"width": "full"},
        {"footer_note": "x" * 301},
        {"suggestions": [f"Q{n}" for n in range(7)]},
        {"suggestions": ["  "]},
        {"sparkles": True},
    ],
)
def test_a_change_that_doesnt_fit_is_refused(wrong: dict) -> None:
    with pytest.raises(ValidationError):
        ChatPageSettingsChange.model_validate(wrong)


def test_a_page_kept_before_a_setting_existed_has_its_default() -> None:
    older = {
        "token": "cp_x",
        "enabled": True,
        "suggestions": ["Hi"],
        "hide_history": True,
    }
    read = page_settings(older)
    assert set(read) == set(PAGE_DEFAULTS)
    assert (read["suggestions"], read["hide_history"]) == (["Hi"], True)
    assert (read["theme"], read["accent"], read["follow_ups"]) == ("light", None, True)
    # Visitors can switch its theme until it's turned off.
    assert read["theme_toggle"] is True
    # What it reads is its own: changing it changes neither the page nor the defaults.
    read["suggestions"].append("More")
    assert older["suggestions"] == ["Hi"] and PAGE_DEFAULTS["suggestions"] == []
    assert page_settings(None) == PAGE_DEFAULTS
