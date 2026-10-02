"""The models the assistant runs on, and those a conversation may choose.

With ``model_provider_config`` they are the shared model_provider.yaml's
(``forge_common.model_provider``, the file the ADK workflows task reads too),
limited to ``agent_models`` when it names some. Without, they are Gemini
models, as ``google_api_key``, ``agent_model`` and ``agent_models`` set the
assistant up before there was a shared file: the same configuration, built
here. Either way the agent runs on one :class:`ProviderModels`, which sends
each turn to the model the conversation chose (``forge.model_selection``);
``GET /agents/apps/{app}/models`` lists them for the web console's model
section (:func:`describe`).
"""

from collections.abc import Mapping
from typing import Any

from forge_common.adk.models import LlmFactory, ProviderModels
from forge_common.model_provider import (
    ModelProviderConfig,
    ResolvedModel,
    load_model_provider_config,
    supported_thinking_levels,
)

from forge_admin import env_files
from forge_admin.config import Settings

# Without a model provider configuration: the Gemini API, and its model when
# agent_model is unset.
GEMINI_PROVIDER = "google"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
# A key the configuration can hold when google_api_key is unset. The runtime
# refuses runs then (runtime.NO_API_KEY), so it never reaches Google.
_NO_KEY = "unset"


def provider_models(
    settings: Settings,
    *,
    environment: Mapping[str, str] | None = None,
    build: LlmFactory | None = None,
) -> ProviderModels:
    """
    :param settings: The application settings.
    :param environment: What the configuration's ``${NAME}`` references
        resolve from; the process environment over .env by default.
    :param build: Makes the ADK model each turn runs on; tests pass a
        scripted one.
    :return: The assistant's models.
    :raises ModelProviderConfigError: The configuration can't be read, isn't
        valid, or doesn't declare agent_model or one of agent_models.
    """
    if settings.model_provider_config is None:
        return ProviderModels(gemini_config(settings), build=build)
    config = load_model_provider_config(
        settings.model_provider_config,
        env_files.environment() if environment is None else environment,
    )
    return ProviderModels(
        config,
        default=settings.agent_model,
        models=settings.agent_models or None,
        build=build,
    )


def gemini_config(settings: Settings) -> ModelProviderConfig:
    """
    The Gemini models ``agent_model`` and ``agent_models`` name, as a model
    provider configuration: each reasons, reads text and images, and is
    named after its id.
    """
    default = settings.agent_model or DEFAULT_GEMINI_MODEL
    key = settings.google_api_key
    ids = dict.fromkeys([default, *settings.agent_models])
    return ModelProviderConfig.model_validate(
        {
            "version": 1,
            "default": {"provider": GEMINI_PROVIDER, "model": default},
            "providers": {
                GEMINI_PROVIDER: {
                    "name": "Google Gemini",
                    "baseUrl": GEMINI_BASE_URL,
                    "api": "google-generative-ai",
                    "auth": {
                        "type": "apiKey",
                        "key": key.get_secret_value() if key else _NO_KEY,
                    },
                    "models": [
                        {
                            "id": model,
                            "name": _display_name(model),
                            "reasoning": True,
                            "input": ["text", "image"],
                        }
                        for model in ids
                    ],
                }
            },
        }
    )


def _display_name(model: str) -> str:
    # gemini-2.5-flash-lite: Gemini 2.5 Flash Lite.
    """
    Constructs and returns a display-friendly name by capitalizing the first
    letter of each word in the given model string, which is expected to be
    separated by hyphens.

    :param model: A string representing the model name, where words are
        separated by hyphens.
    :type model: str
    :return: A string with each word's first letter capitalized and hyphens
        replaced by spaces.
    :rtype: str
    """
    return " ".join(word[:1].upper() + word[1:] for word in model.split("-"))


def accepted_names(models: ProviderModels) -> set[str]:
    """
    Extracts and returns a set of accepted names from the provided model registry.

    The function iterates over the available models and collects names that match
    certain conditions, ensuring that only those which have corresponding entries
    in the `models` registry are included in the returned set.

    :param models: An instance of ProviderModels that contains a collection of
        available models and provides methods for model lookup.
    :return: A set containing unique names derived from the `ref` and `id`
        attributes of the available models that pass the filtering criteria.
    """
    return {
        name
        for model in models.available
        for name in (model.ref, model.model.id)
        if models.find(name) == model
    }


def describe(model: ResolvedModel) -> dict[str, Any]:
    """
    Extracts and structures detailed information from a given `ResolvedModel` object.

    Returns a dictionary containing various attributes about the model, including its
    identifier, provider information, display name, API reference, reasoning capabilities,
    and supported input types. Additionally, it includes information about the model's
    context window, maximum tokens, and supported thinking levels.

    :param model: A `ResolvedModel` instance representing the model whose details need to
        be extracted.
    :type model: ResolvedModel
    :return: A dictionary containing the model's extracted details such as
        'id', 'provider', 'provider_name', 'model', 'name', 'api', 'reasoning',
        'input', 'context_window', 'max_tokens', and 'thinking_levels'.
    :rtype: dict[str, Any]
    """
    return {
        "id": model.ref,
        "provider": model.provider_id,
        "provider_name": model.provider_name,
        "model": model.model.id,
        "name": model.model.display_name,
        "api": model.api,
        "reasoning": model.model.reasoning,
        "input": list(model.model.input),
        "context_window": model.model.context_window,
        "max_tokens": model.model.max_tokens,
        "thinking_levels": list(supported_thinking_levels(model.model)),
    }
