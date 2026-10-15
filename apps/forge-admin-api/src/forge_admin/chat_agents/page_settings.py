"""
How a chat page looks and what it offers, beyond its agent: what its builder
sets (``PUT …/chat-page``), what the agent's record keeps (``chat_page``) and
what the page reads (``GET /runtime/pages/{token}``).

- Its words: the name over it, the welcome an empty conversation shows, the
  message box's placeholder, a note under the box, the prompts it offers.
- What it shows: the visitor's conversations, its top bar, follow-up questions.
- Its look: light, dark or the visitor's own, and whether visitors can switch
  it; an accent colour; how the visitor's messages are drawn; its corners,
  font and width.

Every setting has a default, so a page set before a setting existed reads as
it always looked.
"""

import copy
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: The most prompts a chat page offers.
MAX_SUGGESTIONS = 6

Suggestion = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


Title = Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]
Heading = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
Message = Annotated[str, StringConstraints(strip_whitespace=True, max_length=600)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]

Theme = Literal["system", "light", "dark"]
#: How the visitor's messages are drawn: on the page's grey, in the accent,
#: or tinted with it.
Bubbles = Literal["neutral", "accent", "tint"]
Corners = Literal["default", "rounded", "square"]
Font = Literal["default", "system", "serif", "mono"]
Width = Literal["default", "narrow", "wide"]
#: An accent: a colour as ``#rrggbb``.
Accent = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]


class ChatPageSettings(BaseModel):
    """Everything a chat page's builder sets, as the page reads it."""

    #: The prompts an empty conversation offers, under the message box.
    suggestions: list[str] = Field(default_factory=list)
    #: The page leaves out the visitor's list of conversations.
    hide_history: bool = False
    #: The page leaves out its top bar.
    hide_top_bar: bool = False
    #: The page suggests follow-up questions after each reply.
    follow_ups: bool = True
    #: The name over the page and in the browser's tab; "" for the agent's.
    title: str = ""
    #: The welcome's heading; "" for "Talk to <name>".
    welcome_title: str = ""
    #: The welcome's message under it; "" for the agent's description.
    welcome_message: str = ""
    #: The message box's placeholder; "" for "Send a message...".
    placeholder: str = ""
    #: A note under the message box, e.g. that answers can be wrong; "" for none.
    footer_note: str = ""
    #: Light (until it's set), dark, or as the visitor's system is.
    theme: Theme = "light"
    #: A button on the page that switches it between light and dark, each
    #: visitor's choice kept in their browser.
    theme_toggle: bool = True
    #: The accent (buttons, focus, the visitor's messages); null for Forge's ink.
    accent: str | None = None
    bubbles: Bubbles = "neutral"
    corners: Corners = "default"
    font: Font = "default"
    width: Width = "default"


#: Each setting's default: a page that hasn't set it reads it so.
PAGE_DEFAULTS: dict[str, Any] = ChatPageSettings().model_dump()


class ChatPageSettingsChange(BaseModel):
    """
    A change to a chat page's settings: what's omitted stays as it is.
    ``accent`` null goes back to Forge's ink; a text "" to its default.
    """

    model_config = ConfigDict(extra="forbid")

    #: In order (a click sends one); [] offers none.
    suggestions: list[Suggestion] | None = Field(
        default=None, max_length=MAX_SUGGESTIONS
    )
    hide_history: bool | None = None
    hide_top_bar: bool | None = None
    follow_ups: bool | None = None
    title: Title | None = None
    welcome_title: Heading | None = None
    welcome_message: Message | None = None
    placeholder: Heading | None = None
    footer_note: Note | None = None
    theme: Theme | None = None
    theme_toggle: bool | None = None
    accent: Accent | None = None
    bubbles: Bubbles | None = None
    corners: Corners | None = None
    font: Font | None = None
    width: Width | None = None

    def changes(self) -> dict[str, Any]:
        """
        :return: The settings it changes: those it names (null too, for
            ``accent``), the same prompt twice offered once.
        """
        named = {
            key: value
            for key, value in self.model_dump(exclude_unset=True).items()
            if value is not None or key == "accent"
        }
        if "suggestions" in named:
            named["suggestions"] = list(dict.fromkeys(named["suggestions"]))
        if isinstance(named.get("accent"), str):
            named["accent"] = named["accent"].lower()
        return named


def page_settings(page: Mapping[str, Any] | None) -> dict[str, Any]:
    """:return: A kept chat page's settings, each it hasn't set at its default."""
    kept = page or {}
    return {
        key: copy.deepcopy(kept.get(key, default))
        for key, default in PAGE_DEFAULTS.items()
    }
