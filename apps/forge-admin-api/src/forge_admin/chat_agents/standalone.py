"""A chat agent as a project of its own (Generate standalone agent): what the
starter (``forge_agent_runtime.starter``) is given here.

- The agent: a published version, or the draft, with the saved agents it uses
  bundled in (:mod:`.bundle`).
- The models: this deployment's model provider configuration as it is (its
  keys stay ``${NAME}``s); Gemini with ``${GOOGLE_API_KEY}`` without one.
- The runtime: its wheels, copied into the project's ``vendor/`` until it's on
  PyPI, or a PyPI version (``starter_runtime``).
"""

from pathlib import Path
from typing import Any

import forge_agent_runtime
from forge_agent_runtime.starter import PyPI, Wheels

from forge_admin.assistant.language_models import DEFAULT_GEMINI_MODEL
from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.config import Settings


def starter_runtime(settings: Settings) -> Wheels | PyPI:
    """:return: Where a generated project gets the runtime, as the settings say."""
    if settings.starter_runtime.startswith("pypi:"):
        return PyPI(settings.starter_runtime.removeprefix("pypi:"))
    # In the repository the runtime is installed from its source: its dist
    # folder (make starter-wheels) is beside it.
    default = Path(str(forge_agent_runtime.__file__)).resolve().parents[2] / "dist"
    return Wheels(settings.starter_wheels or default)


def model_provider_yaml(settings: Settings) -> str:
    """
    :return: The model provider configuration a generated project runs on:
        this deployment's file, its keys still ``${NAME}``s, never their values.
    """
    if settings.model_provider_config is not None:
        return settings.model_provider_config.read_text(encoding="utf-8")
    model = settings.agent_model or DEFAULT_GEMINI_MODEL
    return (
        "# The models this agent may run on: Gemini, with a key from Google AI Studio.\n"
        "version: 1\n"
        f"default: {{provider: google, model: {model}}}\n"
        "providers:\n"
        "  google:\n"
        "    name: Google Gemini\n"
        "    baseUrl: https://generativelanguage.googleapis.com/v1beta\n"
        "    api: google-generative-ai\n"
        "    auth: {type: apiKey, key: ${GOOGLE_API_KEY}}\n"
        "    models:\n"
        f"      - {{id: {model}, reasoning: true, input: [text, image]}}\n"
    )


async def standalone_document(
    store: ChatAgentStore, organization_id: str, agent_id: str, version: int | str
) -> dict[str, Any] | None:
    """
    :param version: A published version's number, or ``"draft"``.
    :return: That version's document (its ``version`` set), or None when the
        agent has no such version (or no draft).
    :raises PyMongoError: The store isn't answering.
    """
    if version == "draft":
        record = await store.get(organization_id, agent_id)
        if record is None or not record.get("has_draft"):
            return None
        return {**record["document"], "version": "draft"}
    found = await store.version(organization_id, agent_id, int(version))
    return found["document"] if found is not None else None
