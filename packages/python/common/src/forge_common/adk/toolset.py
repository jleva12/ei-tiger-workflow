"""A Google ADK toolset whose tools never raise and never flood the model's context.

Every tool a :class:`ForgeBaseToolset` gives an agent answers in one of two shapes::

    {"status": "success", "payload": <what the tool returned>}
    {"status": "failed", "reason": "ValueError: no such branch",
     "suggested_fixes": ["Call list_branches and retry with one of its names."]}

A payload whose JSON runs past ``max_result_chars`` is written whole to a file and
cut down to fit, keeping its shape: long strings end early, long lists and objects
keep their first entries, and each cut says how much it left out. The answer says
where the whole payload is, for an agent that needs more of it::

    {"status": "success", "payload": <the cut-down payload>, "truncated": true,
     "complete_result_location": "/tmp/forge-tool-results/search_code-1a2b3c.json"}

A call that runs past ``timeout_seconds`` fails as a :class:`ToolTimeout`;
override :meth:`ForgeBaseToolset.timeout_for` to give slow tools longer. The
deadline stops an async tool at its next ``await``. A sync tool, which ADK runs
on the event loop, can't be stopped: it answers when it returns.

A subclass lists its tools in :meth:`ForgeBaseToolset.get_raw_tools`, and
``get_tools`` filters and guards them. A tool that returns its failures instead
of raising them fails through :meth:`ForgeBaseToolset.to_payload`. The fixes a
failure suggests come from a :class:`ToolFailure` the tool raises, then
``default_suggested_fixes``; override
:meth:`ForgeBaseToolset.suggest_fixes` to suggest fixes for errors a tool can't
anticipate, such as a client library's.
"""

import asyncio
import copy
import json
import logging
import os
import re
import tempfile
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, final

import pydantic_core
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset, ToolPredicate
from google.adk.tools.tool_context import ToolContext
from google.genai import types

__all__ = [
    "DEFAULT_MAX_RESULT_CHARS",
    "DEFAULT_RESULT_DIR",
    "ForgeBaseToolset",
    "ToolFailed",
    "ToolFailure",
    "ToolSuccess",
    "ToolTimeout",
]

logger = logging.getLogger(__name__)

# About 5,000 tokens: past it, a payload is cut down.
DEFAULT_MAX_RESULT_CHARS = 20_000
# Where complete payloads go unless a toolset names a directory.
DEFAULT_RESULT_DIR = Path(tempfile.gettempdir()) / "forge-tool-results"
# How deep ADK looks for media parts in a result: its entries and theirs.
_MEDIA_DEPTH = 2
# Set on a guarded tool, so a toolset built from another's tools guards them once.
_GUARDED = "_forge_guarded"

RunAsync = Callable[..., Awaitable[Any]]


class ToolSuccess(TypedDict):
    """A guarded tool's answer when the tool returned."""

    status: Literal["success"]
    payload: Any
    truncated: NotRequired[bool]
    complete_result_location: NotRequired[str | None]
    # Images, audio or documents the tool returned: ADK sends them to the model as
    # media parts, so they don't count toward the size limit.
    media: NotRequired[list[types.Part]]


class ToolFailed(TypedDict):
    """A guarded tool's answer when the tool raised."""

    status: Literal["failed"]
    reason: str
    suggested_fixes: list[str]


class ToolFailure(Exception):
    """
    Raised by a tool to fail with its own reason and fixes.

    :param reason: What went wrong, as the model reads it.
    :param suggested_fixes: How the model can call the tool again, or what to do
        instead.
    """

    def __init__(self, reason: str, suggested_fixes: Sequence[str] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.suggested_fixes = list(suggested_fixes)


class ToolTimeout(ToolFailure):
    """
    The failure of a call that ran past its timeout, as :meth:`ForgeBaseToolset.suggest_fixes`
    sees it.

    :param seconds: The timeout it ran past.
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(
            f"The tool didn't finish within {seconds:g} seconds.",
            [
                "Call it again with a narrower request, such as fewer items or a smaller scope.",
                "If the call changes something, check whether it took effect before calling "
                "it again.",
            ],
        )
        self.seconds = seconds


class ForgeBaseToolset(BaseToolset):
    """
    An ADK toolset whose tools never raise and answer in a fixed shape, with
    oversized payloads cut down and kept whole in a file.

    Guarding a tool keeps its type, name, declaration and request processing:
    only its answers change. Listing the tools never raises either: a toolset
    whose ``get_raw_tools`` fails gives the agent no tools, instead of ADK
    dropping it.

    :param max_result_chars: The most characters of JSON a payload may take;
        a longer one is cut down to fit.
    :param result_dir: Where complete payloads are written. Defaults to
        ``DEFAULT_RESULT_DIR``; created, private to its owner, if missing.
    :param timeout_seconds: How long a call may take before it fails as a
        :class:`ToolTimeout`; None, the default, for no limit.
    :param tool_filter: ADK's filter: the names of the tools to expose, or a
        ``ToolPredicate``.
    :param tool_name_prefix: ADK's prefix, applied to every tool's name.
    :param kwargs: The next toolset's arguments, for a class that guards
        another ADK toolset by inheriting from both
        (``class Guarded(ForgeBaseToolset, SomeAdkToolset)``).
    """

    default_suggested_fixes: Sequence[str] = ()
    """What to suggest for a failure nothing more specific covers, and when
    ``suggest_fixes`` returns None."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "get_tools" in cls.__dict__:
            raise TypeError(
                f"{cls.__qualname__} overrides get_tools, which guards its tools; "
                "list them in get_raw_tools instead"
            )

    def __init__(
        self,
        *,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
        result_dir: str | Path | None = None,
        timeout_seconds: float | None = None,
        tool_filter: ToolPredicate | list[str] | None = None,
        tool_name_prefix: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(tool_filter=tool_filter, tool_name_prefix=tool_name_prefix, **kwargs)
        if max_result_chars < 1:
            raise ValueError("max_result_chars must be positive")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive, or None for no limit")
        self.max_result_chars = max_result_chars
        self.result_dir = Path(result_dir) if result_dir is not None else DEFAULT_RESULT_DIR
        self.timeout_seconds = timeout_seconds
        self._result_files: set[Path] = set()

    @abstractmethod
    async def get_raw_tools(
        self, readonly_context: ReadonlyContext | None = None
    ) -> list[BaseTool]:
        """
        The toolset's tools as they are: ``get_tools`` applies ``tool_filter``
        and guards them.

        :param readonly_context: The invocation's context; None to list every
            tool.
        """

    @final
    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        try:
            tools = await self.get_raw_tools(readonly_context)
            return [
                self._guard(tool)
                for tool in tools
                if self._is_tool_selected(tool, readonly_context)
            ]
        except Exception:
            logger.exception(
                "%s couldn't list its tools; the agent runs without them",
                type(self).__name__,
            )
            return []

    def suggest_fixes(
        self, tool: BaseTool, args: dict[str, Any], error: Exception
    ) -> Sequence[str]:
        """
        The fixes a failed call suggests to the model. Override it to suggest
        fixes for errors a tool can't anticipate, and fall back to this.

        :param tool: The tool, under its own name (without ``tool_name_prefix``).
        :param args: The arguments the model called it with.
        :param error: What it raised.
        :return: A :class:`ToolFailure`'s own fixes, else
            ``default_suggested_fixes``. None means ``default_suggested_fixes``
            too, so an override that forgets to fall back still suggests them.
        """
        if isinstance(error, ToolFailure) and error.suggested_fixes:
            return error.suggested_fixes
        return self.default_suggested_fixes

    def to_payload(self, tool: BaseTool, result: Any) -> Any:
        """
        What a tool returned, as its answer's payload. Override it for tools
        that return their failures instead of raising them: raise
        :class:`ToolFailure` to answer failed. Anything else it raises answers
        that the tool ran but its result couldn't be returned. It isn't called
        while a call waits on the user to confirm it or sign in.

        :param tool: The tool, under its own name (without ``tool_name_prefix``).
        :param result: What it returned.
        :return: ``result`` by default.
        """
        return result

    def timeout_for(self, tool: BaseTool) -> float | None:
        """
        How long a call to ``tool`` may take before it fails as a
        :class:`ToolTimeout`. Override it to give slow tools longer.

        :param tool: The tool, under its own name (without ``tool_name_prefix``).
        :return: Seconds, or None for no limit; ``timeout_seconds`` by default.
        """
        return self.timeout_seconds

    async def close(self) -> None:
        """Deletes the complete payloads the toolset wrote, then closes the next
        toolset. An override calls it."""
        for path in self._result_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Couldn't delete the tool result %s", path, exc_info=True)
        self._result_files.clear()
        await super().close()

    def _guard(self, tool: BaseTool) -> BaseTool:
        if getattr(tool, _GUARDED, False):
            return tool
        run: RunAsync = tool.run_async
        detect: Callable[[Any], str | None] | None = getattr(
            tool, "_detect_error_in_response", None
        )

        async def run_async(*, args: dict[str, Any], tool_context: ToolContext) -> Any:
            return await self._run(tool, run, args, tool_context)

        def detect_error_in_response(response: Any) -> str | None:
            # ADK's telemetry hook: a failed answer is an error, and a payload is
            # one if the tool says so.
            if not isinstance(response, dict):
                return None
            if response.get("status") == "failed":
                return "TOOL_ERROR"
            return detect(response.get("payload")) if detect is not None else None

        # A shallow copy with its own run_async, as ADK copies a tool to prefix its
        # name: isinstance checks, the declaration and process_llm_request all
        # still see the tool itself.
        guarded = copy.copy(tool)
        guarded.run_async = run_async  # type: ignore[method-assign]
        guarded._detect_error_in_response = detect_error_in_response  # type: ignore[attr-defined]
        setattr(guarded, _GUARDED, True)
        return guarded

    async def _run(
        self,
        tool: BaseTool,
        run: RunAsync,
        args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Any:
        seconds: float | None = None
        deadline: asyncio.Timeout | None = None
        try:
            seconds = self.timeout_for(tool)
            deadline = asyncio.timeout(seconds)
            async with deadline:
                result = await run(args=args, tool_context=tool_context)
        except Exception as error:
            # Only the deadline's own expiry is a timeout: a TimeoutError the tool
            # raises itself is its failure like any other.
            if deadline is not None and deadline.expired() and seconds is not None:
                return self._failed(tool, args, ToolTimeout(seconds))
            return self._failed(tool, args, error)
        try:
            if (tool.is_long_running or getattr(tool, "_defers_response", False)) and not result:
                # ADK answers the call later itself when such a tool returns nothing.
                return result
            if isinstance(result, types.FunctionResponse):
                # A streaming tool's chunk that names its own scheduling: ADK reads
                # the chunk's payload from it.
                answer = await self._answer(tool, args, result.response, tool_context)
                return result.model_copy(update={"response": answer})
            return await self._answer(tool, args, result, tool_context)
        except Exception as error:
            logger.exception("Couldn't shape the result of the tool %s", tool.name)
            return ToolFailed(
                status="failed",
                reason=f"The tool ran, but its result couldn't be returned: {_describe(error)}",
                suggested_fixes=[
                    "Don't repeat a call that changes something before checking "
                    "whether it took effect."
                ],
            )

    def _failed(self, tool: BaseTool, args: dict[str, Any], error: Exception) -> ToolFailed:
        """
        Handles tool failure by logging details, determining the failure reason, and suggesting fixes
        if possible. It attempts to analyze the provided error and returns a ToolFailed instance with
        its status, reason, and possible fixes. This includes catching and managing internal exceptions
        raised during failure handling or fix suggestion.

        :param tool: The tool instance that encountered the failure.
        :type tool: BaseTool
        :param args: A dictionary of arguments used for the tool execution.
        :type args: dict[str, Any]
        :param error: The exception that was raised during the tool execution or processing.
        :type error: Exception
        :return: An object containing the failure status, reason for failure, and suggested fixes if any.
        :rtype: ToolFailed
        """
        try:
            if isinstance(error, ToolFailure):
                reason = error.reason
                level = logging.WARNING if isinstance(error, ToolTimeout) else logging.INFO
                logger.log(level, "The tool %s failed: %s", tool.name, reason)
            else:
                reason = _describe(error)
                logger.warning("The tool %s raised", tool.name, exc_info=error)
            try:
                fixes = self.suggest_fixes(tool, args, error)
            except Exception:
                logger.exception("%s.suggest_fixes raised", type(self).__name__)
                fixes = None
            if fixes is None:
                fixes = self.default_suggested_fixes
            return ToolFailed(status="failed", reason=str(reason), suggested_fixes=_as_fixes(fixes))
        except Exception:
            logger.exception("Couldn't describe the failure of the tool %s", tool.name)
            return ToolFailed(status="failed", reason=_describe(error), suggested_fixes=[])

    async def _answer(
        self, tool: BaseTool, args: dict[str, Any], result: Any, tool_context: ToolContext
    ) -> ToolSuccess | ToolFailed:
        if not _asks_the_user(tool_context):
            try:
                result = self.to_payload(tool, result)
            except ToolFailure as failure:
                return self._failed(tool, args, failure)
        return await self._succeeded(tool, result)

    async def _succeeded(self, tool: BaseTool, result: Any) -> ToolSuccess:
        kept, payload, media = _split_media(result)
        answer = ToolSuccess(status="success", payload=payload if kept else None)
        text = _to_json(answer["payload"])
        if len(text) > self.max_result_chars:
            complete = payload if isinstance(payload, str) else json.loads(text)
            answer["payload"] = _shrink(complete, self.max_result_chars)
            answer["truncated"] = True
            answer["complete_result_location"] = await self._write(tool.name, complete)
        if media:
            answer["media"] = media
        return answer

    async def _write(self, tool_name: str, complete: Any) -> str | None:
        try:
            path = await asyncio.to_thread(_write_result, self.result_dir, tool_name, complete)
        except Exception:
            logger.exception(
                "Couldn't write the complete result of %s to %s", tool_name, self.result_dir
            )
            return None
        self._result_files.add(path)
        return str(path)


def _asks_the_user(tool_context: Any) -> bool:
    """Whether the call is waiting on the user: to confirm it, or to sign in."""
    actions = getattr(tool_context, "actions", None)
    return bool(
        getattr(actions, "requested_tool_confirmations", None)
        or getattr(actions, "requested_auth_configs", None)
    )


def _as_fixes(fixes: Any) -> list[str]:
    """Suggested fixes as a list of strings, whatever a subclass gave: a lone
    string is one fix, None or something that isn't a list is none."""
    if fixes is None:
        return []
    if isinstance(fixes, str):
        return [fixes]
    try:
        return [str(fix) for fix in fixes]
    except Exception:
        logger.warning("Ignoring suggested fixes that aren't a list: %s", type(fixes).__name__)
        return []


def _describe(error: BaseException) -> str:
    try:
        message = str(error)
    except Exception:
        message = ""
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _to_json(value: Any) -> str:
    """
    The JSON the model is sent for ``value``, as pydantic, which ADK sends it
    with, writes it: bytes in base64, what JSON has no form for as its string.
    """
    try:
        return pydantic_core.to_json(value, bytes_mode="base64", fallback=str).decode()
    except (ValueError, RecursionError):
        # A value that contains itself: all there is to show is its repr.
        return pydantic_core.to_json(repr(value)).decode()


def _shrink(value: Any, limit: int) -> Any:
    """
    JSON data cut down until its JSON fits in ``limit`` characters: the most
    entries of each list and object, and the most of each string, that fit.
    """
    best = _cut(value, 0)
    low, high = 1, limit
    # What each list, object and string keeps grows with the cap, so the largest
    # cap that fits is found by bisection.
    while low <= high:
        cap = (low + high) // 2
        cut = _cut(value, cap)
        if len(_to_json(cut)) <= limit:
            best, low = cut, cap + 1
        else:
            high = cap - 1
    return best


# A string keeps this many characters for every entry a list or object keeps: a
# few whole records read better than many clipped ones.
_CHARS_PER_ENTRY = 10


def _cut(value: Any, cap: int) -> Any:
    """JSON data with each list's and object's first ``cap`` entries and each
    string's first ``cap * _CHARS_PER_ENTRY`` characters, saying what it left out."""
    if isinstance(value, str):
        chars = cap * _CHARS_PER_ENTRY
        if len(value) <= chars:
            return value
        return f"{value[:chars]}… [{len(value) - chars} more characters]"
    if isinstance(value, list):
        items = [_cut(item, cap) for item in value[:cap]]
        if len(value) > cap:
            items.append(f"… [{len(value) - cap} more items]")
        return items
    if isinstance(value, dict):
        entries = list(value.items())
        kept = {key: _cut(item, cap) for key, item in entries[:cap]}
        if len(entries) > cap:
            kept["…"] = f"[{len(entries) - cap} more keys]"
        return kept
    return value


def _split_media(value: Any, depth: int = 0) -> tuple[bool, Any, list[types.Part]]:
    """
    ``value`` without the media parts ADK sends the model as media: whether
    anything is left of it, what is, and the parts. It looks as deep as ADK
    does; inside an answer, the parts would sit a level deeper than ADK looks.
    """
    if _is_media(value):
        return False, None, [value]
    if depth >= _MEDIA_DEPTH or not isinstance(value, (dict, list, tuple)):
        return True, value, []
    media: list[types.Part] = []
    rest: dict[Any, Any] | list[Any]
    if isinstance(value, dict):
        rest = {}
        for key, item in value.items():
            kept, left, found = _split_media(item, depth + 1)
            media.extend(found)
            if kept:
                rest[key] = left
    else:
        rest = []
        for item in value:
            kept, left, found = _split_media(item, depth + 1)
            media.extend(found)
            if kept:
                rest.append(left)
    if not media:
        return True, value, []
    return bool(rest), rest, media


def _is_media(value: Any) -> bool:
    """Whether ADK sends ``value`` as a media part rather than as JSON."""
    if not isinstance(value, types.Part):
        return False
    blob, file = value.inline_data, value.file_data
    return bool(blob is not None and blob.data is not None and blob.mime_type) or bool(
        file is not None and file.file_uri and file.mime_type
    )


def _write_result(directory: Path, tool_name: str, complete: Any) -> Path:
    """Writes a complete payload to a new file of its own: text as it is, anything
    else as indented JSON, so file tools can page through it by line."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = isinstance(complete, str)
    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", tool_name)[:64] + "-"
    descriptor, name = tempfile.mkstemp(
        prefix=prefix, suffix=".txt" if text else ".json", dir=directory
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        if text:
            file.write(complete)
        else:
            json.dump(complete, file, ensure_ascii=False, indent=2)
    return Path(name)
