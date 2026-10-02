"""model_provider.yaml: the model providers and models an app may use.

Needs the ``model-provider`` extra: ``forge-common[model-provider]``. The shared
files are in this package's directory (:data:`SHARED_CONFIG_DIR`), where the
admin API's assistant and ADK workflows' LLM nodes read them;
``forge_common.adk.models`` runs Google ADK agents on their models.
"""

from forge_common.model_provider.config import (
    SHARED_CONFIG_DIR,
    ApiKeyAuth,
    ConfiguredModel,
    Cost,
    DefaultModel,
    ModelApi,
    ModelProviderAuthError,
    ModelProviderConfig,
    ModelProviderConfigError,
    OAuth2Auth,
    Provider,
    ResolvedModel,
    ThinkingLevelMap,
    default_model_provider_config_path,
    load_model_provider_config,
    parse_model_provider_yaml,
    resolve_model_provider_environment,
)
from forge_common.model_provider.oauth import OAuth2TokenSource
from forge_common.model_provider.thinking import (
    THINKING_LEVELS,
    ThinkingLevel,
    clamp_thinking_level,
    supported_thinking_levels,
    thinking_value,
)

__all__ = [
    "SHARED_CONFIG_DIR",
    "THINKING_LEVELS",
    "ApiKeyAuth",
    "ConfiguredModel",
    "Cost",
    "DefaultModel",
    "ModelApi",
    "ModelProviderAuthError",
    "ModelProviderConfig",
    "ModelProviderConfigError",
    "OAuth2Auth",
    "OAuth2TokenSource",
    "Provider",
    "ResolvedModel",
    "ThinkingLevel",
    "ThinkingLevelMap",
    "clamp_thinking_level",
    "default_model_provider_config_path",
    "load_model_provider_config",
    "parse_model_provider_yaml",
    "resolve_model_provider_environment",
    "supported_thinking_levels",
    "thinking_value",
]
