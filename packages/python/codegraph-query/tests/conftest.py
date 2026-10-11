import pytest

from forge_codegraph_query.testing import SHOP as REPO
from forge_codegraph_query.testing import FakeEmbedder, Shop, build_shop

__all__ = ["REPO", "FakeEmbedder", "Shop"]

CURSOR_KEY = "k" * 32


@pytest.fixture
def shop() -> Shop:
    return build_shop()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()
