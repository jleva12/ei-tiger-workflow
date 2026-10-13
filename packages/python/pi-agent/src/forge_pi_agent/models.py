"""
A model from the shared model_provider.yaml as pi's ``models.json``: one
provider (``forge-<provider id>``, so pi's own catalog never mixes in) with
the one model, where the provider's settings (base URL, API, headers) are the
model's as :class:`ResolvedModel` settles them.

model_provider.yaml already uses pi's model format (camelCase keys, its
``thinkingLevelMap`` and ``compat``), so the model's entry is its own, less
what pi takes at the provider. pi resolves ``$NAME`` and ``!command`` in
header values; ours are already resolved, so they're escaped (``$$``,
``$!``) and reach the provider as they are.
"""

from typing import Any

from forge_common.model_provider import ResolvedModel

#: What a model's provider is called in pi: ``forge-openai``.
PROVIDER_PREFIX = "forge-"


def provider_name(model: ResolvedModel) -> str:
    """:return: The name pi knows the model's provider by."""
    return PROVIDER_PREFIX + model.provider_id


def model_ref(model: ResolvedModel) -> str:
    """:return: How pi's ``--model`` names it: ``forge-openai/gpt-5.2``."""
    return f"{provider_name(model)}/{model.model.id}"


def literal(value: str) -> str:
    """:return: ``value`` as pi reads it back unchanged: ``$`` and a leading ``!`` escaped."""
    escaped = value.replace("$", "$$")
    return "$" + escaped if escaped.startswith("!") else escaped


def model_entry(model: ResolvedModel) -> dict[str, Any]:
    """:return: The model's entry in its provider's ``models``."""
    configured = model.model
    entry: dict[str, Any] = {
        "id": configured.id,
        "name": configured.display_name,
        "reasoning": configured.reasoning,
        "input": list(configured.input),
        "contextWindow": configured.context_window,
        "maxTokens": configured.max_tokens,
        "cost": configured.cost.model_dump(by_alias=True),
    }
    if configured.thinking_level_map is not None:
        # Only the levels it sets: pi reads a level left out (the provider's
        # default) apart from one set to null (unsupported).
        entry["thinkingLevelMap"] = configured.thinking_level_map.model_dump(
            by_alias=True, exclude_unset=True
        )
    if configured.compat:
        entry["compat"] = configured.compat
    return entry


def models_json(model: ResolvedModel, *, api_key: str) -> dict[str, Any]:
    """
    :param model: The model pi runs on.
    :param api_key: What pi's ``apiKey`` says: e.g. ``!cat '<file>'``, which
        pi runs for each request, so a credential that's renewed is read anew.
    :return: pi's ``models.json``, with the model's provider and the model alone.
    """
    provider: dict[str, Any] = {
        "baseUrl": model.base_url,
        "api": model.api,
        "authHeader": model.provider.auth_header,
        "apiKey": api_key,
        "models": [model_entry(model)],
    }
    if model.headers:
        provider["headers"] = {name: literal(value) for name, value in model.headers.items()}
    return {"providers": {provider_name(model): provider}}
