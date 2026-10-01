"""What the assistant has on each screen, from configuration.

``screens.yaml`` (or the file ``FORGE_ADMIN_AGENT_SCREENS`` names) says,
for every screen of the web console a rule matches, which toolsets the
assistant gets there (all their tools or some), what to add to its
instructions, which model it defaults to, and the prompts suggested under a
new conversation's composer. What it says ``everywhere`` applies on every
screen, and when the page isn't shared.

The agents are built once: a supervisor, and a specialist per toolset
(``forge.py``). On every model call, :class:`Screens` says which toolsets
the screen the person is on gives, from the conversation's
``page_context``: the supervisor is offered those specialists, and each
specialist's :class:`ScreenToolset` gives it its toolset's tools there. The
instructions add the screen's sections. A conversation keeps every toolset
it has had (``STICKY``): its history and a call waiting for the person to
confirm it name specialists and tools a later page mightn't have.

The page comes from the browser. It decides which tools are offered, never
what they may do: every tool still acts as the person.
"""

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal
from uuid import uuid4

import yaml
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.llm_request import LlmRequest
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.tool_context import ToolContext
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from forge_admin.agents.page_context import PAGE_CONTEXT, PageContext

logger = logging.getLogger(__name__)

SCREENS_FILE = Path(__file__).with_name("screens.yaml")
# The session state a conversation keeps the toolsets it has had in: each
# toolset's name, and the names of its tools it may use, or null for all.
# The browser can't set it (the routes' CLIENT_STATE).
STICKY = "assistant_toolsets"
# The most prompts suggested at once.
MAX_PROMPTS = 6

Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ToolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$")]
OneOrMore = str | list[str]

# A toolset's grant: its tools' names, or None for all of them.
Grant = frozenset[str] | None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolsetRef(_Strict):
    """A toolset a screen gives the assistant."""

    name: Name
    # Only these tools, by the names the model sees; all when omitted.
    tools: list[ToolName] | None = Field(default=None, min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _by_name(cls, value: Any) -> Any:
        # A bare name is the whole toolset.
        return {"name": value} if isinstance(value, str) else value


class Prompt(_Strict):
    """A prompt suggested under a new conversation's composer."""

    title: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    # Shown after the title, quieter.
    label: Annotated[str, StringConstraints(max_length=120)] = ""
    # What's sent; the title and label when omitted.
    prompt: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = (
        None
    )

    @model_validator(mode="before")
    @classmethod
    def _as_title(cls, value: Any) -> Any:
        # A bare string is its own title and prompt.
        return {"title": value} if isinstance(value, str) else value

    @property
    def text(self) -> str:
        return self.prompt or f"{self.title} {self.label}".strip()


class When(_Strict):
    """
    Which pages a screen is: all that's given must match. Values are the
    page context's, as the web console sends it.
    """

    # The router's route ID, e.g. /organizations/$organizationId.
    route: OneOrMore | None = None
    # Search parameters, e.g. {view: events}; a list matches any, and ""
    # matches a page without the parameter (an organization's Workflows has no view).
    search: dict[str, OneOrMore] = Field(default_factory=dict)
    # A record of this kind on the screen (e.g. organization, background_task).
    entity: OneOrMore | None = None

    def matches(self, page: PageContext) -> bool:
        if self.route is not None and page.route not in _values(self.route):
            return False
        for key, wanted in self.search.items():
            value = page.search.get(key)
            if ("" if value is None else str(value)) not in _values(wanted):
                return False
        if self.entity is not None:
            kinds = {entity.kind for entity in page.entities}
            if page.focus is not None:
                kinds.add(page.focus.kind)
            if not kinds & set(_values(self.entity)):
                return False
        return True


class Everywhere(_Strict):
    """What the assistant has on every screen."""

    toolsets: list[ToolsetRef] = Field(default_factory=list)
    instructions: str = ""
    prompts: list[Prompt] = Field(default_factory=list)


class Screen(Everywhere):
    """What the assistant has on the screens a rule matches."""

    name: Name
    # What the person calls it, e.g. "Workflows".
    title: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    when: When
    # The model it defaults to here, when the conversation hasn't chosen one.
    model: str | None = None
    # The nearest the model offers is used (forge_common.model_provider.thinking).
    thinking_level: (
        Literal["off", "minimal", "low", "medium", "high", "xhigh"] | None
    ) = None


class ScreenConfig(_Strict):
    """The whole configuration: ``screens.yaml``."""

    everywhere: Everywhere = Field(default_factory=Everywhere)
    screens: list[Screen] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "ScreenConfig":
        names = [screen.name for screen in self.screens]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"screen names must be unique: {', '.join(repeated)}")
        return self

    @classmethod
    def load(cls, path: Path = SCREENS_FILE) -> "ScreenConfig":
        """
        :param path: A YAML file in this shape.
        :raises ValueError: It isn't valid YAML, or not this shape.
        """
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return cls.model_validate(data)
        except (yaml.YAMLError, ValidationError) as error:
            raise ValueError(
                f"{path} isn't a valid screen configuration: {error}"
            ) from None


@dataclass
class AssistantToolset:
    """
    A toolset the configuration can name, and the specialist agent that
    holds it.

    :param name: Its name in the configuration, and its specialist's.
    :param title: What the person sees it called.
    :param description: What it helps with, for the person.
    :param toolset: The toolset; None where it isn't set up, e.g. without
        the admin API its tools call.
    :param instruction: How its specialist uses it.
    :param tools: Its tools' names, when they're known before it runs; a
        configuration naming another fails to load. None for a toolset
        whose tools come from a server.
    :param delegate: What its specialist does, for the supervisor choosing
        one; the description by default.
    """

    name: str
    title: str
    description: str
    toolset: BaseToolset | None
    instruction: str = ""
    tools: frozenset[str] | None = None
    delegate: str = ""


@dataclass(frozen=True)
class Resolved:
    """What the assistant has on one page."""

    screens: tuple[Screen, ...] = ()
    toolsets: Mapping[str, Grant] = field(default_factory=dict)
    instructions: tuple[str, ...] = ()
    prompts: tuple[Prompt, ...] = ()
    model: str | None = None
    thinking_level: str | None = None


class Screens:
    """
    What the assistant has on each screen: the configuration, over the
    toolsets it can name.

    :param config: The screens.
    :param toolsets: Every toolset the configuration can name, by name.
    :raises ValueError: The configuration names a toolset, a tool or a model
        that doesn't exist.
    """

    def __init__(
        self,
        config: ScreenConfig,
        toolsets: Iterable[AssistantToolset],
        *,
        models: Iterable[str] | None = None,
    ) -> None:
        self.config = config
        self.toolsets = {toolset.name: toolset for toolset in toolsets}
        _check(config, self.toolsets, None if models is None else set(models))

    # -- Resolving -----------------------------------------------------------

    def resolve(self, page: PageContext | None) -> Resolved:
        """What the configuration gives the assistant on ``page``; only
        ``everywhere`` without one."""
        matched = tuple(
            screen
            for screen in self.config.screens
            if page is not None and screen.when.matches(page)
        )
        parts: list[Everywhere] = [*matched, self.config.everywhere]
        grants: dict[str, Grant] = {}
        for part in parts:
            for ref in part.toolsets:
                _grant(
                    grants,
                    ref.name,
                    None if ref.tools is None else frozenset(ref.tools),
                )
        prompts: list[Prompt] = []
        for part in parts:
            prompts += [
                p for p in part.prompts if p.text not in {q.text for q in prompts}
            ]
        return Resolved(
            screens=matched,
            toolsets=grants,
            instructions=tuple(
                p.instructions.strip() for p in parts if p.instructions.strip()
            ),
            prompts=tuple(prompts[:MAX_PROMPTS]),
            model=next((s.model for s in matched if s.model), None),
            thinking_level=next(
                (s.thinking_level for s in matched if s.thinking_level), None
            ),
        )

    def resolve_state(self, state: Mapping[str, Any]) -> Resolved:
        """What the configuration gives the assistant on the conversation's page."""
        return self.resolve(page_of(state))

    def active(self, state: Mapping[str, Any]) -> dict[str, Grant]:
        """
        The toolsets the assistant has now: its page's, and those the
        conversation had before, where they're set up.
        """
        grants = dict(sticky_of(state))
        for name, grant in self.resolve_state(state).toolsets.items():
            _grant(grants, name, grant)
        return {
            name: grant
            for name, grant in grants.items()
            if name in self.toolsets and self.toolsets[name].toolset is not None
        }

    def remember(self, callback_context: CallbackContext) -> None:
        """
        From a ``before_model_callback``: the conversation keeps the toolsets
        its page gives it, those set up, for every call after this one.
        """
        state = callback_context.state
        current = state.to_dict()
        grants = dict(sticky_of(current))
        for name, grant in self.active(current).items():
            _grant(grants, name, grant)
        encoded = {
            name: None if grant is None else sorted(grant)
            for name, grant in grants.items()
        }
        if encoded and encoded != current.get(STICKY):
            state[STICKY] = encoded

    def available(self) -> list[AssistantToolset]:
        """The toolsets that are set up, each with a specialist."""
        return [entry for entry in self.toolsets.values() if entry.toolset is not None]

    def toolset(self, name: str) -> "ScreenToolset":
        """The tools of toolset ``name`` the person's screen gives, for its
        specialist."""
        return ScreenToolset(self, name)

    # -- For the person ------------------------------------------------------

    async def describe(
        self,
        *,
        user_id: str,
        page: PageContext | None,
        sticky: Mapping[str, Any] | None = None,
        tools: bool = False,
    ) -> dict[str, Any]:
        """
        What the assistant has on ``page`` for the person, as they'd read it:
        the screens it matched, each toolset and, with ``tools``, its tools,
        the prompts to suggest, and every specialist's name and title.

        :param user_id: The person, whose tools are listed as theirs.
        :param page: Their page; None when they don't share it.
        :param sticky: A conversation's ``STICKY`` state, whose toolsets it
            keeps from earlier pages.
        :param tools: List each toolset's tools too (a server's are asked for).
        """
        state: dict[str, Any] = {
            PAGE_CONTEXT: page.model_dump(mode="json") if page else None
        }
        if sticky:
            state[STICKY] = dict(sticky)
        resolved = self.resolve(page)
        here = {
            name
            for name in resolved.toolsets
            if name in self.toolsets and self.toolsets[name].toolset is not None
        }
        described = []
        for name, grant in self.active(state).items():
            entry = self.toolsets[name]
            item: dict[str, Any] = {
                "name": name,
                "agent": name,
                "title": entry.title,
                "description": entry.description,
                "fromEarlier": name not in here,
            }
            if tools:
                item["tools"] = await self._tools(entry, grant, user_id, state)
            described.append(item)
        return {
            "screens": [{"name": s.name, "title": s.title} for s in resolved.screens],
            "toolsets": described,
            # Every specialist, on any page, so its messages show its title.
            "specialists": [
                {"name": entry.name, "title": entry.title} for entry in self.available()
            ],
            "prompts": [
                {"title": p.title, "label": p.label, "prompt": p.text}
                for p in resolved.prompts
            ],
        }

    async def _tools(
        self, entry: AssistantToolset, grant: Grant, user_id: str, state: dict[str, Any]
    ) -> list[dict[str, Any]]:
        assert entry.toolset is not None
        try:
            listed = await entry.toolset.get_tools_with_prefix(_Reader(user_id, state))  # type: ignore[arg-type]
        except Exception:
            logger.warning(
                "Couldn't list the %s toolset's tools", entry.name, exc_info=True
            )
            return []
        return [
            {
                "name": tool.name,
                "description": _summary(tool.description),
                "asksFirst": bool(getattr(tool, "_require_confirmation", False)),
            }
            for tool in listed
            if grant is None or tool.name in grant
        ]


class ScreenToolset(BaseToolset):
    """
    A specialist's one toolset: on each call, the tools of its toolset that
    the person's screen gives, or that the conversation had before; none
    where it gives none.

    :param screens: The screens.
    :param name: The toolset's name; it must be set up.
    """

    def __init__(self, screens: Screens, name: str) -> None:
        super().__init__()
        entry = screens.toolsets[name]
        if entry.toolset is None:
            raise ValueError(f"The {name} toolset isn't set up")
        self.screens = screens
        self.name = name
        self.inner: BaseToolset = entry.toolset

    def grant(self, state: Mapping[str, Any]) -> Grant | Literal[False]:
        """Its tools the conversation may use now; False for none."""
        return self.screens.active(state).get(self.name, False)

    async def get_tools(
        self, readonly_context: ReadonlyContext | None = None
    ) -> list[BaseTool]:
        state = readonly_context.state if readonly_context is not None else {}
        grant = self.grant(state)
        if grant is False:
            return []
        listed = await self.inner.get_tools_with_prefix(readonly_context)
        return [tool for tool in listed if grant is None or tool.name in grant]

    async def process_llm_request(
        self, *, tool_context: ToolContext, llm_request: LlmRequest
    ) -> None:
        if self.grant(tool_context.state.to_dict()) is not False:
            await self.inner.process_llm_request(
                tool_context=tool_context, llm_request=llm_request
            )

    async def close(self) -> None:
        try:
            await self.inner.close()
        except Exception:
            logger.warning("Couldn't close the %s toolset", self.name, exc_info=True)


def load(
    toolsets: Iterable[AssistantToolset],
    path: Path | None = None,
    *,
    models: Iterable[str] | None = None,
) -> Screens:
    """
    The screens from ``path`` (the bundled ``screens.yaml`` by default), over
    ``toolsets``.

    :raises ValueError: The file isn't a valid configuration, or names a
        toolset, tool or model that doesn't exist.
    """
    return Screens(ScreenConfig.load(path or SCREENS_FILE), toolsets, models=models)


def page_of(state: Mapping[str, Any]) -> PageContext | None:
    """The conversation's page, as its last message sent it."""
    value = state.get(PAGE_CONTEXT)
    if value is None:
        return None
    try:
        return PageContext.model_validate(value)
    except ValidationError:
        return None


def sticky_of(state: Mapping[str, Any]) -> dict[str, Grant]:
    """The toolsets a conversation kept, from its ``STICKY`` state."""
    value = state.get(STICKY)
    if not isinstance(value, Mapping):
        return {}
    grants: dict[str, Grant] = {}
    for name, tools in value.items():
        if isinstance(name, str) and (tools is None or isinstance(tools, list)):
            _grant(grants, name, None if tools is None else frozenset(map(str, tools)))
    return grants


class _Reader:
    """Enough of an invocation's context to list a toolset's tools for a person."""

    def __init__(self, user_id: str, state: Mapping[str, Any]) -> None:
        self.user_id = user_id
        self.state = MappingProxyType(dict(state))
        # Never an invocation's, so no invocation's cached tools are reused.
        self.invocation_id = f"describe-{uuid4()}"


def _grant(grants: dict[str, Grant], name: str, grant: Grant) -> None:
    """Adds a toolset's grant: all of it wins; else the tools add up."""
    if name not in grants:
        grants[name] = grant
    elif grants[name] is not None:
        grants[name] = None if grant is None else grants[name] | grant  # type: ignore[operator]


def _values(value: OneOrMore) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _summary(description: str | None) -> str:
    """A tool's description to its first paragraph, without its argument list."""
    text = (description or "").strip()
    first = re.split(r"\n\s*\n|\n\s*Args:", text, maxsplit=1)[0]
    return " ".join(first.split())


def _check(
    config: ScreenConfig,
    toolsets: Mapping[str, AssistantToolset],
    models: set[str] | None,
) -> None:
    problems = []
    parts: list[tuple[str, Everywhere]] = [("everywhere", config.everywhere)]
    parts += [(f"screen {s.name}", s) for s in config.screens]
    for where, part in parts:
        for ref in part.toolsets:
            known = toolsets.get(ref.name)
            if known is None:
                problems.append(f"{where}: no toolset named {ref.name}")
            elif ref.tools and known.tools is not None:
                unknown = sorted(set(ref.tools) - known.tools)
                if unknown:
                    problems.append(
                        f"{where}: {ref.name} has no tools {', '.join(unknown)}"
                    )
        if (
            isinstance(part, Screen)
            and part.model
            and models is not None
            and part.model not in models
        ):
            problems.append(
                f"{where}: model {part.model} isn't one the assistant may use"
            )
    if problems:
        raise ValueError("Invalid screen configuration: " + "; ".join(problems))
