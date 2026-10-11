"""The admin API's code graph: on with a database and a cursor key, off
without them, and off (not failing startup) when its client can't be made."""

import asyncio

import pytest
from forge_codegraph_query import CodeGraph, CodeGraphSettings
from forge_codegraph_query.settings import LOCAL_DATABASE

from forge_admin.config import Settings
from forge_admin.knowledge.graph import codegraph_from, not_set_up

KEY = "k" * 32


def with_codegraph(settings: Settings, **spanner: str) -> Settings:
    return settings.model_copy(
        update={"codegraph": CodeGraphSettings.model_validate({"spanner": spanner})}
    )


def test_the_code_graph_opens_with_a_database_and_a_key(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The emulator keeps the client off Google's credentials; nothing is read.
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "localhost:19030")
    graph = codegraph_from(
        with_codegraph(settings, database=LOCAL_DATABASE, cursor_signing_key=KEY)
    )
    assert isinstance(graph, CodeGraph)
    asyncio.run(graph.close())


def test_without_a_database_or_a_key_it_is_off(settings: Settings) -> None:
    off = with_codegraph(settings, database="", cursor_signing_key=KEY)
    assert codegraph_from(off) is None
    assert "FORGE_ADMIN_CODEGRAPH__SPANNER__DATABASE" in not_set_up(off)
    keyless = with_codegraph(settings, database=LOCAL_DATABASE)
    assert codegraph_from(keyless) is None
    assert "FORGE_ADMIN_CODEGRAPH__SPANNER__CURSOR_SIGNING_KEY" in not_set_up(keyless)


def test_a_client_that_cant_be_made_leaves_it_off(settings: Settings) -> None:
    broken = with_codegraph(
        settings,
        database=LOCAL_DATABASE,
        cursor_signing_key=KEY,
        credentials_json="not a service account key",
    )
    assert codegraph_from(broken) is None
