"""The base of the assistant's toolsets over this API's own routes.

A :class:`RouteToolset`'s tools call the routes the web console uses, as the
conversation's user (``person_api.py``), whom the ``/agents`` routes checked
is the signed-in person: each call is authorized in its scope and audited as
theirs, so a tool can do exactly what they could in the console, and nothing
more. Who they are comes from the conversation, never from the model's
arguments.

A subclass names its tools (``READ_TOOLS``, ``CHANGE_TOOLS``) and writes
each as a method whose docstring the model reads. Tools that change
something ask the person to confirm each call first, unless the toolset is
made with ``confirm_changes=False``, in the toolset's own words
(:meth:`RouteToolset.confirmation_hint`). As a :class:`ForgeBaseToolset`, no
tool raises: a refused call answers ``failed`` with the route's reason and
how to go on, and large results are cut down.
"""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar
from urllib.parse import quote

from forge_common.adk import ForgeBaseToolset, ToolFailure
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

from forge_admin.assistant.person_api import PersonApi, Refusal

logger = logging.getLogger(__name__)

# The routes' ID formats, checked before an ID goes into a path, so no
# argument can reach another route through it.
UUID = re.compile(r"^[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}$")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Seconds a call may take: a read, and a change, which may wait on the async
# worker or MongoDB.
READ_TIMEOUT = 60.0
CHANGE_TIMEOUT = 120.0

# How to go on after a refusal, by its status; a toolset adds its own.
FIXES: Mapping[int, Sequence[str]] = {
    0: [
        "The assistant can't act as the person here; tell them to do it in the web "
        "console."
    ],
    401: ["The person's sign-in isn't accepted: they sign in to Forge again."],
    403: [
        "The person lacks the permission this needs there (the reason names it). "
        "Tell them, and who can do it or give them a role with it; don't retry."
    ],
    404: [
        "Check the IDs against earlier results, or list them again with this "
        "toolset's list tools."
    ],
    409: [
        "Something about the record's state is in the way, as the reason says. Read "
        "it again and fit the call to it, or tell the person what's needed."
    ],
    410: ["It's gone: tell the person."],
    413: ["The answer was too large: ask for fewer records."],
    422: ["Fix the arguments as the reason says, then call again."],
    502: [
        "That part of Forge isn't available right now: tell the person and try later."
    ],
    503: [
        "That part of Forge isn't available right now: tell the person and try later."
    ],
}


class ApiRefused(ToolFailure):
    """
    The API refused a tool's call.

    :param refusal: Its status and reason.
    """

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.detail)
        self.status = refusal.status


class AsksFirst(FunctionTool):
    """
    A tool that asks the person to confirm each call before it runs, in
    words they can follow: :class:`AskInWords` asks its toolset's question
    (:meth:`RouteToolset.confirmation_hint`) in place of ADK's.

    :param func: The tool's method.
    :param toolset: The toolset that words the question.
    """

    def __init__(self, func: Callable[..., Any], toolset: "RouteToolset") -> None:
        super().__init__(func, require_confirmation=True)
        self._toolset = toolset

    async def confirmation_hint(
        self, args: Mapping[str, Any], tool_context: ToolContext
    ) -> str | None:
        """What the person is asked before this call runs; None for ADK's words."""
        return await self._toolset.confirmation_hint(self.name, args, tool_context)


class AskInWords(BasePlugin):
    """
    Asks the person to confirm a tool call in the tool's words, where it has
    some (``confirmation_hint``, as :class:`AsksFirst` does).

    ADK's gate asks in words written for the client that answers it ("Please
    approve or reject the tool call x() by responding with a
    FunctionResponse ..."), and the chat's approval card shows them to the
    person. After the gate has asked, this asks the tool's question instead:
    its first line is what the card asks, the lines after it say what
    approving does. Whatever goes wrong wording it leaves ADK's.

    When the person denies such a call, the model hears that they chose
    not to, rather than ADK's "This tool call is rejected.", which reads as
    an error to work around.
    """

    def __init__(self) -> None:
        super().__init__(name="ask_in_words")

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        words = getattr(tool, "confirmation_hint", None)
        decided = tool_context.tool_confirmation
        if words is not None and decided is not None and not decided.confirmed:
            return {
                "status": "failed",
                "reason": "The person chose not to allow this call when asked, so "
                "it didn't run.",
                "suggested_fixes": [
                    "Tell them it didn't run, and don't call it again unless they "
                    "ask; ask what they'd like instead."
                ],
            }
        call_id = tool_context.function_call_id
        asked = tool_context.actions.requested_tool_confirmations.get(call_id or "")
        if asked is None or words is None:
            return None
        try:
            hint = await words(tool_args, tool_context)
        except Exception:
            logger.warning(
                "Couldn't word the confirmation of %s", tool.name, exc_info=True
            )
            return None
        if hint:
            tool_context.request_confirmation(hint=hint, payload=asked.payload)
        return None


class RouteToolset(ForgeBaseToolset):
    """
    Tools that call this API's routes as the person the assistant is talking
    to.

    :param api: The admin API, called as the person.
    :param confirm_changes: Ask the person to confirm each change first.
    :param kwargs: :class:`ForgeBaseToolset`'s (``max_result_chars``,
        ``tool_filter``, ``tool_name_prefix``, ...).
    """

    # The tool methods that only read, and those that change something.
    READ_TOOLS: ClassVar[tuple[str, ...]] = ()
    CHANGE_TOOLS: ClassVar[tuple[str, ...]] = ()
    # The change tools that ask the person first; None for all of them.
    CONFIRMED: ClassVar[frozenset[str] | None] = None
    # Fixes by status, over the generic ones (``FIXES``).
    STATUS_FIXES: ClassVar[Mapping[int, Sequence[str]]] = {}
    # Seconds a tool may take, by name, over READ_TIMEOUT and CHANGE_TIMEOUT.
    TIMEOUTS: ClassVar[Mapping[str, float]] = {}

    default_suggested_fixes = (
        "Check the arguments against the tool's description, then call it again.",
    )

    def __init__(
        self, api: PersonApi, *, confirm_changes: bool = True, **kwargs: Any
    ) -> None:
        kwargs.setdefault("timeout_seconds", READ_TIMEOUT)
        super().__init__(**kwargs)
        self._api = api
        asks = self.CONFIRMED if self.CONFIRMED is not None else self.CHANGE_TOOLS
        self._tools: list[BaseTool] = [
            *(FunctionTool(getattr(self, name)) for name in self.READ_TOOLS),
            *(
                AsksFirst(getattr(self, name), self)
                if confirm_changes and name in asks
                else FunctionTool(getattr(self, name))
                for name in self.CHANGE_TOOLS
            ),
        ]

    @classmethod
    def tool_names(cls) -> frozenset[str]:
        """Every tool's name, for the screen configuration to check."""
        return frozenset(cls.READ_TOOLS + cls.CHANGE_TOOLS)

    async def confirmation_hint(
        self, name: str, args: Mapping[str, Any], tool_context: ToolContext
    ) -> str | None:
        """
        What the person is asked before a change runs, for someone who
        isn't technical: a question on the first line ("Give Ada Lovelace
        the Admin role in Acme?"), then what approving does on the next. The
        approval card shows it over the call's arguments, so it names things
        the arguments only give by ID. Override it for the toolset's changes.

        :param name: The tool, under its own name.
        :param args: What the model called it with.
        :param tool_context: The call's, to look names up as the person.
        :return: The question, or None to let the card ask in general terms.
        """
        return None

    async def _read_quietly(
        self, tool_context: ToolContext, path: Callable[[], str]
    ) -> Any:
        """
        A route's answer, to name something in a question by; None when it
        can't be read, as the call itself will then say.

        :param path: Makes the route's path; it may refuse the arguments.
        """
        try:
            return await self._call(tool_context, "GET", path())
        except Exception as error:
            logger.info("Couldn't read what a question names: %s", error)
            return None

    async def get_raw_tools(
        self, readonly_context: ReadonlyContext | None = None
    ) -> list[BaseTool]:
        return self._tools

    def suggest_fixes(
        self, tool: BaseTool, args: dict[str, Any], error: Exception
    ) -> Sequence[str]:
        if isinstance(error, ApiRefused):
            fixes = self.STATUS_FIXES.get(error.status) or FIXES.get(error.status)
            if fixes:
                return fixes
        return super().suggest_fixes(tool, args, error)

    def to_payload(self, tool: BaseTool, result: Any) -> Any:
        # ADK's own replies, e.g. to a missing argument or a call the person
        # rejected, come back as {"error": ...}; every tool here returns more.
        if isinstance(result, dict) and set(result) == {"error"}:
            raise ToolFailure(str(result["error"]))
        return super().to_payload(tool, result)

    def timeout_for(self, tool: BaseTool) -> float | None:
        if tool.name in self.TIMEOUTS:
            return self.TIMEOUTS[tool.name]
        if tool.name in self.CHANGE_TOOLS:
            return CHANGE_TIMEOUT
        return super().timeout_for(tool)

    async def close(self) -> None:
        try:
            await self._api.aclose()
        finally:
            await super().close()

    async def _call(
        self,
        tool_context: ToolContext,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        text: bool = False,
    ) -> Any:
        """
        One route, as the conversation's person.

        :param text: Answer its text rather than its JSON (``PersonApi.call``).

        :raises ToolFailure: There's no person to act as.
        :raises ApiRefused: The route refused it.
        """
        user_id = tool_context.user_id
        if not user_id:
            raise ToolFailure("No signed-in person to act as.")
        try:
            return await self._api.call(
                user_id, method, path, params=params, json=json, text=text
            )
        except Refusal as refusal:
            raise ApiRefused(refusal) from None


def uuid_arg(value: str | None, name: str) -> str:
    """
    An ID in UUID form, safe in a path.

    :raises ToolFailure: It isn't one.
    """
    if not UUID.fullmatch(value or ""):
        raise ToolFailure(f"{name} must be an ID in UUID form, not {value!r}")
    # The routes take Forge's IDs in lowercase.
    return str(value).lower()


def slug_arg(value: str | None, name: str, pattern: re.Pattern[str] = SLUG) -> str:
    """
    A key or name matching ``pattern``, quoted for a path.

    :raises ToolFailure: It doesn't match.
    """
    if not pattern.fullmatch(value or ""):
        raise ToolFailure(f"{name} isn't a valid key: {value!r}")
    return quote(str(value), safe="")


def pick(value: Any, fields: Sequence[str]) -> dict[str, Any]:
    """The given fields of a record, those it has."""
    if not isinstance(value, dict):
        return {}
    return {field: value[field] for field in fields if value.get(field) is not None}


def given(**values: Any) -> dict[str, Any]:
    """The arguments that were given: a request body or query without Nones."""
    return {key: value for key, value in values.items() if value is not None}


# The most characters of a name a question quotes.
QUOTED_CHARS = 80


def quoted(name: Any, otherwise: str) -> str:
    """
    A name as a question quotes it (“HR helper”), or ``otherwise`` ("the
    agent") when there's none.
    """
    text = " ".join(name.split()) if isinstance(name, str) else ""
    if not text:
        return otherwise
    if len(text) > QUOTED_CHARS:
        text = text[: QUOTED_CHARS - 1].rstrip() + "…"
    return f"“{text}”"
