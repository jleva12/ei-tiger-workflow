import asyncio
from typing import Any, cast

from forge_admin.chat_agents.bundle import bundle
from forge_admin.chat_agents.store import ChatAgentStore

DOC: dict[str, Any] = {
    "nodes": [
        {"id": "agent", "kind": "agent", "name": "Support", "config": {}},
        {"id": "kb", "kind": "knowledge_base", "name": "Help center", "config": {}},
        {"id": "mcp", "kind": "mcp", "name": "Tickets", "config": {"server": "ms_1"}},
    ]
}


def test_what_runs_only_in_forge_is_noted_unless_the_starter_notes_it() -> None:
    # Without saved agents the store isn't asked anything.
    store = cast(ChatAgentStore, None)
    _, exported = asyncio.run(bundle(store, "org", DOC))
    assert exported == [
        "Help center searches the organization's knowledge bases, which only Forge's runtime can",
        "Tickets uses one of the organization's MCP servers",
    ]
    _, standalone = asyncio.run(bundle(store, "org", DOC, hosted=False))
    assert standalone == []
