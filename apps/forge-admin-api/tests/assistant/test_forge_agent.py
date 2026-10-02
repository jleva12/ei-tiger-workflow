"""The Forge assistant's setup: its model, thinking levels and storage."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forge_common.adk.models import ModelCall, ProviderModels
from forge_common.model_provider import ModelProviderConfigError
from google.adk.agents import LlmAgent
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.sessions.schemas import v0, v1
from google.genai import types
from pydantic import SecretStr
from scripted_llm import ScriptedLlm

from forge_admin.assistant import ADK_TABLES, forge, language_models
from forge_admin.assistant.page_context import describe_page, page_context_from
from forge_admin.assistant.person import Person, PersonOrganization, describe_person
from forge_admin.assistant.runtime import NO_API_KEY, AgentRuntime
from forge_admin.config import Settings
from forge_admin.db.session import create_engine


def thinking(settings: Settings, model: str, level: object) -> Any:
    """The thinking config a turn reaches Gemini with, for the session's choices."""
    scripted = ScriptedLlm(turns=[[types.Part(text="Hi.")]])
    choices = settings.model_copy(update={"agent_models": ["gemini-2.5-flash"]})
    models = language_models.provider_models(choices, build=lambda call: scripted)
    select = forge.model_selection(models)
    request = LlmRequest()
    state = {"model": model, "thinking_level": level}
    select(SimpleNamespace(state=SimpleNamespace(to_dict=lambda: state)), request)  # type: ignore[arg-type]

    async def reply() -> None:
        async for _ in models.generate_content_async(request):
            pass

    asyncio.run(reply())
    [sent] = scripted.requests
    return sent.config.thinking_config


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (
            "off",
            types.ThinkingConfig(
                include_thoughts=False, thinking_level=types.ThinkingLevel.MINIMAL
            ),
        ),
        (
            "low",
            types.ThinkingConfig(
                include_thoughts=True, thinking_level=types.ThinkingLevel.LOW
            ),
        ),
        (
            "Medium",
            types.ThinkingConfig(
                include_thoughts=True, thinking_level=types.ThinkingLevel.MEDIUM
            ),
        ),
        (
            "high",
            types.ThinkingConfig(
                include_thoughts=True, thinking_level=types.ThinkingLevel.HIGH
            ),
        ),
        # Gemini has no extra-high: the nearest it has.
        (
            "xhigh",
            types.ThinkingConfig(
                include_thoughts=True, thinking_level=types.ThinkingLevel.HIGH
            ),
        ),
        # No choice, or an unknown one: the model's own level, thoughts shown.
        (None, types.ThinkingConfig(include_thoughts=True)),
        ("extreme", types.ThinkingConfig(include_thoughts=True)),
    ],
)
def test_gemini_3_thinks_at_a_level(
    settings: Settings, level: object, expected: types.ThinkingConfig
) -> None:
    assert thinking(settings, "gemini-3.5-flash", level) == expected


def test_gemini_2_thinks_within_a_budget(settings: Settings) -> None:
    off = thinking(settings, "gemini-2.5-flash", "off")
    assert off == types.ThinkingConfig(include_thoughts=False, thinking_budget=0)
    high = thinking(settings, "google/gemini-2.5-flash", "high")
    assert high == types.ThinkingConfig(include_thoughts=True, thinking_budget=24576)


def test_without_a_model_provider_configuration_it_offers_gemini(
    settings: Settings,
) -> None:
    choices = settings.model_copy(
        update={"agent_model": "gemini-2.5-flash", "agent_models": ["gemini-3.5-pro"]}
    )

    models = language_models.provider_models(choices)

    assert models.choices == ("google/gemini-2.5-flash", "google/gemini-3.5-pro")
    assert [language_models.describe(model) for model in models.available][1] == {
        "id": "google/gemini-3.5-pro",
        "provider": "google",
        "provider_name": "Google Gemini",
        "model": "gemini-3.5-pro",
        "name": "Gemini 3.5 Pro",
        "api": "google-generative-ai",
        "reasoning": True,
        "input": ["text", "image"],
        "context_window": 128_000,
        "max_tokens": 16_384,
        "thinking_levels": ["off", "minimal", "low", "medium", "high"],
    }
    assert language_models.accepted_names(models) == {
        "google/gemini-2.5-flash",
        "gemini-2.5-flash",
        "google/gemini-3.5-pro",
        "gemini-3.5-pro",
    }


PROVIDERS = """
version: 1
default: {provider: openai, model: gpt-5.2}
providers:
  openai:
    name: OpenAI API
    baseUrl: https://api.openai.com/v1
    api: openai-responses
    auth: {type: apiKey, key: "${OPENAI_API_KEY}"}
    models:
      - {id: gpt-5.2, reasoning: true, thinkingLevelMap: {xhigh: xhigh}}
      - {id: gpt-4.1}
  google:
    baseUrl: https://generativelanguage.googleapis.com/v1beta
    api: google-generative-ai
    auth: {type: apiKey, key: "${GOOGLE_API_KEY}"}
    models:
      - {id: gemini-3.5-flash, reasoning: true}
"""
PROVIDER_ENVIRONMENT = {"OPENAI_API_KEY": "sk-test", "GOOGLE_API_KEY": "g-test"}


def with_providers(settings: Settings, tmp_path: Path, **update: Any) -> Settings:
    config = tmp_path / "model_provider.yaml"
    config.write_text(PROVIDERS)
    return settings.model_copy(update={"model_provider_config": config, **update})


def test_with_a_model_provider_configuration_it_offers_its_models(
    settings: Settings, tmp_path: Path
) -> None:
    configured = with_providers(settings, tmp_path)

    models = language_models.provider_models(
        configured, environment=PROVIDER_ENVIRONMENT
    )

    assert models.choices == (
        "openai/gpt-5.2",
        "openai/gpt-4.1",
        "google/gemini-3.5-flash",
    )
    described = language_models.describe(models.default)
    assert described["thinking_levels"] == [
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert "sk-test" not in repr(described)


def test_agent_model_and_agent_models_narrow_the_configuration(
    settings: Settings, tmp_path: Path
) -> None:
    configured = with_providers(
        settings,
        tmp_path,
        agent_model="gemini-3.5-flash",
        agent_models=["openai/gpt-4.1"],
    )

    models = language_models.provider_models(
        configured, environment=PROVIDER_ENVIRONMENT
    )

    assert models.choices == ("google/gemini-3.5-flash", "openai/gpt-4.1")
    unknown = configured.model_copy(update={"agent_model": "gemini-9-ultra"})
    with pytest.raises(ModelProviderConfigError, match="gemini-9-ultra"):
        language_models.provider_models(unknown, environment=PROVIDER_ENVIRONMENT)
    with pytest.raises(ModelProviderConfigError, match="OPENAI_API_KEY"):
        language_models.provider_models(configured, environment={})


def test_a_page_context_keeps_what_was_sent() -> None:
    sent = {"path": "/", "title": "Home", "focus": None, "search": {"open": True}}

    assert page_context_from(sent) == {
        "path": "/",
        "title": "Home",
        "params": {},
        "search": {"open": True},
        "breadcrumbs": [],
        "entities": [],
        "view": {},
    }


@pytest.mark.parametrize(
    "sent",
    [
        {"title": "No path"},
        {"path": "/", "entities": [{"kind": "Task!", "id": "1"}]},
        {"path": "/", "view": {"not a key": "x"}},
        {"path": "/", "view": {"nested": {"a": 1}}},
        {"path": "/", "title": "x" * 121},
        "/organizations",
    ],
)
def test_a_malformed_page_context_is_left_out(sent: object) -> None:
    assert page_context_from(sent) is None


def test_a_conversation_without_a_page_context_says_nothing_of_it() -> None:
    assert describe_page({}) is None


def test_a_long_list_of_organizations_is_cut_short() -> None:
    organizations = [
        PersonOrganization(id=f"o-{n}", name=f"Org {n}", roles=["org:member"])
        for n in range(33)
    ]

    text = describe_person(Person(id="u-1", organizations=organizations).model_dump())

    assert text is not None
    assert '"Org 29"' in text and '"Org 30"' not in text
    assert text.endswith("and 3 more.")


def test_without_a_key_the_assistant_is_unavailable(settings: Settings) -> None:
    runtime = AgentRuntime.create(settings, create_engine(settings))

    assert runtime.unavailable == NO_API_KEY
    assert list(runtime.runners) == [forge.APP_NAME]


def test_with_a_key_gemini_uses_it(settings: Settings) -> None:
    keyed = settings.model_copy(update={"google_api_key": SecretStr("test-key")})

    runtime = AgentRuntime.create(keyed, create_engine(keyed))
    agent = runtime.runners[forge.APP_NAME].agent
    assert isinstance(agent, LlmAgent)
    models = agent.model
    assert isinstance(models, ProviderModels)
    assert runtime.models == {forge.APP_NAME: models}

    assert runtime.unavailable is None
    assert models.default.ref == "google/gemini-3.5-flash"
    key = asyncio.run(models._credential(models.default))
    gemini = models._default_build(ModelCall(models.default, None, key))
    assert isinstance(gemini, Gemini) and gemini.model == "gemini-3.5-flash"
    assert gemini.api_client._api_client.api_key == "test-key"


def test_with_a_model_provider_configuration_no_gemini_key_is_needed(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = with_providers(settings, tmp_path)
    for name, value in PROVIDER_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    # Only the process environment: no .env.
    monkeypatch.chdir(tmp_path)

    runtime = AgentRuntime.create(configured, create_engine(configured))

    assert runtime.unavailable is None
    assert runtime.models[forge.APP_NAME].default.ref == "openai/gpt-5.2"


def test_migrations_skip_exactly_the_tables_adk_manages() -> None:
    adk = set(v0.Base.metadata.tables) | set(v1.Base.metadata.tables)
    assert adk == ADK_TABLES
