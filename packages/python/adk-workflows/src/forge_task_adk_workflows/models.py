"""
The models LLM nodes run on: the shared model provider configuration's
(model_provider.yaml, which the admin API's assistant reads too), else
Gemini's, as the one ADK model ``ProviderModels`` that runs each request on
the model and thinking level its node picks
(``graph.services.model_selection``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.graph import RunFailed
from forge_tasks.env_files import environment

if TYPE_CHECKING:
    from forge_common.adk.models import ProviderModels
    from forge_common.model_provider import ModelProviderConfig

#: Why LLM nodes can't run, when no models are set up.
NOT_SET_UP = (
    "LLM nodes can't run on this worker: set HYBRID_ADK_WORKFLOWS__MODEL_PROVIDER_CONFIG "
    "(the shared model_provider.yaml), or HYBRID_ADK_WORKFLOWS__GOOGLE_API_KEY "
    "(FORGE_GOOGLE_API_KEY)"
)
#: Without a model provider configuration: the Gemini API, and its model when
#: none is set (the admin API's assistant has the same, agents/language_models.py).
GEMINI_PROVIDER = "google"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
#: Statuses a model's API answers that are worth trying again.
RETRYABLE = frozenset({408, 429})
#: What a provider's client raises when it didn't get an answer (LiteLLM's, by name).
UNANSWERED = frozenset(
    {"APIConnectionError", "Timeout", "ServiceUnavailableError", "InternalServerError", "RateLimitError"}
)


def load_models(settings: AdkWorkflowsSettings) -> ProviderModels | None:
    """
    The models LLM nodes run on: the model provider configuration's, when
    ``model_provider_config`` names one (its ``${NAME}`` references resolved
    from the process environment, then ``.env``); else Gemini's, with
    ``google_api_key``; else none.

    :raises ModelProviderConfigError: The configuration can't be read, isn't
        valid, or doesn't declare ``default_model``.
    :raises SettingsError: ``.env`` references a name defined nowhere.
    """
    from forge_common.adk.models import ProviderModels
    from forge_common.model_provider import load_model_provider_config

    if settings.model_provider_config:
        config = load_model_provider_config(settings.model_provider_config, environment())
        return ProviderModels(config, default=settings.default_model or None)
    if settings.google_api_key is not None:
        key = settings.google_api_key.get_secret_value()
        return ProviderModels(gemini_config(key, settings.default_model))
    return None


def gemini_config(api_key: str, model: str | None = None) -> ModelProviderConfig:
    """
    The Gemini API as a model provider configuration: ``model`` (by default
    gemini-3.5-flash), which reasons and reads text and images.
    """
    from forge_common.model_provider import ModelProviderConfig

    model = model or DEFAULT_GEMINI_MODEL
    return ModelProviderConfig.model_validate(
        {
            "version": 1,
            "default": {"provider": GEMINI_PROVIDER, "model": model},
            "providers": {
                GEMINI_PROVIDER: {
                    "name": "Google Gemini",
                    "baseUrl": GEMINI_BASE_URL,
                    "api": "google-generative-ai",
                    "auth": {"type": "apiKey", "key": api_key},
                    "models": [
                        {
                            "id": model,
                            "name": " ".join(w[:1].upper() + w[1:] for w in model.split("-")),
                            "reasoning": True,
                            "input": ["text", "image"],
                        }
                    ],
                }
            },
        }
    )


class NoModels(BaseLlm):
    """
    The model LLM nodes run on when the worker has none: each request fails
    the run, saying why (:class:`RunFailed`), so runs without LLM nodes still
    run.
    """

    model: str = "unavailable"
    reason: str = NOT_SET_UP

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        raise RunFailed(self.reason)
        yield  # an async generator, as ADK calls it
