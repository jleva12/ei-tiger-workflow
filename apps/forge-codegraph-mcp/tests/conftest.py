from typing import Any

import pytest
from forge_codegraph_query.testing import SHOP as REPO
from forge_codegraph_query.testing import SHOP_URL, FakeEmbedder, Shop, build_shop
from pydantic_settings import SettingsConfigDict

from forge_codegraph_mcp.core.settings import Environment, Settings

__all__ = ["REPO", "SHOP_URL", "FakeEmbedder", "Shop"]

CURSOR_KEY = "k" * 32

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Mcp-Protocol-Version": "2025-06-18",
}


class IsolatedSettings(Settings):
    """Settings that ignore a developer's local .env file."""

    model_config = SettingsConfigDict(env_file=None)


def make_settings(**overrides: Any) -> Settings:
    spanner = {"cursor_signing_key": CURSOR_KEY, **overrides.pop("spanner", {})}
    return IsolatedSettings(**{"environment": Environment.TEST, "spanner": spanner, **overrides})


@pytest.fixture
def shop() -> Shop:
    return build_shop()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()
