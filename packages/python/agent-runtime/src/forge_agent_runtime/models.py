"""The model each agent runs on, and how long it thinks.

An agent's settings pick a model (``{provider, name}``) and a thinking level;
the chat may pick others for the conversation (the ``model`` and
``thinking_level`` state its model picker sends), and the chat's choice wins.
A model the runtime doesn't offer runs on the default instead; a level the
model doesn't offer, on the nearest it does (``ProviderModels.select``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from forge_common.adk.models import ProviderModels

log = logging.getLogger(__name__)


def model_id(config: Mapping[str, Any]) -> str:
    """
    :param config: An LLM agent's settings, whose ``model`` is ``{provider, name}``.
    :return: ``provider/name``, the name alone without a provider, or ``""``
        for the default.
    """
    model = config.get("model")
    if isinstance(model, str):
        return model.strip()
    if not isinstance(model, dict):
        return ""
    provider = str(model.get("provider") or "").strip()
    name = str(model.get("name") or "").strip()
    if not name:
        return ""
    return f"{provider}/{name}" if provider else name


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def model_selection(models: ProviderModels, config: Mapping[str, Any], *, timeout: float | None = None):
    """
    :param models: The models agents run on.
    :param config: The agent's settings.
    :param timeout: Seconds one model call may take; None leaves the provider's.
    :return: The agent's ``before_model_callback``.
    """
    chosen = model_id(config)
    level = _text(config.get("thinking_level"))
    if chosen and models.find(chosen) is None:
        log.warning(
            "%s isn't one of the runtime's models; the default, %s, answers", chosen, models.default.ref
        )

    def select(callback_context: CallbackContext, llm_request: LlmRequest) -> None:
        state = callback_context.state
        models.select(
            llm_request,
            _text(state.get("model")) or chosen or None,
            _text(state.get("thinking_level")) or level,
        )
        if timeout is not None:
            options = llm_request.config.http_options or types.HttpOptions()
            if options.timeout is None:
                options.timeout = int(timeout * 1000)
            llm_request.config.http_options = options

    return select
