"""The fixtures of an organization's access without MySQL (key_world)."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from key_world import World, build_world

from forge_admin.config import Settings


@pytest.fixture
def make_world(settings: Settings, tmp_path: Path) -> Iterator[Callable[..., World]]:
    clients: list[TestClient] = []

    def make(*, public: bool = True, **changes: Any) -> World:
        world = build_world(settings, tmp_path, public=public, **changes)
        clients.append(world.client)
        return world

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def world(make_world: Callable[..., World]) -> World:
    return make_world()
