"""Google ADK models from model_provider.yaml. Needs ``forge-common[adk-models]``.

:class:`ProviderModels` is one ADK model an agent runs on. It sends each request
to one of the configuration's models, so a conversation can change model (and
how long it thinks) from one turn to the next::

    config = load_model_provider_config(path)
    models = ProviderModels(config)

    def choose(context: CallbackContext, request: LlmRequest) -> None:
        models.select(request, context.state.get("model"), context.state.get("thinking_level"))

    agent = LlmAgent(name="helper", model=models, before_model_callback=choose)

How each API is spoken:

- ``google-generative-ai``: ADK's own ``Gemini``, with the provider's API key.
  A base URL may end in the API version, as Google's does
  (``https://generativelanguage.googleapis.com/v1beta``). OAuth2 isn't
  supported for it.
- ``openai-responses``, ``openai-completions`` and ``anthropic-messages``: ADK's
  ``LiteLlm``, as ``openai/responses/<id>``, ``openai/<id>`` and
  ``anthropic/<id>``, at the provider's base URL (OpenAI's ends in ``/v1``,
  Anthropic's doesn't), with its key, or its OAuth2 token as the key.

Every model gets its provider's headers merged with its own. Each API sends
its key its own standard way, whatever ``authHeader`` says. ``maxTokens`` caps
each reply on the OpenAI and Anthropic APIs; Gemini's replies keep the model's
own limit.
"""

import weakref
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable, Iterable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from google.adk.models import BaseLlm, Gemini
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import Client, types
from pydantic import PrivateAttr

from forge_common.model_provider import (
    THINKING_LEVELS,
    ConfiguredModel,
    ModelProviderConfig,
    ModelProviderConfigError,
    OAuth2Auth,
    OAuth2TokenSource,
    ResolvedModel,
    ThinkingLevel,
    clamp_thinking_level,
    thinking_value,
)

__all__ = [
    "LlmFactory",
    "ModelCall",
    "ProviderModels",
    "gemini_thinking_config",
    "litellm_model",
]

# Gemini 3's thinking levels for each level; "off" is the least it allows.
_GEMINI_LEVELS = {
    "off": types.ThinkingLevel.MINIMAL,
    "minimal": types.ThinkingLevel.MINIMAL,
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
    "xhigh": types.ThinkingLevel.HIGH,
}
# The same as token budgets, for Gemini 2.x, which has no levels.
_GEMINI_BUDGETS = {
    "off": 0,
    "minimal": 0,
    "low": 1024,
    "medium": 8192,
    "high": 24576,
    "xhigh": 32768,
}
# LiteLLM's model prefix for each API.
_LITELLM_PREFIXES = {
    "openai-responses": "openai/responses/",
    "openai-completions": "openai/",
    "anthropic-messages": "anthropic/",
}


@dataclass(frozen=True)
class ModelCall:
    """
    What one request runs on.

    :ivar model: The configured model.
    :ivar thinking_level: How long it thinks, one of its levels; None leaves
        the provider's default.
    :ivar credential: The provider's API key, or its current OAuth2 token.
    """

    model: ResolvedModel
    thinking_level: ThinkingLevel | None
    credential: str


LlmFactory = Callable[[ModelCall], BaseLlm]


def litellm_model(model: ResolvedModel) -> str:
    """
    :return: The LiteLLM model a model runs as, e.g. ``openai/responses/gpt-5.2``.
    :raises ValueError: Its API isn't one LiteLLM speaks here.
    """
    prefix = _LITELLM_PREFIXES.get(model.api)
    if prefix is None:
        raise ValueError(f"{model.api} isn't an API LiteLLM speaks here")
    return prefix + model.model.id


def gemini_thinking_config(model: ConfiguredModel, level: ThinkingLevel) -> types.ThinkingConfig:
    """
    A thinking level as Gemini's thinking config. Its thoughts are shown unless
    it's ``off``. The model's ``thinkingLevelMap`` may name a Gemini thinking
    level (``xhigh: high``) or, for Gemini 2.x, a token budget.

    :param model: A Gemini model.
    :param level: One of its levels.
    :return: The request's thinking config.
    """
    shown = level != "off"
    value = thinking_value(model, level)
    if model.id.removeprefix("models/").startswith("gemini-2"):
        budget = _GEMINI_BUDGETS[level]
        if value is not None and value.lstrip("-").isdigit():
            budget = int(value)
        return types.ThinkingConfig(include_thoughts=shown, thinking_budget=budget)
    named = value.upper() if value else ""
    thinking_level = (
        types.ThinkingLevel(named)
        if named in types.ThinkingLevel.__members__
        else _GEMINI_LEVELS[level]
    )
    return types.ThinkingConfig(include_thoughts=shown, thinking_level=thinking_level)


def _gemini_http_options(model: ResolvedModel) -> types.HttpOptions:
    url = urlsplit(model.base_url)
    path = url.path.rstrip("/")
    head, _, last = path.rpartition("/")
    # Google's base URLs end in the API version, which genai keeps apart.
    versioned = last[:1] == "v" and last[1:2].isdigit()
    base_url = urlunsplit((url.scheme, url.netloc, (head if versioned else path) + "/", "", ""))
    return types.HttpOptions(
        base_url=base_url,
        api_version=last if versioned else None,
        headers=model.headers or None,
        # Rate limits and overloads are often gone a moment later.
        retry_options=types.HttpRetryOptions(attempts=3),
    )


def _pair_function_call_ids(contents: list[types.Content]) -> None:
    """
    Gives function calls without an id one, and their responses the same one,
    pairing them by name in order.

    ADK strips the ids it made up itself (``adk-…``) before a request unless the
    agent's model is one it knows pairs calls by id, which this router isn't;
    OpenAI and Anthropic refuse a call or result without one. Calls a provider
    gave an id keep it.
    """
    waiting: dict[str, list[str]] = defaultdict(list)
    count = 0
    for content in contents:
        for part in content.parts or ():
            call = part.function_call
            if call is not None and not call.id:
                count += 1
                call.id = f"call_forge_{count}"
                waiting[call.name or ""].append(call.id)
            response = part.function_response
            if response is not None and not response.id:
                queue = waiting.get(response.name or "")
                if queue:
                    response.id = queue.pop(0)


class ProviderModels(BaseLlm):
    """
    An ADK model that runs each request on one of a model_provider.yaml's
    models: the one :meth:`select` chose for it, or else the one the request
    names (``provider/model``, or a model id only one provider has), or else
    the default.

    :param config: The configuration.
    :param default: The model a request runs on when it names none it may
        use: ``provider/model`` or an id; the configuration's default when None.
    :param models: The models requests may run on; all of the configuration's
        when None. The default is always one.
    :param build: Makes the ADK model a request runs on; tests pass their own.
        By default, Gemini or LiteLLM (see the module).
    :param token_transport: The HTTP transport for OAuth2 token requests.
    :raises ModelProviderConfigError: ``default`` or one of ``models`` isn't
        in the configuration, or a model can't be run (OAuth2 on Gemini).
    """

    config: ModelProviderConfig
    # The ``provider/model`` references requests may run on, default first.
    choices: tuple[str, ...]

    _build: LlmFactory = PrivateAttr()
    _token_transport: httpx.AsyncBaseTransport | None = PrivateAttr(default=None)
    _tokens: dict[str, OAuth2TokenSource] = PrivateAttr(default_factory=dict)
    _geminis: dict[tuple[str, str, str], Gemini] = PrivateAttr(default_factory=dict)
    # Thinking levels select chose, by id() of the request, until it runs.
    _levels: dict[int, ThinkingLevel | None] = PrivateAttr(default_factory=dict)

    def __init__(
        self,
        config: ModelProviderConfig,
        *,
        default: str | None = None,
        models: Iterable[str] | None = None,
        build: LlmFactory | None = None,
        token_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Initializes the ModelProvider instance by validating and resolving the model
        configuration. It selects the default model, resolves provided models, and ensures
        that correct authentication is configured for certain provider types.

        :param config: The configuration object for the model provider. It contains
            details about the available models, authentication, and provider-specific
            settings.
        :type config: ModelProviderConfig
        :param default: The name of the default model to use. If not supplied, the
            configuration's default model will be used.
        :type default: str | None
        :param models: An optional iterable of model names to include in this instance.
            If not provided, all models from the configuration will be used.
        :type models: Iterable[str] | None
        :param build: An optional factory function for building or initializing the model.
            If not provided, a default build function will be used.
        :type build: LlmFactory | None
        :param token_transport: An optional async transport mechanism for token-based
            authentication. If not provided, the default transport will be used.
        :type token_transport: httpx.AsyncBaseTransport | None
        """
        def find(name: str, setting: str) -> ResolvedModel:
            found = config.find(name)
            if found is None:
                raise ModelProviderConfigError(
                    f"{setting} names {name}, which the model provider "
                    "configuration doesn't declare (or more than one provider does: "
                    "use provider/model)"
                )
            return found

        first = find(default, "The default model") if default else config.default_model
        chosen = (
            [find(name, "The models") for name in models]
            if models is not None
            else list(config.models())
        )
        served = [first, *(model for model in chosen if model.ref != first.ref)]
        for model in served:
            if model.api == "google-generative-ai" and isinstance(model.provider.auth, OAuth2Auth):
                raise ModelProviderConfigError(
                    f"{model.ref}: google-generative-ai providers need an API key; "
                    "OAuth2 isn't supported for them"
                )
        refs = tuple(dict.fromkeys(model.ref for model in served))
        # BaseLlm's own fields and ours, which type checkers don't see on it.
        fields: dict[str, Any] = {"model": first.ref, "config": config, "choices": refs}
        super().__init__(**fields)
        self._build = build or self._default_build
        self._token_transport = token_transport

    @property
    def available(self) -> list[ResolvedModel]:
        """
        Provides a list of resolved models based on the available choices.

        This method retrieves the models referenced in the `choices` attribute by
        resolving their configuration references and casting them to the
        `ResolvedModel` type.

        :return: A list of resolved models corresponding to the current choices.
        :rtype: list[ResolvedModel]
        """
        return [cast(ResolvedModel, self.config.find(ref)) for ref in self.choices]

    @property
    def default(self) -> ResolvedModel:
        """
        Provides access to the default resolved model configuration.

        This property fetches and returns a specific resolved model instance associated
        with the current configuration and model.

        :return: The resolved model instance associated with the configuration.
        :rtype: ResolvedModel
        """
        return cast(ResolvedModel, self.config.find(self.model))

    def find(self, name: str) -> ResolvedModel | None:
        """
        :param name: ``provider/model``, or a model id only one provider has.
        :return: The model, when requests may run on it.
        """
        found = self.config.find(name)
        return found if found is not None and found.ref in self.choices else None

    def resolve(self, name: object) -> ResolvedModel:
        """:return: The model ``name`` is, when requests may run on it; else the default."""
        found = self.find(name) if isinstance(name, str) and name else None
        return found or self.default

    def select(
        self, request: LlmRequest, model: object = None, thinking_level: object = None
    ) -> ResolvedModel:
        """
        Chooses the model a request runs on and how long it thinks, e.g. from a
        ``before_model_callback`` with the session's choices.

        :param request: The request, which this model is about to run.
        :param model: ``provider/model`` or a model id; anything requests may
            not run on (or not a string) means the default.
        :param thinking_level: off, minimal, low, medium, high or xhigh: the
            nearest the model offers is used. Anything else leaves the
            provider's default (a Gemini model still shows its thoughts).
        :return: The model.
        """
        chosen = self.resolve(model)
        request.model = chosen.ref
        level = thinking_level.lower() if isinstance(thinking_level, str) else None
        key = id(request)
        if key not in self._levels:
            # Forget the choice if the request is dropped before it runs.
            weakref.finalize(request, self._levels.pop, key, None)
        self._levels[key] = (
            clamp_thinking_level(chosen.model, level) if level in THINKING_LEVELS else None
        )
        return chosen

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """
        Generates content asynchronously by initiating a model call and yielding responses.

        This coroutine builds the necessary model call object based on the input request, manages
        configuration specific to the request model, and streams content back asynchronously.

        :param llm_request: The request object containing information about the model and input
            parameters.
        :type llm_request: LlmRequest
        :param stream: Indicates whether the content should be streamed. If set to True, responses
            are provided iteratively; otherwise, the content is processed as a complete result.
            Defaults to False.
        :type stream: bool
        :return: An asynchronous generator yielding LlmResponse objects containing the responses
            produced by the model.
        :rtype: AsyncGenerator[LlmResponse, None]
        """
        target = self.resolve(llm_request.model)
        level = self._levels.pop(id(llm_request), None)
        if level is not None and not target.model.reasoning:
            level = None
        call = ModelCall(target, level, await self._credential(target))
        llm = self._build(call)
        self._prepare(llm_request, call)
        async with aclosing(llm.generate_content_async(llm_request, stream=stream)) as replies:
            async for reply in replies:
                yield reply

    def _prepare(self, request: LlmRequest, call: ModelCall) -> None:
        """
        Prepares and configures the request object based on the model information and call parameters.

        Depending on the selected API and its requirements, this method adjusts the request object to
        ensure compatibility with the model's configurations and constraints. It handles specific
        cases for "google-generative-ai" API, reasoning configurations, and token limits
        for different target models.

        :param request: The request object to be prepared and configured. It contains details about the input,
            output configurations, and model-specific adjustments.
        :type request: LlmRequest
        :param call: The model call that contains the target model and its relevant settings such as
            API type, model ID, and reasoning level.
        :type call: ModelCall
        :return: None
        """
        target = call.model
        if target.api == "google-generative-ai":
            request.model = target.model.id
            if call.thinking_level is not None:
                request.config.thinking_config = gemini_thinking_config(
                    target.model, call.thinking_level
                )
            elif target.model.reasoning and request.config.thinking_config is None:
                # Its own level, with its thoughts shown.
                request.config.thinking_config = types.ThinkingConfig(include_thoughts=True)
            return
        request.model = litellm_model(target)
        # Anthropic requires a limit; Gemini's replies keep the model's own.
        if request.config.max_output_tokens is None:
            request.config.max_output_tokens = target.model.max_tokens
        # Their reasoning effort goes with the model (_default_build).
        request.config.thinking_config = None
        _pair_function_call_ids(request.contents)

    async def _credential(self, target: ResolvedModel) -> str:
        """
        Fetches and returns a credential for a given target by determining the authentication
        method based on its configuration. If the authentication method utilizes OAuth2,
        it retrieves the token from an associated token source or creates one if it does
        not already exist.

        :param target: The resolved target model for which the credential needs to be fetched.
        :type target: ResolvedModel
        :return: The credential as a string.
        :rtype: str
        """
        auth = target.provider.auth
        if not isinstance(auth, OAuth2Auth):
            return auth.key.get_secret_value()
        source = self._tokens.get(target.provider_id)
        if source is None:
            source = OAuth2TokenSource(auth, transport=self._token_transport)
            self._tokens[target.provider_id] = source
        return await source.token()

    def _default_build(self, call: ModelCall) -> BaseLlm:
        """
        Builds and returns an appropriate BaseLlm instance based on the provided model call configuration.
        This method is responsible for determining the specific implementation of BaseLlm
        to initialize, depending on the `api` and model settings in the `call` parameter.

        :param call: Instance of ModelCall containing the configuration for the model,
            including its provider, model ID, API details, headers, and credentials.
        :type call: ModelCall
        :return: A configured instance of BaseLlm, either Gemini or LiteLlm, based on the
            model and provider settings in the given `call`.
        :rtype: BaseLlm
        """
        target = call.model
        if target.api == "google-generative-ai":
            # One client per model and key, whose requests share its connections.
            key = (target.provider_id, target.model.id, call.credential)
            gemini = self._geminis.get(key)
            if gemini is None:
                client = Client(api_key=call.credential, http_options=_gemini_http_options(target))
                gemini = Gemini(model=target.model.id, client=client)
                self._geminis[key] = gemini
            return gemini
        options: dict[str, Any] = {
            "api_base": target.base_url,
            "api_key": call.credential,
            "num_retries": 2,
        }
        if target.headers:
            options["extra_headers"] = target.headers
        effort = thinking_value(target.model, call.thinking_level) if call.thinking_level else None
        if effort is not None:
            options["reasoning_effort"] = effort
        return LiteLlm(model=litellm_model(target), **options)
