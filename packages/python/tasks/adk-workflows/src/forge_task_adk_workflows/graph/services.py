"""
What a run's nodes use from outside the document, made once per run and
handed to every factory: tests put in a fake HTTP transport, a clock they
move and a scripted model.

LLM nodes run on the worker's models (``model``: the shared
``forge_common.adk.models.ProviderModels``), and each request on the model and
thinking level its node's settings pick (``model_callbacks``:
:func:`model_selection`).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self

import httpx
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.llm_agent import BeforeModelCallback
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_workflows.expressions import Evaluator

if TYPE_CHECKING:
    from forge_common.adk.models import ProviderModels

log = logging.getLogger(__name__)

#: Finds another of the organization's agents by ID: its document, or None
#: when there's no such agent.
Resolve = Callable[[str], dict[str, Any] | None]
#: Makes an LLM agent's ``before_model_callback`` from its settings (its
#: ``model`` and ``thinking_level`` among them), or None for none.
ModelCallbacks = Callable[[dict[str, Any]], BeforeModelCallback | None]

# The worker's defaults (forge_task_adk_workflows.config.AdkWorkflowsSettings).
_DEFAULTS = AdkWorkflowsSettings()


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RunServices:
    """
    What a run's nodes use: HTTP, time, expressions, where HTTP may go, the
    limits the worker keeps, the model LLM agents run on, and how saved agents
    are found.
    """

    #: HTTP requests' client; None makes one per request.
    http: httpx.AsyncClient | None = None
    #: The time now, and waiting a while (a short delay, an HTTP retry's
    #: back-off).
    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    #: JSONata, as the worker evaluates it.
    evaluator: Evaluator = field(default_factory=Evaluator)
    #: HTTP requests never reach private, loopback or link-local addresses
    #: unless allowed; hosts listed are always allowed
    #: (``forge_task_workflows.services.http_guard.check_url``).
    allow_private: bool = _DEFAULTS.http_allow_private
    allowed_hosts: tuple[str, ...] = ()
    #: The most of a response an HTTP request reads.
    max_response_bytes: int = _DEFAULTS.http_max_response_bytes
    #: A delay this long or shorter waits in place; a longer one pauses the
    #: run until the timer wakes it.
    inline_delay_seconds: float = _DEFAULTS.inline_delay_seconds
    #: The most items a loop goes through, whatever it allows.
    max_loop_items: int = _DEFAULTS.max_loop_items
    #: The model every LLM agent runs on when given (else its own setting's,
    #: else the default), and its ``before_model_callback`` from its
    #: settings.
    model: str | BaseLlm | None = None
    model_callbacks: ModelCallbacks | None = None
    #: Finds the organization's other agents, for saved-agent nodes.
    resolve: Resolve | None = None

    @classmethod
    def of(cls, settings: AdkWorkflowsSettings, **services: Any) -> Self:
        """
        :param settings: The ADK workflows task's settings: where HTTP may go,
            its limits.
        :param services: Any of the others (``http``, ``clock``, ``model``…).
            A ``model`` that's the shared ``ProviderModels`` also chooses each
            LLM node's model and thinking level (:func:`model_selection`),
            unless ``model_callbacks`` are given.
        :return: The services, with the task's limits.
        """
        from forge_common.adk.models import ProviderModels

        model = services.get("model")
        if isinstance(model, ProviderModels) and "model_callbacks" not in services:
            services["model_callbacks"] = model_selection(model, timeout=settings.model_timeout)
        return cls(
            **{
                "evaluator": Evaluator(settings.expression_timeout_ms, settings.expression_depth),
                "allow_private": settings.http_allow_private,
                "allowed_hosts": tuple(settings.http_allowed_hosts),
                "max_response_bytes": settings.http_max_response_bytes,
                "inline_delay_seconds": settings.inline_delay_seconds,
                "max_loop_items": settings.max_loop_items,
                **services,
            }
        )

    def but(self, **changes: Any) -> Self:
        """:return: These services with some changed; None changes nothing."""
        return replace(self, **{key: value for key, value in changes.items() if value is not None})


def model_id(config: dict[str, Any]) -> str:
    """
    :param config: An LLM agent's settings, whose ``model`` is
        ``{"provider", "name"}`` as the builder's model select writes it.
    :return: The model it picks, as ``ProviderModels`` finds it:
        ``provider/model``, the name alone when it has no provider, or ``""``
        for the default.
    """
    model = config.get("model")
    if isinstance(model, str):
        return model.strip()
    if not isinstance(model, dict):
        return ""
    provider = model.get("provider")
    name = model.get("name")
    provider = provider.strip() if isinstance(provider, str) else ""
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        return ""
    return f"{provider}/{name}" if provider else name


def model_selection(models: "ProviderModels", *, timeout: float | None = None) -> ModelCallbacks:
    """
    Each LLM agent's ``before_model_callback``: its requests run on the model
    and thinking level its settings pick (``ProviderModels.select``). A model
    the worker doesn't offer runs on the default instead; a level the model
    doesn't offer, on the nearest it does; none, on the provider's default.

    :param models: The models LLM agents run on (``RunServices.model``).
    :param timeout: Seconds one model call may take (the request's HTTP
        options, which Gemini and LiteLLM both honour); None leaves theirs.
    :return: Makes the callback from an agent's settings.
    """

    def callbacks(config: dict[str, Any]) -> BeforeModelCallback:
        chosen = model_id(config)
        level = config.get("thinking_level")
        thinking = level if isinstance(level, str) and level else None
        if chosen and models.find(chosen) is None:
            log.warning(
                "adk_workflows: %s isn't one of the worker's models; the default, %s, answers instead",
                chosen,
                models.default.ref,
            )

        def select(callback_context: CallbackContext, llm_request: LlmRequest) -> None:
            models.select(llm_request, chosen, thinking)
            if timeout is not None:
                options = llm_request.config.http_options or types.HttpOptions()
                if options.timeout is None:
                    options.timeout = int(timeout * 1000)
                llm_request.config.http_options = options

        return select

    return callbacks
