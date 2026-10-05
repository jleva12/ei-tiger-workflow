"""What ADK agents use: every model call (its model, tokens and price) and
every tool call, handed to a sink. Needs ``forge-common[adk]``.

:class:`UsagePlugin` goes on an ADK ``App`` (``App(..., plugins=[...])``). For
each model response that ends a call it hands its sink a :class:`UsageCall`:
who the call was for (an :class:`Attribution`: the organization, and the
workflow, agent or assistant), the ADK agent that made it, the model, its
tokens and what they cost; and one for each tool call, failed or not::

    usage = UsagePlugin(store.record, Attribution("org_1", "agent", "ca_x", "Support"))
    app = App(name="ca_x", root_agent=root, plugins=[usage])

An app shared by everyone (the assistant) leaves the attribution out, and each
turn names its own with :func:`attributed`; a call with neither is not
recorded. A process can also say once where usage goes (:func:`recording`),
for code that builds its runners further down (:func:`current_sink`).

The model is the ``provider/model`` the request ran on: ``ProviderModels``
stamps it on each response (``custom_metadata["forge_model"]``); otherwise it's
the version the provider reported. ``price`` finds that model's price per
million tokens (model_provider.yaml's ``cost``); one that's unknown, or all
zero, leaves the call unpriced rather than free.

Tokens are counted the same whichever API answered: ``input`` is the whole
prompt (``cached`` of it read back from the provider's cache), ``output`` the
whole reply (``thinking`` of it the model's reasoning): Gemini counts its
thoughts beside the reply, LiteLLM inside it.

Recording never fails a run: a sink that raises is logged and the call is
lost.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from google.adk.plugins.base_plugin import BasePlugin

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext
    from google.genai import types

    from forge_common.model_provider import Cost

log = logging.getLogger(__name__)

#: What ``ProviderModels`` stamps on each response: the ``provider/model`` it ran on.
MODEL_KEY = "forge_model"

Kind = Literal["workflow", "agent", "assistant"]


@dataclass(frozen=True)
class Attribution:
    """
    Who an agent's calls are for.

    :ivar organization_id: The organization that pays for them.
    :ivar kind: ``workflow`` (a workflow run), ``agent`` (a chat agent) or
        ``assistant``.
    :ivar subject_id: The workflow's or agent's ID (``assistant`` for it).
    :ivar subject_name: Its name then.
    :ivar run_id: The workflow run; for the others, the session is used.
    :ivar user_id: Who the calls are for; the invocation's user otherwise.
    """

    organization_id: str
    kind: Kind
    subject_id: str
    subject_name: str = ""
    run_id: str | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class UsageCall:
    """A model or tool call, as the sink gets it."""

    attribution: Attribution
    #: ``model`` or ``tool``.
    call: Literal["model", "tool"]
    #: The model (``provider/model``) or the tool.
    name: str
    #: The ADK agent that made it: a workflow's step, an agent or a sub-agent.
    author: str
    at: datetime
    invocation_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    #: The run: the attribution's, else the session.
    run_id: str | None = None
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    #: USD; None when the model has no known price (and for tools).
    cost: float | None = None
    failed: bool = False


UsageSink = Callable[[UsageCall], Awaitable[None]]
PriceOf = Callable[[str], "Cost | None"]

_sink: contextvars.ContextVar[UsageSink | None] = contextvars.ContextVar(
    "forge_usage_sink", default=None
)
_attribution: contextvars.ContextVar[Attribution | None] = contextvars.ContextVar(
    "forge_usage_attribution", default=None
)


@contextlib.contextmanager
def recording(sink: UsageSink | None) -> Iterator[None]:
    """Where usage goes while inside: :func:`current_sink` answers ``sink``."""
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def current_sink() -> UsageSink | None:
    """:return: Where usage goes here (:func:`recording`), if anywhere."""
    return _sink.get()


@contextlib.contextmanager
def attributed(attribution: Attribution | None) -> Iterator[None]:
    """Whom the calls made inside are for, when their plugin doesn't say."""
    token = _attribution.set(attribution)
    try:
        yield
    finally:
        _attribution.reset(token)


@dataclass(frozen=True)
class Tokens:
    input: int = 0
    cached: int = 0
    output: int = 0
    thinking: int = 0
    #: Of the input, written to the provider's cache (Anthropic).
    cache_write: int = 0


def tokens_of(usage: types.GenerateContentResponseUsageMetadata | None) -> Tokens:
    """A response's usage, counted the same whichever API answered."""
    if usage is None:
        return Tokens()
    prompt = usage.prompt_token_count or 0
    tool_prompt = usage.tool_use_prompt_token_count or 0
    replied = usage.candidates_token_count or 0
    thoughts = usage.thoughts_token_count or 0
    total = usage.total_token_count or 0
    # Gemini's total adds its thoughts to the reply; LiteLLM's reply has them in it.
    output = (
        replied + thoughts
        if thoughts and total >= prompt + tool_prompt + replied + thoughts
        else replied
    )
    whole = prompt + tool_prompt
    write = getattr(usage, "cache_creation_input_tokens", None)
    return Tokens(
        input=whole,
        cached=min(usage.cached_content_token_count or 0, whole),
        output=output,
        thinking=min(thoughts, output),
        cache_write=min(write, whole) if isinstance(write, int) and write > 0 else 0,
    )


def cost_of(tokens: Tokens, price: Cost | None) -> float | None:
    """
    :return: What the tokens cost in USD at a price per million; None when
        there's no price (or it's all zero: none was set).
    """
    if price is None or not any((price.input, price.output, price.cache_read, price.cache_write)):
        return None
    write = tokens.cache_write
    fresh = max(0, tokens.input - tokens.cached - write)
    # A cache write the price leaves out costs what input does (OpenAI's).
    write_price = price.cache_write or price.input
    return (
        fresh * price.input
        + tokens.cached * price.cache_read
        + write * write_price
        + tokens.output * price.output
    ) / 1_000_000


class UsagePlugin(BasePlugin):
    """
    Hands every model and tool call to ``sink``.

    :param sink: Where calls go, e.g. a store's insert.
    :param attribution: Whom they're for; :func:`attributed` otherwise.
    :param price: A model's price per million tokens, by ``provider/model``.
    """

    def __init__(
        self,
        sink: UsageSink,
        attribution: Attribution | None = None,
        *,
        price: PriceOf | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(name="forge_usage")
        self.sink = sink
        self.attribution = attribution
        self.price = price
        self.clock = clock

    # ------------------------------------------------------------------ models

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        # Streamed fragments carry running counts; the call's last response has them all.
        if llm_response.partial:
            return None
        failed = bool(llm_response.error_code)
        if llm_response.usage_metadata is None and not failed:
            return None
        tokens = tokens_of(llm_response.usage_metadata)
        model = _model_of(llm_response)
        await self._record(
            callback_context,
            call="model",
            name=model,
            input_tokens=tokens.input,
            cached_tokens=tokens.cached,
            output_tokens=tokens.output,
            thinking_tokens=tokens.thinking,
            cost=cost_of(tokens, self._price(model)),
            failed=failed,
        )
        return None

    async def on_model_error_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest, error: Exception
    ) -> LlmResponse | None:
        await self._record(
            callback_context, call="model", name=llm_request.model or "unknown", failed=True
        )
        return None

    # ------------------------------------------------------------------- tools

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        failed = _tool_failed(result)
        await self._record(tool_context, call="tool", name=tool.name, failed=failed)
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        await self._record(tool_context, call="tool", name=tool.name, failed=True)
        return None

    # --------------------------------------------------------------- recording

    def _price(self, model: str) -> Cost | None:
        if self.price is None:
            return None
        try:
            return self.price(model)
        except Exception:
            log.warning("usage: the price of %s couldn't be found", model, exc_info=True)
            return None

    async def _record(self, context: CallbackContext, **fields: Any) -> None:
        attribution = self.attribution or _attribution.get()
        if attribution is None:
            return
        try:
            session_id = context.session.id
        except Exception:
            session_id = None
        user_id = attribution.user_id or context.user_id
        call = UsageCall(
            attribution=attribution,
            author=context.agent_name or "",
            at=self.clock(),
            invocation_id=context.invocation_id,
            session_id=session_id,
            user_id=user_id,
            run_id=attribution.run_id or session_id,
            **fields,
        )
        try:
            await self.sink(call)
        except Exception:
            log.warning(
                "usage: a %s call of %s %s wasn't recorded",
                call.call,
                attribution.kind,
                attribution.subject_id,
                exc_info=True,
            )


def _tool_failed(result: object) -> bool:
    """Whether a tool answered with a failure: MCP's ``isError``, or an ``error`` and no result."""
    if not isinstance(result, dict):
        return False
    return result.get("isError") is True or ("error" in result and "result" not in result)


def _model_of(response: LlmResponse) -> str:
    stamped = (response.custom_metadata or {}).get(MODEL_KEY)
    if isinstance(stamped, str) and stamped:
        return stamped
    return response.model_version or "unknown"


def price_from(models: Any) -> PriceOf | None:
    """
    :param models: The ``ProviderModels`` agents run on (anything else: none).
    :return: A model's price per million tokens, as its configuration sets it.
    """
    config = getattr(models, "config", None)
    find = getattr(config, "find", None)
    if find is None:
        return None

    def price(model: str) -> Cost | None:
        found = find(model)
        return found.model.cost if found is not None else None

    return price
